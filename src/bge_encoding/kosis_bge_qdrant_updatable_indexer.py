"""Version-independent BGE-M3 + Qdrant index/update runner for KOSIS catalogs.

This program creates a structured-retrieval collection with three named BGE-M3
vector pairs (dense+sparse): ``title``, ``meta`` and ``item``.  The point
payload keeps the KOSIS identifiers, category paths, items, dimension axes and
dimension values needed for Qdrant payload filtering.

It is deliberately separate from ``bge_encoding/bge_m3_encode.py``.  The
original script encodes an entire, fixed ordered corpus.  Reusing its
``--resume`` option after a catalog changes is unsafe because the old shard
position can then point at another table.  This script reuses a vector only
when both ``table_key`` and the field's SHA-256 text hash match.

First migration from the legacy v4 BGE bundle:

    python src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py ^
      --catalog data/.../kosis_catalog_v5_260814.jsonl ^
      --reuse-bundle src/bge_encoding/encoded ^
      --reuse-documents src/bge_encoding/documents.jsonl ^
      --output-bundle src/bge_encoding/encoded_260814 ^
      --db-path src/bge_encoding/qdrant_structured ^
      --collection kosis_tables_structured --delete-stale

For the next catalog, pass the previous ``--output-bundle`` as
``--reuse-bundle``.  It includes its own ``documents.jsonl`` state, so no
``--reuse-documents`` option is then needed.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import numpy as np


# This runner lives in src/bge_encoding; repository root is three levels up.
ROOT = Path(__file__).resolve().parent.parent.parent
MODEL = "BAAI/bge-m3"
REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
DIM = 1024
MAX_LENGTH = 512
DEFAULT_SHARD_SIZE = 20_000
DEFAULT_BATCH_SIZE = 16
DEFAULT_QDRANT_BATCH = 256
FIELDS = ("title", "meta", "item")
DENSE = {field: f"{field}_dense" for field in FIELDS}
SPARSE = {field: f"{field}_sparse" for field in FIELDS}
POINT_NAMESPACE = uuid.UUID("f59d8a57-6667-4fa0-a60e-31f7b8679942")

# Keyword indexes are deliberately on the flat fields.  The full nested
# objects below remain in the payload for the final structured lookup.
KEYWORD_INDEX_FIELDS = (
    "stat_id",
    "org_id",
    "primary_view",
    "view_codes",
    "catalog_version",
    "period_types",
    "units",
    "item_ids",
    "item_names",
    "dim_axis_ids",
    "dim_axis_names",
    "dim_value_ids",
    "dim_value_names",
    "rec_tbl_se",
    "status",
)


@dataclass(frozen=True)
class DocumentState:
    table_key: str
    texts: dict[str, str]
    text_hashes: dict[str, str]
    payload_hash: str | None


def point_id(table_key: str) -> str:
    """Stable UUID compatible with the earlier v5 Qdrant indexer."""
    return str(uuid.uuid5(POINT_NAMESPACE, table_key))


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ordered_unique(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def document_texts(row: dict[str, Any]) -> dict[str, str]:
    """Keep the exact BGE contract used by bge_m3_encode.py."""
    items = row.get("items") or []
    item_text = " ".join(ordered_unique(item.get("itm_nm") for item in items if isinstance(item, dict)))
    return {
        "title": str(row.get("tbl_name") or ""),
        "meta": str(row.get("doc_meta_text") or ""),
        "item": item_text,
    }


def build_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Payload required for semantic search combined with structural filters."""
    dimensions = [d for d in (row.get("dimensions") or []) if isinstance(d, dict)]
    items = [i for i in (row.get("items") or []) if isinstance(i, dict)]
    dim_values = [
        value
        for dim in dimensions
        for value in (dim.get("values") or [])
        if isinstance(value, dict)
    ]
    return {
        # Stable 1:1 catalog link and table identifiers
        "table_key": row.get("table_key"),
        "catalog_version": row.get("catalog_version"),
        "stat_id": row.get("stat_id"),
        "org_id": row.get("org_id"),
        "org_name": row.get("org_name"),
        "tbl_id": row.get("tbl_id"),
        "tbl_name": row.get("tbl_name"),
        # Classification / source context
        "primary_view": row.get("primary_view"),
        "primary_path": row.get("primary_path"),
        "view_codes": row.get("view_codes") or [],
        "category_paths": row.get("category_paths") or {},
        # Structured retrieval source data
        "items": items,
        "dimensions": dimensions,
        "period_types": row.get("period_types") or [],
        "latest_period": row.get("latest_period"),
        "units": row.get("units") or [],
        "send_de": row.get("send_de"),
        "rec_tbl_se": row.get("rec_tbl_se"),
        "status": row.get("status", "active"),
        # Original catalog texts: no change to their construction.
        "doc_meta_text": row.get("doc_meta_text"),
        "doc_item_index": row.get("doc_item_index"),
        # Flat keyword-filter fields; nested data above remains authoritative.
        "item_ids": ordered_unique(item.get("itm_id") for item in items),
        "item_names": ordered_unique(item.get("itm_nm") for item in items),
        "dim_axis_ids": ordered_unique(dim.get("obj_id") for dim in dimensions),
        "dim_axis_names": ordered_unique(dim.get("obj_nm") for dim in dimensions),
        "dim_value_ids": ordered_unique(value.get("id") for value in dim_values),
        "dim_value_names": ordered_unique(value.get("nm") for value in dim_values),
    }


def iter_jsonl(path: Path, limit: int | None = None) -> Iterator[dict[str, Any]]:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL: {path} line {line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise SystemExit(f"Invalid JSONL object: {path} line {line_number}")
            yield value
            count += 1
            if limit is not None and count >= limit:
                return


def load_catalog_state(path: Path, limit: int | None) -> dict[str, DocumentState]:
    if not path.is_file():
        raise SystemExit(f"Catalog not found: {path}")
    states: dict[str, DocumentState] = {}
    duplicate_keys = 0
    for row in iter_jsonl(path, limit):
        table_key = str(row.get("table_key") or "").strip()
        if not table_key:
            continue
        if table_key in states:
            duplicate_keys += 1
        texts = document_texts(row)
        states[table_key] = DocumentState(
            table_key=table_key,
            texts=texts,
            text_hashes={field: text_hash(texts[field]) for field in FIELDS},
            payload_hash=json_hash(build_payload(row)),
        )
    if not states:
        raise SystemExit(f"No usable catalog records found: {path}")
    print(f"[LOAD] catalog: {len(states):,} unique table_key")
    if duplicate_keys:
        print(f"[WARN] duplicate table_key in catalog; last row used: {duplicate_keys:,}")
    return states


def resolve_reuse_documents(bundle: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    candidate = bundle / "documents.jsonl"
    return candidate if candidate.is_file() else None


def load_reuse_state(documents_path: Path | None) -> dict[str, DocumentState]:
    """Load a previous compact state or legacy bge_encoding/documents.jsonl."""
    if documents_path is None:
        return {}
    if not documents_path.is_file():
        raise SystemExit(f"Reuse documents not found: {documents_path}")
    states: dict[str, DocumentState] = {}
    for row in iter_jsonl(documents_path):
        table_key = str(row.get("table_key") or "").strip()
        if not table_key:
            continue
        texts = {
            "title": str(row.get("title") or ""),
            "meta": str(row.get("meta") or ""),
            "item": str(row.get("item") or ""),
        }
        supplied_hashes = row.get("text_hashes")
        hashes = (
            {field: str(supplied_hashes.get(field) or text_hash(texts[field])) for field in FIELDS}
            if isinstance(supplied_hashes, dict)
            else {field: text_hash(texts[field]) for field in FIELDS}
        )
        states[table_key] = DocumentState(
            table_key=table_key,
            texts=texts,
            text_hashes=hashes,
            payload_hash=str(row["payload_hash"]) if row.get("payload_hash") else None,
        )
    print(f"[LOAD] reuse documents: {len(states):,} unique table_key")
    return states


def shard_bases(bundle: Path, field: str) -> list[Path]:
    pattern = str(bundle / field / "shard_*.rows.json")
    return [Path(path[: -len(".rows.json")]) for path in sorted(glob.glob(pattern))]


def load_vector_shard(base: Path) -> tuple[list[str], np.ndarray, list[dict[str, float]]]:
    rows_path = Path(str(base) + ".rows.json")
    dense_path = Path(str(base) + ".dense.npy")
    sparse_path = Path(str(base) + ".sparse.json")
    if not dense_path.is_file() or not sparse_path.is_file():
        raise SystemExit(f"Incomplete vector shard: {base}")
    rows_raw = json.loads(rows_path.read_text(encoding="utf-8"))
    rows = [str(value.get("table_key") if isinstance(value, dict) else value) for value in rows_raw]
    dense = np.load(dense_path)
    sparse_raw = json.loads(sparse_path.read_text(encoding="utf-8"))
    if len(rows) != len(dense) or len(rows) != len(sparse_raw):
        raise SystemExit(
            f"Shard length mismatch: {base} rows={len(rows)} dense={len(dense)} sparse={len(sparse_raw)}"
        )
    if dense.ndim != 2 or dense.shape[1] != DIM:
        raise SystemExit(f"Unexpected dense shape in {dense_path}: {dense.shape}; expected (*, {DIM})")
    sparse: list[dict[str, float]] = []
    for item in sparse_raw:
        if not isinstance(item, dict):
            raise SystemExit(f"Invalid sparse vector in {sparse_path}")
        sparse.append({str(key): float(value) for key, value in item.items()})
    return rows, np.asarray(dense, dtype=np.float32), sparse


def chunk_text(text: str, tokenizer: Any) -> list[str]:
    ids = tokenizer.encode(text, add_special_tokens=False)
    if len(ids) <= MAX_LENGTH - 2:
        return [text]
    return [
        tokenizer.decode(ids[start : start + MAX_LENGTH - 2], skip_special_tokens=True)
        for start in range(0, len(ids), MAX_LENGTH - 2)
    ]


class BGEEncoder:
    def __init__(self, fp16: bool) -> None:
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            raise SystemExit(
                "FlagEmbedding is required when new/changed texts need encoding. "
                "Install it on the GPU computer before running this script."
            ) from exc
        print(f"[MODEL] loading {MODEL} @ {REVISION} (fp16={fp16})")
        self.model = BGEM3FlagModel(MODEL, revision=REVISION, use_fp16=fp16)

    @staticmethod
    def _sparse_json(weights: dict[Any, Any]) -> dict[str, float]:
        return {str(int(key)): float(value) for key, value in weights.items()}

    def encode(self, texts: list[str], field: str, batch_size: int) -> list[tuple[np.ndarray, dict[str, float]]]:
        """Encode one vector per table.  Item text keeps the legacy first-chunk policy."""
        expanded: list[str] = []
        owners: list[int] = []
        for owner, text in enumerate(texts):
            chunks = chunk_text(text, self.model.tokenizer) if field == "item" else [text]
            for chunk in chunks:
                if chunk.strip():
                    expanded.append(chunk)
                    owners.append(owner)

        result: list[tuple[np.ndarray, dict[str, float]] | None] = [None] * len(texts)
        for start in range(0, len(expanded), batch_size):
            batch = expanded[start : start + batch_size]
            encoded = self.model.encode(
                batch,
                batch_size=batch_size,
                max_length=MAX_LENGTH,
                return_dense=True,
                return_sparse=True,
            )
            dense = np.asarray(encoded["dense_vecs"], dtype=np.float32)
            norms = np.linalg.norm(dense, axis=1, keepdims=True)
            dense = np.divide(dense, norms, out=np.zeros_like(dense), where=norms != 0)
            sparse = [self._sparse_json(value) for value in encoded["lexical_weights"]]
            for offset, vector in enumerate(dense):
                owner = owners[start + offset]
                # The earlier item indexer used the first emitted chunk for a table.
                if result[owner] is None:
                    result[owner] = (vector, sparse[offset])
        return [value if value is not None else (np.zeros(DIM, dtype=np.float32), {}) for value in result]


class BundleWriter:
    """Write a complete reusable snapshot, regardless of reused/new vector origin."""
    def __init__(self, root: Path, shard_size: int) -> None:
        self.root = root
        self.shard_size = shard_size
        self.counts = {field: 0 for field in FIELDS}
        self._rows = {field: [] for field in FIELDS}
        self._hashes = {field: [] for field in FIELDS}
        self._dense = {field: [] for field in FIELDS}
        self._sparse = {field: [] for field in FIELDS}
        self._shard = {field: 0 for field in FIELDS}
        for field in FIELDS:
            (self.root / field).mkdir(parents=True, exist_ok=True)

    def add(self, field: str, table_key: str, field_hash: str, dense: np.ndarray, sparse: dict[str, float]) -> None:
        self._rows[field].append(table_key)
        self._hashes[field].append(field_hash)
        self._dense[field].append(np.asarray(dense, dtype=np.float32))
        self._sparse[field].append({str(key): float(value) for key, value in sparse.items()})
        self.counts[field] += 1
        if len(self._rows[field]) >= self.shard_size:
            self.flush(field)

    def flush(self, field: str) -> None:
        rows = self._rows[field]
        if not rows:
            return
        index = self._shard[field]
        base = self.root / field / f"shard_{index:04d}"
        np.save(Path(str(base) + ".dense.npy"), np.asarray(self._dense[field], dtype=np.float32))
        Path(str(base) + ".sparse.json").write_text(
            json.dumps(self._sparse[field], ensure_ascii=False), encoding="utf-8"
        )
        Path(str(base) + ".rows.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        Path(str(base) + ".hashes.json").write_text(json.dumps(self._hashes[field]), encoding="utf-8")
        Path(str(base) + ".complete.json").write_text(
            json.dumps({"rows": len(rows), "field": field, "shard": index}), encoding="utf-8"
        )
        self._rows[field].clear()
        self._hashes[field].clear()
        self._dense[field].clear()
        self._sparse[field].clear()
        self._shard[field] += 1

    def close(self) -> None:
        for field in FIELDS:
            self.flush(field)


class QdrantVectorSink:
    def __init__(self, client: Any, models: Any, collection: str, existing_ids: set[str], batch_size: int) -> None:
        self.client = client
        self.models = models
        self.collection = collection
        self.existing_ids = existing_ids
        self.batch_size = batch_size
        self.upserts: list[Any] = []
        self.updates: list[Any] = []
        self.uploaded = {field: 0 for field in FIELDS}

    def add(self, field: str, table_key: str, dense: np.ndarray, sparse: dict[str, float], needs_update: bool) -> None:
        if not needs_update:
            return
        pid = point_id(table_key)
        vector = {
            DENSE[field]: np.asarray(dense, dtype=np.float32).tolist(),
            SPARSE[field]: self.models.SparseVector(
                indices=[int(key) for key in sparse.keys()],
                values=[float(value) for value in sparse.values()],
            ),
        }
        if pid in self.existing_ids:
            self.updates.append(self.models.PointVectors(id=pid, vector=vector))
        else:
            self.upserts.append(self.models.PointStruct(id=pid, vector=vector))
            self.existing_ids.add(pid)
        self.uploaded[field] += 1
        if len(self.upserts) + len(self.updates) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if self.upserts:
            self.client.upsert(collection_name=self.collection, points=self.upserts, wait=True)
            self.upserts = []
        if self.updates:
            self.client.update_vectors(collection_name=self.collection, points=self.updates, wait=True)
            self.updates = []


def prepare_output(path: Path, overwrite: bool) -> None:
    if path.exists():
        entries = list(path.iterdir())
        if entries and not overwrite:
            raise SystemExit(
                f"Output bundle already exists and is not empty: {path}. "
                "Choose a new dated path or pass --overwrite-output."
            )
        if entries and overwrite:
            shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def open_qdrant(args: argparse.Namespace) -> tuple[Any, Any, set[str], bool]:
    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:
        raise SystemExit("qdrant-client is required. Install requirements.txt first.") from exc

    client = (
        QdrantClient(url=args.qdrant_url, api_key=args.qdrant_api_key)
        if args.qdrant_url
        else QdrantClient(path=str(args.db_path))
    )
    collections = {item.name for item in client.get_collections().collections}
    if args.recreate and args.collection in collections:
        client.delete_collection(args.collection)
        collections.remove(args.collection)
        print(f"[QDRANT] recreated collection: {args.collection}")
    created = args.collection not in collections
    if created:
        client.create_collection(
            collection_name=args.collection,
            vectors_config={
                DENSE[field]: models.VectorParams(size=DIM, distance=models.Distance.COSINE)
                for field in FIELDS
            },
            sparse_vectors_config={SPARSE[field]: models.SparseVectorParams() for field in FIELDS},
        )
        print(f"[QDRANT] created collection: {args.collection}")
        return client, models, set(), True

    # Fetch once so unchanged vectors can be left completely untouched.
    existing_ids: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=args.collection,
            limit=1_000,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        existing_ids.update(str(point.id) for point in points)
        if offset is None:
            break
    print(f"[QDRANT] existing points: {len(existing_ids):,}")
    return client, models, existing_ids, False


def field_is_same(old: DocumentState | None, current: DocumentState, field: str) -> bool:
    return old is not None and old.text_hashes.get(field) == current.text_hashes[field]


def process_field(
    *,
    field: str,
    current: dict[str, DocumentState],
    old: dict[str, DocumentState],
    reuse_bundle: Path | None,
    writer: BundleWriter,
    sink: QdrantVectorSink | None,
    encoder_holder: dict[str, BGEEncoder | None],
    args: argparse.Namespace,
) -> dict[str, int]:
    """Copy safe old vectors, then encode only rows absent from that safe copy."""
    emitted: set[str] = set()
    reused = 0
    encoded = 0

    def emit(table_key: str, dense: np.ndarray, sparse: dict[str, float], is_reused: bool) -> None:
        nonlocal reused, encoded
        record = current[table_key]
        writer.add(field, table_key, record.text_hashes[field], dense, sparse)
        if is_reused:
            reused += 1
        else:
            encoded += 1
        if sink is not None:
            same = field_is_same(old.get(table_key), record, field)
            sink.add(
                field,
                table_key,
                dense,
                sparse,
                # A newly encoded vector must be uploaded even when its text
                # happens to match legacy documents.  That case occurs when a
                # legacy field shard is incomplete (notably title in v4).
                needs_update=(
                    not is_reused or not same or point_id(table_key) not in sink.existing_ids
                ),
            )
        emitted.add(table_key)

    if reuse_bundle is not None:
        for base in shard_bases(reuse_bundle, field):
            rows, dense_vectors, sparse_vectors = load_vector_shard(base)
            for index, table_key in enumerate(rows):
                if table_key in emitted or table_key not in current:
                    continue
                if not field_is_same(old.get(table_key), current[table_key], field):
                    continue
                emit(table_key, dense_vectors[index], sparse_vectors[index], True)
            print(f"  [{field}] reused scan {base.name}: {reused:,}")

    missing = [table_key for table_key in current if table_key not in emitted]
    if missing and args.dry_run:
        encoded = len(missing)
        print(f"  [{field}] would encode {encoded:,} new/changed/missing vectors")
        return {"reused": reused, "encoded": encoded, "total": len(current)}

    for start in range(0, len(missing), args.encode_batch_size):
        table_keys = missing[start : start + args.encode_batch_size]
        if encoder_holder["encoder"] is None:
            encoder_holder["encoder"] = BGEEncoder(args.fp16)
        vectors = encoder_holder["encoder"].encode(
            [current[table_key].texts[field] for table_key in table_keys],
            field,
            args.encode_batch_size,
        )
        for table_key, (dense, sparse) in zip(table_keys, vectors):
            emit(table_key, dense, sparse, False)
        if start == 0 or (start // args.encode_batch_size + 1) % 50 == 0:
            print(f"  [{field}] encoded {min(start + len(table_keys), len(missing)):,}/{len(missing):,}")
    if sink is not None:
        sink.flush()
    print(f"[FIELD] {field}: reused={reused:,}, encoded={encoded:,}, total={len(current):,}")
    return {"reused": reused, "encoded": encoded, "total": len(current)}


def payload_needs_update(
    old: DocumentState | None,
    current: DocumentState,
    existing_ids_at_start: set[str],
    force: bool,
) -> bool:
    if force or point_id(current.table_key) not in existing_ids_at_start:
        return True
    # Legacy bge_encoding/documents.jsonl has no payload hash: refresh it once.
    return old is None or old.payload_hash != current.payload_hash


def flush_payload_batch(client: Any, models: Any, collection: str, entries: list[tuple[str, dict[str, Any]]]) -> None:
    if not entries:
        return
    try:
        operations = [
            models.SetPayloadOperation(
                set_payload=models.SetPayload(payload=payload, points=[point_id(table_key)])
            )
            for table_key, payload in entries
        ]
        client.batch_update_points(
            collection_name=collection,
            update_operations=operations,
            wait=True,
        )
    except Exception as exc:  # compatibility fallback for an older qdrant-client
        print(f"[WARN] batch payload update unavailable ({exc}); using single-point fallback")
        for table_key, payload in entries:
            client.set_payload(
                collection_name=collection,
                payload=payload,
                points=[point_id(table_key)],
                wait=True,
            )


def update_payloads(
    *,
    catalog: Path,
    current: dict[str, DocumentState],
    old: dict[str, DocumentState],
    existing_ids_at_start: set[str],
    client: Any,
    models: Any,
    args: argparse.Namespace,
) -> int:
    pending: list[tuple[str, dict[str, Any]]] = []
    updated = 0
    seen: set[str] = set()
    for row in iter_jsonl(catalog, args.limit):
        table_key = str(row.get("table_key") or "").strip()
        if table_key not in current or table_key in seen:
            continue
        seen.add(table_key)
        record = current[table_key]
        if not payload_needs_update(
            old.get(table_key), record, existing_ids_at_start, args.refresh_payload
        ):
            continue
        pending.append((table_key, build_payload(row)))
        updated += 1
        if len(pending) >= args.qdrant_batch_size:
            flush_payload_batch(client, models, args.collection, pending)
            pending = []
    flush_payload_batch(client, models, args.collection, pending)
    print(f"[PAYLOAD] updated: {updated:,}")
    return updated


def create_payload_indexes(client: Any, models: Any, collection: str) -> None:
    for field in KEYWORD_INDEX_FIELDS:
        try:
            client.create_payload_index(
                collection_name=collection,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
                wait=True,
            )
        except Exception as exc:  # Existing index / server difference is non-fatal.
            print(f"[INDEX-WARN] {field}: {exc}")
    print(f"[INDEX] ensured keyword indexes: {len(KEYWORD_INDEX_FIELDS)}")


def delete_stale_points(client: Any, models: Any, collection: str, stale_ids: set[str], batch_size: int) -> int:
    ids = sorted(stale_ids)
    for start in range(0, len(ids), batch_size):
        client.delete(
            collection_name=collection,
            points_selector=models.PointIdsList(points=ids[start : start + batch_size]),
            wait=True,
        )
    if ids:
        print(f"[DELETE] stale points deleted: {len(ids):,}")
    return len(ids)


def write_documents_state(path: Path, current: dict[str, DocumentState]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in current.values():
            handle.write(
                json.dumps(
                    {
                        "table_key": record.table_key,
                        "title": record.texts["title"],
                        "meta": record.texts["meta"],
                        "item": record.texts["item"],
                        "text_hashes": record.text_hashes,
                        "payload_hash": record.payload_hash,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely reuse BGE-M3 vectors and incrementally update a structured KOSIS Qdrant collection."
    )
    parser.add_argument("--catalog", type=Path, required=True, help="Newest catalog JSONL to index")
    parser.add_argument(
        "--reuse-bundle",
        type=Path,
        default=None,
        help="Previous vector bundle. Omit only for a full first-time encode.",
    )
    parser.add_argument(
        "--reuse-documents",
        type=Path,
        default=None,
        help="Legacy previous documents.jsonl; not needed if reuse bundle contains one.",
    )
    parser.add_argument(
        "--output-bundle",
        type=Path,
        required=True,
        help="New complete reusable vector bundle (use this as --reuse-bundle next time).",
    )
    parser.add_argument("--mode", choices=("auto", "incremental", "full"), default="auto")
    parser.add_argument("--collection", default="kosis_tables_structured")
    parser.add_argument("--db-path", type=Path, default=Path(__file__).resolve().parent / "qdrant_structured")
    parser.add_argument("--qdrant-url", default=None, help="Use a Qdrant server instead of an embedded local DB")
    parser.add_argument("--qdrant-api-key", default=None)
    parser.add_argument("--recreate", action="store_true", help="Delete and rebuild the named collection")
    parser.add_argument(
        "--delete-stale",
        action="store_true",
        help="Delete points absent from the newest catalog after a successful update.",
    )
    parser.add_argument(
        "--refresh-payload",
        action="store_true",
        help="Rewrite all payloads even if their saved payload hash is unchanged.",
    )
    parser.add_argument("--overwrite-output", action="store_true")
    parser.add_argument("--encode-batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--qdrant-batch-size", type=int, default=DEFAULT_QDRANT_BATCH)
    parser.add_argument("--shard-size", type=int, default=DEFAULT_SHARD_SIZE)
    parser.add_argument("--fp16", action="store_true", help="Use FP16 inference for newly encoded vectors")
    parser.add_argument("--limit", type=int, default=None, help="First N catalog records only; useful for a smoke test")
    parser.add_argument("--dry-run", action="store_true", help="Report reuse/encode counts without GPU, output or Qdrant writes")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    for option in ("encode_batch_size", "qdrant_batch_size", "shard_size"):
        if getattr(args, option) <= 0:
            parser.error(f"--{option.replace('_', '-')} must be positive")
    if args.mode == "incremental" and args.reuse_bundle is None:
        parser.error("--mode incremental requires --reuse-bundle")
    if args.reuse_bundle is not None and not args.reuse_bundle.is_dir():
        parser.error(f"--reuse-bundle does not exist: {args.reuse_bundle}")
    if args.reuse_bundle is not None and args.output_bundle.resolve() == args.reuse_bundle.resolve():
        parser.error("--output-bundle must be a new directory, not the reuse bundle itself")
    if args.limit is not None and args.delete_stale:
        parser.error("--delete-stale cannot be combined with --limit")
    return args


def main() -> None:
    args = parse_args()
    started = time.perf_counter()
    current = load_catalog_state(args.catalog, args.limit)

    reuse_documents = resolve_reuse_documents(args.reuse_bundle, args.reuse_documents) if args.reuse_bundle else None
    old = load_reuse_state(reuse_documents)
    has_reuse = args.reuse_bundle is not None and bool(old)
    if args.mode == "incremental" and not has_reuse:
        raise SystemExit(
            "Incremental mode needs both a vector bundle and a matching previous documents state. "
            "For the legacy v4 bundle, pass --reuse-documents src/bge_encoding/documents.jsonl."
        )
    if args.mode == "full":
        old = {}
        has_reuse = False
        print("[MODE] full: all vectors will be encoded again")
    else:
        print(f"[MODE] {'incremental' if has_reuse else 'full (no reusable state found)'}")

    new_keys = set(current) - set(old)
    removed_keys = set(old) - set(current)
    changed_fields = {
        field: sum(not field_is_same(old.get(key), record, field) for key, record in current.items())
        for field in FIELDS
    }
    changed_payloads = sum(
        old.get(key) is None or old[key].payload_hash != record.payload_hash
        for key, record in current.items()
    )
    print(
        "[DIFF] "
        f"new={len(new_keys):,}, removed={len(removed_keys):,}, "
        + ", ".join(f"{field}_changed={changed_fields[field]:,}" for field in FIELDS)
        + f", payload_changed_or_unknown={changed_payloads:,}"
    )
    if args.dry_run:
        for field in FIELDS:
            reusable = len(current) - changed_fields[field] if has_reuse else 0
            print(f"[DRY-RUN] {field}: reuse={reusable:,}, encode={len(current)-reusable:,}")
        print("[DRY-RUN] No GPU model, files, or Qdrant collection was touched.")
        return

    prepare_output(args.output_bundle, args.overwrite_output)
    writer = BundleWriter(args.output_bundle, args.shard_size)
    client, models, existing_ids, created_collection = open_qdrant(args)
    existing_ids_at_start = set(existing_ids)
    sink = QdrantVectorSink(client, models, args.collection, existing_ids, args.qdrant_batch_size)
    encoder_holder: dict[str, BGEEncoder | None] = {"encoder": None}
    field_counts: dict[str, dict[str, int]] = {}

    try:
        for field in FIELDS:
            field_counts[field] = process_field(
                field=field,
                current=current,
                old=old,
                reuse_bundle=args.reuse_bundle if has_reuse else None,
                writer=writer,
                sink=sink,
                encoder_holder=encoder_holder,
                args=args,
            )
        sink.flush()
        payload_updates = update_payloads(
            catalog=args.catalog,
            current=current,
            old=old,
            existing_ids_at_start=existing_ids_at_start,
            client=client,
            models=models,
            args=args,
        )
        create_payload_indexes(client, models, args.collection)
        stale_ids = existing_ids_at_start - {point_id(table_key) for table_key in current}
        deleted = delete_stale_points(client, models, args.collection, stale_ids, args.qdrant_batch_size) if args.delete_stale else 0
        writer.close()
        write_documents_state(args.output_bundle / "documents.jsonl", current)
        summary = {
            "status": "complete",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "catalog": str(args.catalog),
            "catalog_records": len(current),
            "collection": args.collection,
            "qdrant_target": args.qdrant_url or str(args.db_path),
            "collection_created": created_collection,
            "vectors": {"dense": DENSE, "sparse": SPARSE, "dimension": DIM},
            "bge": {"model": MODEL, "revision": REVISION, "max_length": MAX_LENGTH},
            "field_counts": field_counts,
            "payload_updates": payload_updates,
            "stale_deleted": deleted,
            "delete_stale_requested": args.delete_stale,
            "output_bundle": str(args.output_bundle),
            "reuse_bundle": str(args.reuse_bundle) if args.reuse_bundle else None,
            "elapsed_seconds": round(time.perf_counter() - started, 1),
        }
        (args.output_bundle / "manifest.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
