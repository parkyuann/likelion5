"""kosis_indexer.py — 실전2 T2-2 (검색 DB·색인)

KOSIS 표 카탈로그를 Qdrant 벡터DB에 색인하고, 검색 후보를 공용 스키마
ClaimTableMapping으로 반환하는 저수준 검색 함수(dense_search)를 제공한다.

- 표 1개 = point 1개: [dense 벡터(doc_meta_text 임베딩)] + [payload(메타)]
- 멱등 색인: point id를 table_key에서 결정론적으로 생성 → 재색인 시 덮어씀
- 임베딩 캐시로 재실행 비용 절감, 색인 비용 리포트 출력

CLI:
    python src/kosis_indexer.py index  --limit 200
    python src/kosis_indexer.py index  --dry-run           # 임베딩/Qdrant 없이 색인 대상만 확인
    python src/kosis_indexer.py search --query "혼인 건수" --top-n 5
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from retrieval_schema import ClaimTableMapping, KOSISTable, validate_table  # 공용 스키마

# --- 경로 (src/kosis_indexer.py 기준) ---
PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_DATA = PROJECT_ROOT / "data" / "kosis_catalog_v1.jsonl"   # 색인 대상 카탈로그
OUTPUT_DATA = PROJECT_ROOT / "output" / "kosis_index"           # 캐시·리포트 저장 위치
LOCAL_DB = PROJECT_ROOT / "output" / "kosis_qdrant"             # 로컬(파일) Qdrant 저장소

# --- 색인 상수 ---
COLLECTION_NAME = "kosis_tables"
QDRANT_URL = "http://localhost:6333"
EMBEDDING_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2"
# table_key → point id 결정론적 생성용 고정 네임스페이스(멱등성 핵심)
POINT_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
CACHE_PATH = OUTPUT_DATA / "embed_cache.jsonl"
REPORT_PATH = OUTPUT_DATA / "index_cost_report.json"


# ------------------------------------------------------------------ 임베딩 (CLOVA)

def _api_key() -> str:
    """프로젝트 공통 방식으로 CLOVA 키를 해석한다(HCX_API_KEY 우선)."""
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    key = (os.getenv("HCX_API_KEY") or os.getenv("NCP_CLOVASTUDIO_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("HCX_API_KEY 또는 NCP_CLOVASTUDIO_API_KEY가 .env에 필요합니다.")
    return key


def embed(text: str) -> list[float]:
    """텍스트 1건을 CLOVA 임베딩 v2 벡터로 변환한다."""
    import requests
    headers = {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}
    res = requests.post(EMBEDDING_URL, headers=headers, json={"text": text}, timeout=15)
    data = res.json()
    if res.status_code != 200 or data.get("status", {}).get("code") != "20000":
        raise RuntimeError(f"임베딩 API 오류 (status={res.status_code}): {data.get('status')}")
    return data["result"]["embedding"]


# ------------------------------------------------------------------ 카탈로그 로딩

def iter_catalog(path: Path, limit: int | None = None, offset: int = 0, on_bad_line=None):
    """카탈로그 jsonl을 한 줄씩 dict로 흘려보낸다(대용량 스트리밍).

    offset: 앞에서 이만큼 건너뛴다. limit: 그 이후 최대 N개.
    깨진 JSON 줄은 죽지 않고 건너뛰며, on_bad_line(줄번호, 사유)로 알린다.
    """
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i < offset:
                continue
            if limit is not None and i >= offset + limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                if on_bad_line:
                    on_bad_line(i, str(e))


def meta_text(record: dict) -> str:
    """Dense 임베딩 대상 텍스트를 고른다.

    v2 카탈로그면 doc_meta_text를 우선, v1이면 document_text를 쓴다.
    둘 다 없으면 표 이름+분류경로로 최소 구성한다.
    """
    if record.get("doc_meta_text"):
        return record["doc_meta_text"]
    if record.get("document_text"):
        return record["document_text"]
    paths = record.get("category_paths") or []
    flat_path = " ".join(seg for p in paths for seg in p)
    return f"{record.get('tbl_name', '')} {flat_path}".strip()


def validate_record(record) -> str | None:
    """색인 입력으로 유효한지 검사. 유효하면 None, 아니면 사유 문자열(C-1).

    - 공용 스키마 validate_table()로 필수 필드/키 형식(table_key=org_id:tbl_id) 검증
    - 임베딩할 텍스트가 실제로 있는지 확인
    """
    if not isinstance(record, dict):
        return "레코드가 dict가 아님"
    errs = validate_table(KOSISTable(
        table_key=record.get("table_key") or "",
        org_id=record.get("org_id") or "",
        org_name=record.get("org_name") or "",
        tbl_id=record.get("tbl_id") or "",
        tbl_name=record.get("tbl_name") or "",
    ))
    if errs:
        return "; ".join(errs)
    if not meta_text(record).strip():
        return "임베딩할 텍스트 없음"
    return None


def point_id(table_key: str) -> str:
    """table_key로부터 항상 같은 point id 생성 → 재색인 시 덮어쓰기(멱등)."""
    return str(uuid.uuid5(POINT_NAMESPACE, table_key))


def build_payload(record: dict) -> dict:
    """검색 필터·결과 표시에 쓸 메타데이터만 골라 payload로 만든다."""
    return {
        "table_key": record.get("table_key"),
        "org_id": record.get("org_id"),
        "tbl_id": record.get("tbl_id"),
        "tbl_name": record.get("tbl_name"),
        "stat_id": record.get("stat_id"),
        "category_paths": record.get("category_paths"),
        # v2 메타(있으면 필터/디부스트에 사용, 없으면 None)
        "period_types": record.get("period_types"),
        "units": record.get("units"),
        "version_status": record.get("version_status"),
        "latest_period": record.get("latest_period"),
        "document_text": record.get("document_text"),
    }


# ------------------------------------------------------------------ 임베딩 캐시

class EmbedCache:
    """텍스트 md5 → 벡터 로컬 캐시. 재실행 시 같은 텍스트는 API를 다시 부르지 않는다."""

    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self.mem: dict[str, list[float]] = {}
        self.hits = 0
        self.misses = 0
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for line in f:
                    row = json.loads(line)
                    self.mem[row["k"]] = row["v"]

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def _put(self, text: str, vec: list[float]):
        k = self._key(text)
        self.mem[k] = vec
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"k": k, "v": vec}) + "\n")

    def embed_many(self, texts: list[str], embed_fn, workers: int = 1) -> dict[str, list[float]]:
        """texts를 임베딩해 {text: vec} 반환. 캐시 미스만 실제 호출, workers>1이면 병렬."""
        need, seen = [], set()
        for t in texts:
            k = self._key(t)
            if k not in self.mem and k not in seen:
                need.append(t)
                seen.add(k)
        if need:
            if workers > 1:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    vecs = list(ex.map(embed_fn, need))
            else:
                vecs = [embed_fn(t) for t in need]
            for t, v in zip(need, vecs):   # 파일 기록은 병렬 구간 밖에서(경합 방지)
                self._put(t, v)
        self.misses += len(need)
        self.hits += len(texts) - len(need)
        return {t: self.mem[self._key(t)] for t in texts}


# ------------------------------------------------------------------ Qdrant 색인기

def make_client(local: bool):
    """Qdrant 클라이언트 생성. local=True면 Docker 없이 파일(로컬) 저장소 사용."""
    from qdrant_client import QdrantClient
    if local:
        return QdrantClient(path=str(LOCAL_DB))
    return QdrantClient(url=QDRANT_URL)


class KosisIndexer:
    """Qdrant 컬렉션 생성 + 카탈로그 색인."""

    def __init__(self, collection: str = COLLECTION_NAME, local: bool = False):
        self.collection = collection
        self.local = local
        self._client = None

    @property
    def client(self):
        # qdrant-client는 실제 필요 시점에만 import/연결(dry-run은 불필요)
        if self._client is None:
            self._client = make_client(self.local)
        return self._client

    def ensure_collection(self, vector_size: int):
        """컬렉션이 없으면 dense 벡터용으로 생성한다(코사인 거리)."""
        from qdrant_client.models import Distance, VectorParams
        existing = {c.name for c in self.client.get_collections().collections}
        if self.collection not in existing:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )

    def index(self, catalog_path: Path, limit: int | None, batch_size: int, dry_run: bool,
              workers: int = 1, offset: int = 0):
        """카탈로그를 읽어 임베딩 후 Qdrant에 배치 upsert. 비용 리포트를 남긴다.

        workers>1이면 청크 단위로 임베딩을 병렬 호출한다.
        """
        cache = EmbedCache()
        collection_ready = False
        t0 = time.time()
        total = 0
        skipped = []
        chunk = []

        def log_skip(ident, reason):
            skipped.append({"id": ident, "reason": reason})

        def flush(records):
            nonlocal collection_ready, total
            texts = [meta_text(r) for r in records]
            vecs = cache.embed_many(texts, embed, workers)     # 청크 병렬 임베딩
            if not collection_ready:
                self.ensure_collection(len(next(iter(vecs.values()))))
                collection_ready = True
            points = [{
                "id": point_id(r["table_key"]),
                "vector": vecs[t],
                "payload": build_payload(r),
            } for r, t in zip(records, texts)]
            self._upsert(points)
            total += len(points)

        for record in iter_catalog(
            catalog_path, limit, offset,
            on_bad_line=lambda i, e: log_skip(f"line#{i}", f"JSON 파싱 오류: {e}"),
        ):
            reason = validate_record(record)          # C-1: 입력 검증
            if reason:
                log_skip(record.get("table_key") if isinstance(record, dict) else "?", reason)
                continue
            if dry_run:
                total += 1
                if total <= 5:
                    print(f"[dry-run] {record['table_key']}  meta_text='{meta_text(record)[:60]}...'")
                continue
            chunk.append(record)
            if len(chunk) >= batch_size:
                flush(chunk)
                chunk = []
        if chunk and not dry_run:
            flush(chunk)

        elapsed = time.time() - t0
        skipped_log = None
        if skipped:                                 # C-1: 스킵된 레코드 사유 로깅
            OUTPUT_DATA.mkdir(parents=True, exist_ok=True)
            skipped_log = OUTPUT_DATA / "skipped_records.jsonl"
            with skipped_log.open("w", encoding="utf-8") as f:
                for s in skipped:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")
        report = {
            "catalog": str(catalog_path),
            "dry_run": dry_run,
            "workers": workers,
            "indexed_points": total,
            "skipped_records": len(skipped),         # C-1: 검증 탈락 수
            "embed_api_calls": cache.misses,       # 실제 호출 수
            "embed_cache_hits": cache.hits,        # 캐시로 아낀 호출 수
            "elapsed_sec": round(elapsed, 2),
            "points_per_sec": round(total / elapsed, 2) if elapsed else None,
        }
        if skipped_log:
            report["skipped_log"] = str(skipped_log)
        if not dry_run:
            OUTPUT_DATA.mkdir(parents=True, exist_ok=True)
            REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n=== 색인 리포트 ===")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report

    def _upsert(self, points: list[dict]):
        from qdrant_client.models import PointStruct
        self.client.upsert(
            collection_name=self.collection,
            points=[PointStruct(**p) for p in points],
        )


# ------------------------------------------------------------------ 저수준 검색 함수 (검색·랭킹 단계에 제공)

def dense_search(query: str, top_n: int = 50, meta_filter: dict | None = None,
                 collection: str = COLLECTION_NAME, local: bool = False) -> list[ClaimTableMapping]:
    """doc_meta_text 임베딩 공간에서 질의와 의미가 가까운 표 top_n을 찾는다.

    반환: ClaimTableMapping 리스트(검색 단계 필드만 채움).
        - 채움: table_key, retrieval_stage="dense", rank, dense_score, status="CANDIDATE"
        - None: reranker_score / matched_* / align_status  (리랭킹·정렬 단계에서 채움)

    meta_filter 예: {"org_id": "101"} → 해당 기관 표로만 제한.
    """
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    client = make_client(local)
    qvec = embed(query)

    qfilter = None
    if meta_filter:
        qfilter = Filter(must=[
            FieldCondition(key=k, match=MatchValue(value=v))
            for k, v in meta_filter.items()
        ])

    hits = client.query_points(
        collection_name=collection,
        query=qvec,
        limit=top_n,
        query_filter=qfilter,
        with_payload=True,
    ).points

    results: list[ClaimTableMapping] = []
    for rank, hit in enumerate(hits, start=1):
        payload = hit.payload or {}
        results.append(ClaimTableMapping(
            mapping_id=str(uuid.uuid4()),
            claim_id="",                       # 호출측이 실제 claim_id를 채워 연결
            table_key=payload.get("table_key", ""),
            retrieval_stage="dense",
            rank=rank,
            dense_score=float(hit.score),
            status="CANDIDATE",
            # 아래는 검색 단계에서 비움 = 인터페이스 계약
            sparse_score=None,
            reranker_score=None,
            align_status=None,
        ))
    return results


# ------------------------------------------------------------------ CLI

def main():
    parser = argparse.ArgumentParser(description="KOSIS 표 Qdrant 색인기 (T2-2)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="카탈로그를 Qdrant에 색인")
    p_index.add_argument("--input", type=Path, default=INPUT_DATA)
    p_index.add_argument("--limit", type=int, default=None, help="앞에서 N개만(스모크용)")
    p_index.add_argument("--batch-size", type=int, default=64)
    p_index.add_argument("--workers", type=int, default=1, help="임베딩 동시 요청 수(>1이면 병렬)")
    p_index.add_argument("--offset", type=int, default=0, help="앞에서 N개 건너뛰기")
    p_index.add_argument("--dry-run", action="store_true", help="임베딩/Qdrant 없이 색인 대상만 확인")
    p_index.add_argument("--local", action="store_true", help="Docker 없이 로컬(파일) Qdrant 사용")

    p_search = sub.add_parser("search", help="dense_search 동작 확인")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--top-n", type=int, default=5)
    p_search.add_argument("--org-id", default=None, help="특정 기관으로 필터")
    p_search.add_argument("--local", action="store_true", help="Docker 없이 로컬(파일) Qdrant 사용")

    args = parser.parse_args()

    if args.cmd == "index":
        KosisIndexer(local=args.local).index(
            args.input, args.limit, args.batch_size, args.dry_run, args.workers, args.offset)
    elif args.cmd == "search":
        meta_filter = {"org_id": args.org_id} if args.org_id else None
        rows = dense_search(args.query, args.top_n, meta_filter, local=args.local)
        for r in rows:
            print(f"#{r.rank:>2}  score={r.dense_score:.4f}  {r.table_key}")
        print(f"\n총 {len(rows)}개 후보 (retrieval_stage=dense)")


if __name__ == "__main__":
    main()
