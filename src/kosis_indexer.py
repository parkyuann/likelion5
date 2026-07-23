"""kosis_catalog_enriched.jsonl 을 Qdrant 벡터DB에 색인한다 (실전2 T2-2).

표 하나를 세 갈래로 저장한다 — T2-1에서 필드를 나눈 이유가 여기서 쓰인다.

  dense  : doc_meta_text 를 HCX 임베딩(1024차원)으로. '의미' 검색용.
  sparse : doc_item_index 를 BM25 가중 희소벡터로. '키워드' 검색용.
  payload: org_id, units, dimensions, period_types, latest_period 등. 메타 '필터'용.

Qdrant는 로컬 임베디드 모드(QdrantClient(path=...))로 쓴다 — 별도 서버·Docker 없이
파일로 관리된다. 나중에 서버로 옮겨도 코드는 client 생성부만 바꾸면 된다.

멱등성(재색인 idempotent): point id를 table_key 기반 UUID5로 고정하므로 두 번
돌려도 같은 표는 덮어쓰기되어 컬렉션 수가 불어나지 않는다. dense 임베딩은
data/kosis_embedding_cache.jsonl 에 캐시해 재실행 시 API를 다시 부르지 않는다.

BM25 모델(어휘·IDF·평균문서길이)은 data/kosis_bm25_model.json 에 저장한다 —
T2-3 검색기가 질의를 같은 방식으로 인코딩하려면 이 파일이 필요하다. 문서 쪽
희소벡터 값에는 BM25 항 가중치(IDF 포함)를 이미 넣어두므로, 질의 쪽은 존재하는
항마다 값 1.0 을 주면 내적이 곧 BM25 점수 합이 된다.

사용 예 (레포 루트에서):
    # 표본(현재 kosis_catalog_enriched.jsonl) 색인
    venv/Scripts/python.exe src/kosis_indexer.py

    # 컬렉션을 지우고 처음부터 (벡터 설정을 바꿨을 때)
    venv/Scripts/python.exe src/kosis_indexer.py --recreate
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
import uuid
from collections import Counter
from pathlib import Path

from qdrant_client import QdrantClient, models

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.hcx_embedding_client import embed  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT / "data" / "kosis_catalog_enriched.jsonl"
DEFAULT_QDRANT_PATH = ROOT / "data" / "qdrant_kosis"
DEFAULT_BM25_MODEL = ROOT / "data" / "kosis_bm25_model.json"
DEFAULT_EMBED_CACHE = ROOT / "data" / "kosis_embedding_cache.jsonl"

COLLECTION = "kosis_tables"
DENSE_NAME = "dense"
SPARSE_NAME = "lexical"
DENSE_DIM = 1024  # HCX 임베딩 v2 출력 차원(공식 문서)

# table_key -> point UUID 를 결정적으로 만들기 위한 네임스페이스(임의 고정값).
# 이 값이 바뀌면 같은 표라도 다른 point id가 되어 멱등성이 깨지므로 고정한다.
POINT_NAMESPACE = uuid.UUID("6b1e7a3c-4d2f-4e8a-9c1b-0a5f7d3e2c11")

BM25_K1 = 1.5
BM25_B = 0.75
EMBED_SLEEP_SEC = 0.05  # HCX 임베딩 연속 호출 간 최소 간격


# ---------------------------------------------------------------- BM25 (sparse)

def tokenize(text: str) -> list[str]:
    """doc_item_index/질의를 토큰화한다.

    doc_item_index 는 이미 항목명·차원값이 공백으로 이어진 형태라 공백 분리가 기본이다.
    다만 '서울특별시'처럼 붙은 복합어는 그대로 한 토큰으로 둔다(부분일치는 T2-3 리랭커
    단계의 몫). 영문/숫자는 소문자화한다.
    """
    text = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return [t for t in text.split(" ") if t]


def build_bm25_model(catalog: list[dict]) -> dict:
    """코퍼스 전체에서 어휘·IDF·평균문서길이를 계산한다."""
    vocab: dict[str, int] = {}
    doc_freq: Counter = Counter()
    lengths: list[int] = []

    for record in catalog:
        tokens = tokenize(record.get("doc_item_index", ""))
        lengths.append(len(tokens))
        for term in set(tokens):
            if term not in vocab:
                vocab[term] = len(vocab)
            doc_freq[term] += 1

    n_docs = len(catalog)
    avgdl = (sum(lengths) / n_docs) if n_docs else 0.0
    # BM25 IDF: log(1 + (N - df + 0.5)/(df + 0.5)) — 항상 양수.
    idf = {
        term: math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        for term, df in doc_freq.items()
    }
    return {
        "vocab": vocab,
        "idf": idf,
        "avgdl": avgdl,
        "n_docs": n_docs,
        "k1": BM25_K1,
        "b": BM25_B,
        "note": "질의 인코딩: 존재하는 항마다 value=1.0. 문서 값에 BM25 가중치가 이미 반영됨.",
    }


def encode_doc_sparse(text: str, model: dict) -> models.SparseVector:
    """문서의 doc_item_index 를 BM25 가중 희소벡터로 만든다."""
    tokens = tokenize(text)
    dl = len(tokens)
    tf = Counter(tokens)
    avgdl = model["avgdl"] or 1.0
    k1, b = model["k1"], model["b"]

    indices: list[int] = []
    values: list[float] = []
    for term, freq in tf.items():
        idx = model["vocab"].get(term)
        if idx is None:
            continue
        idf = model["idf"].get(term, 0.0)
        # BM25 문서항 가중치.
        weight = idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * dl / avgdl))
        if weight > 0:
            indices.append(idx)
            values.append(weight)
    return models.SparseVector(indices=indices, values=values)


# ----------------------------------------------------------- dense (embedding)

def load_embed_cache(path: Path) -> dict[str, list[float]]:
    """table_key+텍스트해시 -> 임베딩 캐시. 재실행 시 API 재호출을 막는다."""
    if not path.exists():
        return {}
    cache: dict[str, list[float]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "key" in row and "vector" in row:
                cache[row["key"]] = row["vector"]
    return cache


def embed_cache_key(table_key: str, text: str) -> str:
    """텍스트가 바뀌면 캐시가 무효가 되도록 table_key + 텍스트 해시로 키를 만든다.

    파이썬 내장 hash()는 프로세스마다 값이 달라(문자열 해시 랜덤화) 재실행 시 캐시가
    전부 어긋난다. 프로세스 간 안정적인 hashlib을 써야 한다.
    """
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    return f"{table_key}|{digest}"


def get_dense_vector(record: dict, cache: dict, cache_handle, stats: dict) -> list[float]:
    text = record.get("doc_meta_text", "")
    key = embed_cache_key(record["table_key"], text)
    if key in cache:
        stats["embed_cache_hits"] += 1
        return cache[key]
    vector = embed(text)
    stats["embed_api_calls"] += 1
    time.sleep(EMBED_SLEEP_SEC)
    cache[key] = vector
    cache_handle.write(json.dumps({"key": key, "vector": vector}, ensure_ascii=False) + "\n")
    cache_handle.flush()
    return vector


# --------------------------------------------------------------------- payload

def build_payload(record: dict) -> dict:
    """메타 프리필터·표시에 쓸 값. 큰 텍스트(doc_item_index)는 payload에 넣지 않는다."""
    return {
        "table_key": record["table_key"],
        "org_id": record.get("org_id"),
        "org_name": record.get("org_name"),
        "tbl_id": record.get("tbl_id"),
        "tbl_name": record.get("tbl_name"),
        "stat_id": record.get("stat_id"),
        "category_paths": record.get("category_paths", []),
        "dimension_names": [d.get("obj_nm") for d in record.get("dimensions", []) if d.get("obj_nm")],
        "units": record.get("units", []),
        "period_types": record.get("period_types", []),
        "latest_period": record.get("latest_period"),
        # version_status: 작성중지(MT_STOP_TITLE) 크롤은 아직 안 함 → 지금은 unknown.
        "version_status": record.get("version_status", "unknown"),
        "meta_status": record.get("meta_status", "missing"),
    }


def point_id(table_key: str) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, table_key))


# ------------------------------------------------------------------------ main

def ensure_collection(client: QdrantClient, recreate: bool) -> None:
    exists = client.collection_exists(COLLECTION)
    if exists and recreate:
        client.delete_collection(COLLECTION)
        exists = False
    if not exists:
        client.create_collection(
            COLLECTION,
            vectors_config={DENSE_NAME: models.VectorParams(size=DENSE_DIM, distance=models.Distance.COSINE)},
            sparse_vectors_config={SPARSE_NAME: models.SparseVectorParams()},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="KOSIS 카탈로그(enriched) → Qdrant 색인 (T2-2)")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--qdrant-path", type=Path, default=DEFAULT_QDRANT_PATH)
    parser.add_argument("--bm25-model", type=Path, default=DEFAULT_BM25_MODEL)
    parser.add_argument("--embed-cache", type=Path, default=DEFAULT_EMBED_CACHE)
    parser.add_argument("--recreate", action="store_true", help="컬렉션을 지우고 처음부터 색인")
    parser.add_argument("--batch", type=int, default=128, help="upsert 배치 크기")
    parser.add_argument("--limit", type=int, default=None, help="앞에서 N개만 (테스트용)")
    args = parser.parse_args()

    catalog = [json.loads(l) for l in args.catalog.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit is not None:
        catalog = catalog[: args.limit]
    print(f"[LOAD] 카탈로그 {len(catalog):,}건 | {args.catalog}", flush=True)

    started = time.time()

    # 1) BM25 모델 (코퍼스 전체 기준으로 계산 후 저장)
    model = build_bm25_model(catalog)
    args.bm25_model.parent.mkdir(parents=True, exist_ok=True)
    args.bm25_model.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
    print(f"[BM25] 어휘 {len(model['vocab']):,} | 평균문서길이 {model['avgdl']:.1f} | {args.bm25_model.name}", flush=True)

    # 2) Qdrant 컬렉션 준비
    # 로컬 임베디드 모드에서 delete_collection이 저장소를 완전히 비우지 않는 경우가 있어,
    # --recreate는 저장 폴더 자체를 지워 확실히 처음부터 시작한다.
    if args.recreate and args.qdrant_path.exists():
        import shutil
        shutil.rmtree(args.qdrant_path)
    client = QdrantClient(path=str(args.qdrant_path))
    try:
        ensure_collection(client, args.recreate)

        # 3) dense 임베딩(캐시) + sparse 인코딩 → upsert
        cache = load_embed_cache(args.embed_cache)
        stats = {"embed_api_calls": 0, "embed_cache_hits": 0}
        args.embed_cache.parent.mkdir(parents=True, exist_ok=True)

        with args.embed_cache.open("a", encoding="utf-8") as cache_handle:
            batch: list[models.PointStruct] = []
            done = 0
            for record in catalog:
                dense = get_dense_vector(record, cache, cache_handle, stats)
                sparse = encode_doc_sparse(record.get("doc_item_index", ""), model)
                batch.append(models.PointStruct(
                    id=point_id(record["table_key"]),
                    vector={DENSE_NAME: dense, SPARSE_NAME: sparse},
                    payload=build_payload(record),
                ))
                if len(batch) >= args.batch:
                    client.upsert(COLLECTION, points=batch)
                    done += len(batch)
                    batch = []
                    print(f"[UPSERT] {done:,}/{len(catalog):,} | 임베딩 API {stats['embed_api_calls']:,} "
                          f"캐시 {stats['embed_cache_hits']:,}", flush=True)
            if batch:
                client.upsert(COLLECTION, points=batch)
                done += len(batch)

        # 4) 검증
        count = client.count(COLLECTION).count
        elapsed = time.time() - started
        unique_keys = len({r["table_key"] for r in catalog})
        print("\n=== 색인 완료 ===", flush=True)
        print(f"  컬렉션 카운트   : {count:,}", flush=True)
        print(f"  유니크 표 수    : {unique_keys:,}  ({'일치 ✅' if count == unique_keys else '불일치 ⚠️'})", flush=True)
        print(f"  임베딩 API 호출 : {stats['embed_api_calls']:,}  (캐시 적중 {stats['embed_cache_hits']:,})", flush=True)
        print(f"  소요 시간       : {elapsed:.1f}초", flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
