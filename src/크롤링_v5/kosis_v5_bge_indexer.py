"""BGE-M3 벡터(dense+sparse, title/meta/item) + catalog_v5 rich payload → Qdrant 색인.

무엇을 만드는가
--------------
유안 structured retrieval engine 용 Qdrant 컬렉션 `kosis_tables_v5`. 각 포인트는
  · named dense  : title_dense / meta_dense / item_dense  (각 1024, Cosine)
  · named sparse : title_sparse / meta_sparse / item_sparse (BGE-M3 lexical)
  · rich payload : stat_id·기관·분류경로·items(id/nm)·dimensions(축·값 id/nm/up_id)·
                   period_types·units·표메타 + 필터용 평탄배열(item_names/dim_value_names 등)
payload 인덱스(keyword)를 걸어 `stat_id==`, `period_types==`, `units==`,
`item_names contains`, `dim_value_names contains` 같은 구조적 필터를 빠르게 건다.

입력
----
  · BGE 번들   : bge_encoding/encoded/{title,meta,item}/shard_*.{dense.npy,sparse.json,rows.json}
                 (rows.json[i] == 그 행의 table_key. dense.npy=(n,1024) float32. sparse=list[{tok:w}])
  · payload    : data/크롤링_v5/kosis_catalog_v5_260814.jsonl (차원값 코드 포함본)

3-pass (메모리 안전 — 벡터를 전량 RAM에 올리지 않는다)
----------------------------------------------------
  [1] meta 스파인: meta shard 를 순서대로 읽어 포인트 생성(id+meta벡터+payload). meta 는 전량(265,094).
  [2] title      : update_vectors 로 기존 포인트에 title 벡터만 추가(있는 것만 — 90.5%).
  [3] item       : update_vectors 로 item 벡터 추가. chunk 분할된 table_key 는 첫 chunk 채택.

필드별 커버리지가 다르다(title 240,000 / meta 265,094 / item 265,056). Qdrant 는 포인트별
named vector 부분 저장을 허용하므로, title 없는 표는 meta/item 검색엔 정상 참여하고 title
벡터 검색에서만 빠진다. 커버리지는 index_summary.json 에 기록한다.

사용 예 (레포 루트에서):
    # 스모크 (meta 앞 2000건만)
    venv/Scripts/python.exe src/크롤링_v5/kosis_v5_bge_indexer.py --recreate --limit 2000
    # 전량 → 임베디드 로컬(실험용)
    venv/Scripts/python.exe src/크롤링_v5/kosis_v5_bge_indexer.py --recreate
    # 도커 서버로
    venv/Scripts/python.exe src/크롤링_v5/kosis_v5_bge_indexer.py --recreate --qdrant-url http://127.0.0.1:6333
"""
import argparse
import glob
import json
import sys
import time
import uuid
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_BUNDLE = ROOT / "bge_encoding" / "encoded"
DEFAULT_CATALOG = ROOT / "data" / "크롤링_v5" / "kosis_catalog_v5_260814.jsonl"
DEFAULT_DB = ROOT / "kosis_qdrant_v5"
DEFAULT_SUMMARY = ROOT / "data" / "크롤링_v5" / "kosis_qdrant_v5_index_summary.json"

COLLECTION = "kosis_tables_v5"
POINT_NAMESPACE = uuid.UUID("f59d8a57-6667-4fa0-a60e-31f7b8679942")  # v2 인덱서와 동일 → id 호환
FIELDS = ("title", "meta", "item")
DENSE = {f: f"{f}_dense" for f in FIELDS}
SPARSE = {f: f"{f}_sparse" for f in FIELDS}
DIM = 1024
BATCH = 256

# payload keyword 인덱스 대상 (구조적 필터에 자주 쓰는 필드)
KEYWORD_INDEX_FIELDS = (
    "stat_id", "org_id", "primary_view", "view_codes", "catalog_version",
    "period_types", "units", "item_names", "dim_axis_names", "dim_value_names",
    "rec_tbl_se", "status",
)


def point_id(table_key: str) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, table_key))


def build_payload(row: dict) -> dict:
    """catalog_v5 레코드 → Qdrant payload. 임베딩 원문(doc_*)은 제외, 필터용 평탄배열 추가."""
    dims = row.get("dimensions") or []
    items = row.get("items") or []
    dim_axis_names = [d.get("obj_nm") for d in dims if d.get("obj_nm")]
    dim_value_names = [v.get("nm") for d in dims for v in (d.get("values") or []) if v.get("nm")]
    item_names = [i.get("itm_nm") for i in items if i.get("itm_nm")]
    return {
        # 식별 / 1:1 연결
        "table_key": row["table_key"],
        "catalog_version": row.get("catalog_version"),
        "stat_id": row.get("stat_id"),
        "org_id": row.get("org_id"),
        "org_name": row.get("org_name"),
        "tbl_id": row.get("tbl_id"),
        "tbl_name": row.get("tbl_name"),
        # 분류 경로
        "primary_view": row.get("primary_view"),
        "primary_path": row.get("primary_path"),
        "view_codes": row.get("view_codes") or [],
        "category_paths": row.get("category_paths") or {},
        # 구조체 (유안: itm_id/nm, 축 id/nm, 차원값 id/nm/up_id)
        "items": items,
        "dimensions": dims,
        # 주기·단위·표 메타
        "period_types": row.get("period_types") or [],
        "latest_period": row.get("latest_period"),
        "units": row.get("units") or [],
        "send_de": row.get("send_de"),
        "rec_tbl_se": row.get("rec_tbl_se"),
        "status": row.get("status", "active"),
        # 필터용 평탄배열 (payload index 대상)
        "item_names": item_names,
        "dim_axis_names": dim_axis_names,
        "dim_value_names": dim_value_names,
    }


def shard_bases(bundle: Path, field: str) -> list[str]:
    files = sorted(glob.glob(str(bundle / field / "shard_*.rows.json")))
    return [f[: -len(".rows.json")] for f in files]


def load_shard(base: str):
    rows = json.loads(Path(base + ".rows.json").read_text(encoding="utf-8"))
    dense = np.load(base + ".dense.npy")
    sparse = json.loads(Path(base + ".sparse.json").read_text(encoding="utf-8"))
    return rows, dense, sparse


def to_sparse(models, d: dict):
    return models.SparseVector(
        indices=[int(k) for k in d.keys()],
        values=[float(v) for v in d.values()],
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="BGE-M3 + catalog_v5 → Qdrant 색인기 (v5)")
    ap.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    ap.add_argument("--db-path", type=Path, default=DEFAULT_DB, help="임베디드 로컬 Qdrant 폴더")
    ap.add_argument("--qdrant-url", type=str, default=None, help="지정 시 도커/원격 서버 사용")
    ap.add_argument("--collection", type=str, default=COLLECTION)
    ap.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    ap.add_argument("--recreate", action="store_true", help="기존 컬렉션 삭제 후 재생성")
    ap.add_argument("--batch-size", type=int, default=BATCH)
    ap.add_argument("--limit", type=int, default=None, help="meta 스파인 앞 N건만 (스모크)")
    args = ap.parse_args()

    from qdrant_client import QdrantClient, models

    # ── payload: BGE 코퍼스(meta table_key)만 카탈로그에서 로드 ──────────────────
    meta_bases = shard_bases(args.bundle, "meta")
    if not meta_bases:
        raise SystemExit(f"[ERROR] meta shard 가 없습니다: {args.bundle/'meta'}")
    bge_keys: set[str] = set()
    for b in meta_bases:
        bge_keys.update(json.loads(Path(b + ".rows.json").read_text(encoding="utf-8")))
    print(f"[LOAD] BGE meta table_key {len(bge_keys):,}건", flush=True)

    payloads: dict[str, dict] = {}
    with args.catalog.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            tk = row.get("table_key")
            if tk in bge_keys:
                payloads[tk] = build_payload(row)
    print(f"[LOAD] catalog payload {len(payloads):,}건 (BGE∩catalog)", flush=True)
    payload_missing = len(bge_keys - payloads.keys())
    if payload_missing:
        print(f"[WARN] payload 없는 BGE table_key {payload_missing}건 — 색인 제외", flush=True)

    # ── 클라이언트 / 컬렉션 ─────────────────────────────────────────────────────
    client = QdrantClient(url=args.qdrant_url) if args.qdrant_url else QdrantClient(path=str(args.db_path))
    existing = {c.name for c in client.get_collections().collections}
    if args.recreate and args.collection in existing:
        client.delete_collection(args.collection)
        existing.discard(args.collection)
    if args.collection not in existing:
        client.create_collection(
            collection_name=args.collection,
            vectors_config={DENSE[f]: models.VectorParams(size=DIM, distance=models.Distance.COSINE) for f in FIELDS},
            sparse_vectors_config={SPARSE[f]: models.SparseVectorParams() for f in FIELDS},
        )
        print(f"[INIT] 컬렉션 생성: {args.collection} (dense×3 + sparse×3)", flush=True)

    coverage = {f: 0 for f in FIELDS}
    t0 = time.time()

    # ── [1] meta 스파인: 포인트 생성 + payload ──────────────────────────────────
    created_keys: set[str] = set()
    batch: list = []
    stop = False
    for base in meta_bases:
        if stop:
            break
        rows, dense, sparse = load_shard(base)
        for i, tk in enumerate(rows):
            pl = payloads.get(tk)
            if pl is None:
                continue
            batch.append(models.PointStruct(
                id=point_id(tk),
                vector={DENSE["meta"]: dense[i].tolist(), SPARSE["meta"]: to_sparse(models, sparse[i])},
                payload=pl,
            ))
            created_keys.add(tk)
            if len(batch) >= args.batch_size:
                client.upsert(collection_name=args.collection, points=batch, wait=True)
                batch = []
            if args.limit is not None and len(created_keys) >= args.limit:
                stop = True
                break
        print(f"  [meta] {Path(base).name}: 누적 포인트 {len(created_keys):,}", flush=True)
    if batch:
        client.upsert(collection_name=args.collection, points=batch, wait=True)
        batch = []
    coverage["meta"] = len(created_keys)
    print(f"[1/3] meta 포인트 {len(created_keys):,} 생성 ({time.time()-t0:.0f}s)", flush=True)

    # ── [2],[3] title / item: 기존 포인트(created_keys)에만 벡터 추가 ────────────
    for field in ("title", "item"):
        seen: set[str] = set()
        batch = []
        for base in shard_bases(args.bundle, field):
            rows, dense, sparse = load_shard(base)
            for i, tk in enumerate(rows):
                if tk not in created_keys or tk in seen:  # 미생성 포인트/재chunk 제외
                    continue
                seen.add(tk)
                batch.append(models.PointVectors(
                    id=point_id(tk),
                    vector={DENSE[field]: dense[i].tolist(), SPARSE[field]: to_sparse(models, sparse[i])},
                ))
                if len(batch) >= args.batch_size:
                    client.update_vectors(collection_name=args.collection, points=batch, wait=True)
                    batch = []
            print(f"  [{field}] {Path(base).name}: 누적 {len(seen):,}", flush=True)
        if batch:
            client.update_vectors(collection_name=args.collection, points=batch, wait=True)
        coverage[field] = len(seen)
        print(f"[{'2' if field=='title' else '3'}/3] {field} 벡터 {len(seen):,} 추가 ({time.time()-t0:.0f}s)", flush=True)

    # ── payload 인덱스 ──────────────────────────────────────────────────────────
    for fld in KEYWORD_INDEX_FIELDS:
        try:
            client.create_payload_index(collection_name=args.collection, field_name=fld,
                                        field_schema=models.PayloadSchemaType.KEYWORD)
        except Exception as e:  # noqa: BLE001
            print(f"  [IDX-WARN] {fld}: {e}", flush=True)
    print(f"[IDX] payload keyword 인덱스 {len(KEYWORD_INDEX_FIELDS)}개 생성", flush=True)

    point_count = int(client.count(args.collection, exact=True).count)
    client.close()

    summary = {
        "collection": args.collection,
        "target": args.qdrant_url or str(args.db_path),
        "points": point_count,
        "bundle": str(args.bundle),
        "catalog": args.catalog.name,
        "vectors": {"dense": list(DENSE.values()), "sparse": list(SPARSE.values()), "dim": DIM},
        "field_coverage": coverage,
        "payload_indexes": list(KEYWORD_INDEX_FIELDS),
        "payload_missing_bge_keys": payload_missing,
        "elapsed_sec": round(time.time() - t0, 1),
        "notes": {
            "title_gap": "title shard 0012·0013 미인코딩 → 25,094건 title 벡터 없음(GPU 재인코딩 필요)",
            "item_chunk": "chunk 분할된 table_key 는 첫 chunk 벡터 채택",
            "named_vector_partial": "title/item 없는 포인트도 meta 검색엔 정상 참여",
        },
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
