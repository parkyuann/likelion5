"""Concrete official-search, v6 BM25, and v6 Dense channel adapters."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests

from src.news_verification.runtime.operational_retrieval_v2 import QuerySpec


SEARCH_URL = "https://kosis.kr/openapi/statisticsSearch.do"
V6_FIELDS = ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS")


def _fts5_and_query(text: str) -> str:
    """Compile article text as literal FTS5 terms joined by implicit AND."""
    tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    if not tokens:
        raise RuntimeError("V6_BM25_EMPTY_QUERY")
    # Quoting each tokenizer-like term prevents article punctuation and words
    # such as OR/NOT from being interpreted as FTS5 query syntax.  Adjacent
    # quoted terms retain the existing implicit-AND retrieval semantics.
    return " ".join(f'"{token}"' for token in tokens)


def build_bm25_index(records_path: Path, output_path: Path) -> dict[str, Any]:
    """Build a fresh additive FTS5 index; never modifies v5/v5b artifacts."""
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite BM25 index: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(output_path)
    try:
        connection.execute("CREATE TABLE records(record_id TEXT PRIMARY KEY, table_key TEXT NOT NULL, field TEXT NOT NULL, text TEXT NOT NULL, text_sha256 TEXT NOT NULL)")
        connection.execute("CREATE VIRTUAL TABLE records_fts USING fts5(record_id UNINDEXED, table_key UNINDEXED, field UNINDEXED, text, tokenize='unicode61')")
        count = 0
        with records_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                values = (
                    str(row["record_id"]), str(row["table_key"]), str(row["field"]),
                    str(row["text"]), str(row["text_sha256"]),
                )
                connection.execute("INSERT INTO records VALUES(?,?,?,?,?)", values)
                connection.execute("INSERT INTO records_fts VALUES(?,?,?,?)", values[:4])
                count += 1
                if count % 10000 == 0:
                    connection.commit()
        connection.commit()
        return {"contract": "kosis-v6-bm25-index-v1", "records": count, "fields": list(V6_FIELDS)}
    except Exception:
        connection.close()
        # Leave a clearly incomplete artifact for diagnosis; callers must use a
        # fresh output path rather than treating it as READY.
        raise
    finally:
        try:
            connection.close()
        except Exception:
            pass


class OfficialKosisSearchChannel:
    def __init__(self, api_key: str, *, timeout_seconds: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def __call__(self, query: QuerySpec, fields: Sequence[str], top_k: int) -> Iterable[Mapping[str, Any]]:
        try:
            response = requests.get(
                SEARCH_URL,
                params={
                    "method": "getList", "apiKey": self.api_key, "searchNm": query.text,
                    "sort": "RANK", "startCount": 1, "resultCount": top_k,
                    "format": "json", "jsonVD": "Y",
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise RuntimeError("KOSIS_SEARCH_UNAVAILABLE") from exc
        except ValueError as exc:
            raise RuntimeError("KOSIS_SEARCH_INVALID_RESPONSE") from exc
        # KOSIS represents a valid zero-hit search as an error-shaped JSON
        # object (err=30), not as an empty list.  Preserve it as an audited
        # empty path; other object-shaped responses remain fail-closed.
        if isinstance(payload, dict) and str(payload.get("err") or "") == "30":
            return []
        if not isinstance(payload, list):
            raise RuntimeError("KOSIS_SEARCH_INVALID_RESPONSE")
        rows = []
        for rank, row in enumerate(payload, 1):
            org_id = str(row.get("ORG_ID") or "")
            tbl_id = str(row.get("TBL_ID") or "")
            if org_id and tbl_id:
                rows.append({
                    "table_key": f"{org_id}:{tbl_id}",
                    "record_id": f"official:{query.query_id}:{rank}:{org_id}:{tbl_id}",
                    "field": "TITLE",
                    "score": row.get("RANK"),
                })
        return rows


class V6Bm25Channel:
    def __init__(self, index_path: Path) -> None:
        self.index_path = index_path

    def __call__(self, query: QuerySpec, fields: Sequence[str], top_k: int) -> Iterable[Mapping[str, Any]]:
        if not self.index_path.is_file():
            raise RuntimeError("V6_BM25_UNAVAILABLE")
        fts_query = _fts5_and_query(query.text)
        connection = sqlite3.connect(f"file:{self.index_path.as_posix()}?mode=ro", uri=True)
        try:
            placeholders = ",".join("?" for _ in fields)
            sql = (
                "SELECT record_id,table_key,field,bm25(records_fts) AS distance "
                f"FROM records_fts WHERE records_fts MATCH ? AND field IN ({placeholders}) "
                "ORDER BY distance ASC, record_id ASC LIMIT ?"
            )
            rows = connection.execute(sql, (fts_query, *fields, top_k)).fetchall()
            return [
                {"record_id": row[0], "table_key": row[1], "field": row[2], "score": -float(row[3])}
                for row in rows
            ]
        except sqlite3.Error as exc:
            raise RuntimeError("V6_BM25_QUERY_FAILED") from exc
        finally:
            connection.close()


class V6DenseChannel:
    """Qdrant adapter; query encoder must be the pinned BGE-M3-ko deployment."""

    def __init__(
        self,
        client: Any,
        collection: str,
        encoder: Callable[[str], Sequence[float]],
        *,
        vector_name: str = "dense",
    ) -> None:
        self.client = client
        self.collection = collection
        self.encoder = encoder
        self.vector_name = vector_name

    def __call__(self, query: QuerySpec, fields: Sequence[str], top_k: int) -> Iterable[Mapping[str, Any]]:
        from qdrant_client import models
        vector = list(self.encoder(query.text))
        if len(vector) != 1024:
            raise RuntimeError("V6_QUERY_VECTOR_DIMENSION_MISMATCH")
        try:
            result = self.client.query_points(
                collection_name=self.collection,
                query=vector,
                using=self.vector_name,
                query_filter=models.Filter(must=[
                    models.FieldCondition(key="field", match=models.MatchAny(any=list(fields)))
                ]),
                limit=top_k,
                with_payload=True,
            )
        except Exception as exc:
            raise RuntimeError("V6_DENSE_QUERY_FAILED") from exc
        points = getattr(result, "points", result)
        return [
            {
                "record_id": str(point.payload["record_id"]),
                "table_key": str(point.payload["table_key"]),
                "field": str(point.payload["field"]),
                "score": float(point.score),
            }
            for point in points
        ]


__all__ = ["OfficialKosisSearchChannel", "V6Bm25Channel", "V6DenseChannel", "build_bm25_index"]




