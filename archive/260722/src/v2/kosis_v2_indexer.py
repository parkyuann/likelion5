"""Index KOSIS Catalog v2 into an isolated local Qdrant collection.

Created: 2026-07-22
Catalog: data/kosis_catalog_v2.jsonl
Vectors:
  * doc_meta_vector <- doc_meta_text
  * tbl_name_vector <- tbl_name
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[4]
ARCHIVE_ROOT = PROJECT_ROOT / "archive" / "260722"
DEFAULT_INPUT = PROJECT_ROOT / "data" / "kosis_catalog_v2.jsonl"
DEFAULT_DB = PROJECT_ROOT / "output" / "kosis_qdrant_v2"
DEFAULT_OUTPUT = ARCHIVE_ROOT / "outputs" / "hybrid_v2_20260722"
DEFAULT_CACHE = DEFAULT_OUTPUT / "embed_cache.jsonl"
DEFAULT_SEED_CACHE = PROJECT_ROOT / "output" / "kosis_index" / "embed_cache_v2.jsonl"
COLLECTION = "kosis_tables_v2"
DOC_META_VECTOR = "doc_meta_vector"
TBL_NAME_VECTOR = "tbl_name_vector"
EMBEDDING_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2"
EMBEDDING_CACHE_VERSION = "clova-embedding-v2|kosis-two-vector-v1"
POINT_NAMESPACE = uuid.UUID("f59d8a57-6667-4fa0-a60e-31f7b8679942")


def load_api_key() -> str:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    key = (os.getenv("HCX_API_KEY") or os.getenv("NCP_CLOVASTUDIO_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("HCX_API_KEY 또는 NCP_CLOVASTUDIO_API_KEY가 필요합니다")
    return key


def embed_api(text: str) -> list[float]:
    import requests

    response = requests.post(
        EMBEDDING_URL,
        headers={"Authorization": f"Bearer {load_api_key()}", "Content-Type": "application/json"},
        json={"text": text},
        timeout=30,
    )
    data = response.json()
    vector = data.get("result", {}).get("embedding")
    if response.status_code != 200 or not isinstance(vector, list) or not vector:
        raise RuntimeError(f"Embedding API 오류 (status={response.status_code}): {data.get('status')}")
    return [float(value) for value in vector]


class EmbedCache:
    def __init__(self, path: Path, seed_path: Path | None = None):
        self.path = path
        self.local: dict[str, list[float]] = {}
        self.seed: dict[str, list[float]] = {}
        self.hits = 0
        self.misses = 0
        self.api_calls = 0
        self._load(path, self.local)
        if seed_path and seed_path != path:
            self._load(seed_path, self.seed)

    @staticmethod
    def _load(path: Path, target: dict[str, list[float]]) -> None:
        if not path.exists():
            return
        with path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                key, vector = row.get("key"), row.get("vector")
                if isinstance(key, str) and isinstance(vector, list) and vector:
                    target[key] = [float(value) for value in vector]

    @staticmethod
    def key(text: str) -> str:
        source = f"{EMBEDDING_CACHE_VERSION}|{text}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def _append(self, key: str, vector: list[float]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"key": key, "vector": vector}) + "\n")
        self.local[key] = vector

    def embed(self, text: str) -> list[float]:
        normalized = str(text or "").strip()
        if not normalized:
            raise ValueError("embedding text must not be empty")
        key = self.key(normalized)
        if key in self.local:
            self.hits += 1
            return self.local[key]
        if key in self.seed:
            self.hits += 1
            vector = self.seed[key]
            self._append(key, vector)
            return vector
        self.misses += 1
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                vector = embed_api(normalized)
                self.api_calls += 1
                self._append(key, vector)
                return vector
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        assert last_error is not None
        raise last_error


def load_records(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            table_key = str(row.get("table_key") or "").strip()
            doc_meta_text = str(row.get("doc_meta_text") or "").strip()
            tbl_name = str(row.get("tbl_name") or "").strip()
            if not table_key or not doc_meta_text or not tbl_name:
                raise ValueError(f"line {line_number}: table_key/doc_meta_text/tbl_name 누락")
            if table_key in seen:
                raise ValueError(f"line {line_number}: duplicate table_key={table_key}")
            seen.add(table_key)
            records.append(row)
            if limit is not None and len(records) >= limit:
                break
    return records


def point_id(table_key: str) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, table_key))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--collection", default=COLLECTION)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--seed-cache", type=Path, default=DEFAULT_SEED_CACHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--local", action="store_true", help="호환성 옵션; v2는 전용 local DB 사용")
    parser.add_argument("--recreate", action="store_true", help="기존 v2 collection 삭제 후 재생성")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.batch_size < 1:
        raise ValueError("batch-size must be >= 1")
    records = load_records(args.input, args.limit)
    if not records:
        raise ValueError("no valid v2 records")
    if args.dry_run:
        print(json.dumps({
            "catalog_version": "kosis-catalog-v2",
            "run_date": "2026-07-22",
            "records": len(records),
            "missing_doc_meta_text": 0,
            "missing_tbl_name": 0,
        }, ensure_ascii=False, indent=2))
        return

    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    started = time.perf_counter()
    cache = EmbedCache(args.cache, args.seed_cache)
    client = QdrantClient(path=str(args.db_path))
    indexed = 0
    try:
        existing = {item.name for item in client.get_collections().collections}
        if args.recreate and args.collection in existing:
            client.delete_collection(args.collection)
            existing.remove(args.collection)

        first_meta = cache.embed(str(records[0]["doc_meta_text"]))
        first_name = cache.embed(str(records[0]["tbl_name"]))
        if len(first_meta) != len(first_name):
            raise ValueError("vector dimension mismatch")
        dimension = len(first_meta)
        if args.collection not in existing:
            client.create_collection(
                collection_name=args.collection,
                vectors_config={
                    DOC_META_VECTOR: VectorParams(size=dimension, distance=Distance.COSINE),
                    TBL_NAME_VECTOR: VectorParams(size=dimension, distance=Distance.COSINE),
                },
            )

        for start in range(0, len(records), args.batch_size):
            batch = records[start : start + args.batch_size]
            points = []
            for record in batch:
                meta_vector = cache.embed(str(record["doc_meta_text"]))
                name_vector = cache.embed(str(record["tbl_name"]))
                points.append(PointStruct(
                    id=point_id(str(record["table_key"])),
                    vector={DOC_META_VECTOR: meta_vector, TBL_NAME_VECTOR: name_vector},
                    payload={
                        "table_key": record["table_key"],
                        "org_id": record.get("org_id"),
                        "org_name": record.get("org_name"),
                        "tbl_id": record.get("tbl_id"),
                        "tbl_name": record["tbl_name"],
                        "doc_meta_text": record["doc_meta_text"],
                        "catalog_version": "kosis-catalog-v2",
                    },
                ))
            client.upsert(collection_name=args.collection, points=points, wait=True)
            indexed += len(points)
            print(f"indexed={indexed}/{len(records)} api_calls={cache.api_calls} cache_hits={cache.hits}", flush=True)

        point_count = int(client.count(args.collection, exact=True).count)
    finally:
        client.close()

    summary = {
        "run_date": "2026-07-22",
        "catalog_version": "kosis-catalog-v2",
        "input": str(args.input.resolve()),
        "input_records": len(records),
        "collection": args.collection,
        "db_path": str(args.db_path.resolve()),
        "qdrant_points": point_count,
        "vectors": {
            DOC_META_VECTOR: "doc_meta_text",
            TBL_NAME_VECTOR: "tbl_name",
            "dimension": dimension,
        },
        "cache_hits": cache.hits,
        "cache_misses": cache.misses,
        "embedding_api_calls": cache.api_calls,
        "elapsed_sec": round(time.perf_counter() - started, 3),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "index_summary.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
