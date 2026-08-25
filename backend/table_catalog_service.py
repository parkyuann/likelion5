"""KOSIS 통계표 카탈로그 검색.

우선순위:
  1) data/kosis_catalog/tables_v5.sqlite (v5, 28.7만 통계표) — build_table_index.py로 생성
  2) 없으면 data/kosis_catalog_enriched_sample600.jsonl (600개) 메모리 폴백

검색은 통계표명 부분일치(substring). SQLite 경로는 tbl_name LIKE 스캔으로 저RAM·빠름.
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SQLITE_PATH = ROOT / "data" / "kosis_catalog" / "tables_v5.sqlite"
SAMPLE_PATH = ROOT / "data" / "kosis_catalog_enriched_sample600.jsonl"


def kosis_table_url(org_id: str, tbl_id: str) -> str:
    return f"https://kosis.kr/statHtml/statHtml.do?orgId={org_id}&tblId={tbl_id}"


# ── SQLite(v5) 경로 ─────────────────────────────────────────
@lru_cache(maxsize=1)
def _sqlite_available() -> bool:
    return SQLITE_PATH.exists()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_public(row: sqlite3.Row) -> dict[str, Any]:
    try:
        units = json.loads(row["units"]) if row["units"] else []
    except json.JSONDecodeError:
        units = []
    try:
        period_types = json.loads(row["period_types"]) if row["period_types"] else []
    except json.JSONDecodeError:
        period_types = []
    return {
        "table_key": row["table_key"],
        "org_id": row["org_id"],
        "org_name": row["org_name"],
        "tbl_id": row["tbl_id"],
        "tbl_name": row["tbl_name"],
        "category_path": row["category_path"] or "",
        "units": units,
        "period_types": period_types,
        "latest_period": row["latest_period"],
        "kosis_url": kosis_table_url(row["org_id"], row["tbl_id"]),
    }


def _sqlite_search(
    query: str,
    *,
    limit: int,
    offset: int,
    organization: str = "",
) -> dict[str, Any]:
    tokens = [t for t in query.lower().split() if t][:6]  # 토큰 과다 방지
    token_clauses = ["LOWER(tbl_name) LIKE ?" for _ in tokens]
    token_params: list[Any] = [f"%{t}%" for t in tokens]
    facet_where = f"WHERE {' AND '.join(token_clauses)}" if token_clauses else ""

    clauses = list(token_clauses)
    params = list(token_params)
    if organization:
        clauses.append("org_name = ?")
        params.append(organization)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _connect()
    try:
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM stat_tables {where}", params).fetchone()[0]
        )
        rows = conn.execute(
            f"SELECT * FROM stat_tables {where} ORDER BY latest_period DESC "
            "LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        facet_prefix = f"{facet_where} AND" if facet_where else "WHERE"
        organization_rows = conn.execute(
            "SELECT org_name, COUNT(*) AS count FROM stat_tables "
            f"{facet_prefix} org_name <> '' GROUP BY org_name "
            "ORDER BY count DESC, org_name ASC",
            token_params,
        ).fetchall()
    finally:
        conn.close()
    return {
        "items": [_row_to_public(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "organizations": [
            {"name": row["org_name"], "count": int(row["count"])}
            for row in organization_rows
        ],
    }


def _sqlite_get(table_key: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM stat_tables WHERE table_key = ?", (table_key,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_public(row) if row else None


# ── sample600 폴백 경로(메모리) ──────────────────────────────
def _sample_first_category(record: dict[str, Any]) -> str:
    paths = record.get("category_paths") or []
    if paths and isinstance(paths[0], list):
        return " > ".join(str(p) for p in paths[0])
    return ""


def _sample_to_public(record: dict[str, Any]) -> dict[str, Any]:
    org_id = str(record.get("org_id") or "")
    tbl_id = str(record.get("tbl_id") or "")
    return {
        "table_key": record.get("table_key") or f"{org_id}:{tbl_id}",
        "org_id": org_id,
        "org_name": str(record.get("org_name") or ""),
        "tbl_id": tbl_id,
        "tbl_name": str(record.get("tbl_name") or ""),
        "category_path": _sample_first_category(record),
        "units": record.get("units") or [],
        "period_types": record.get("period_types") or [],
        "latest_period": record.get("latest_period"),
        "kosis_url": kosis_table_url(org_id, tbl_id),
    }


@lru_cache(maxsize=1)
def _sample_load() -> list[tuple[dict[str, Any], str]]:
    entries: list[tuple[dict[str, Any], str]] = []
    if not SAMPLE_PATH.exists():
        return entries
    with SAMPLE_PATH.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            public = _sample_to_public(record)
            haystack = public["tbl_name"].lower()
            entries.append((public, haystack))
    return entries


def _sample_search(
    query: str,
    *,
    limit: int,
    offset: int,
    organization: str = "",
) -> dict[str, Any]:
    entries = _sample_load()
    tokens = [t for t in query.lower().split() if t]
    if not tokens:
        scored = [(0, public) for public, _ in entries]
        scored.sort(key=lambda x: x[1]["tbl_name"])
    else:
        scored = []
        for public, haystack in entries:
            score = sum(1 for t in tokens if t in haystack)
            if score:
                scored.append((score, public))
        scored.sort(key=lambda x: (x[0], x[1].get("latest_period") or 0), reverse=True)
    organization_counts: dict[str, int] = {}
    for _, public in scored:
        name = public.get("org_name") or ""
        if name:
            organization_counts[name] = organization_counts.get(name, 0) + 1
    if organization:
        scored = [item for item in scored if item[1].get("org_name") == organization]
    total = len(scored)
    page = [public for _, public in scored[offset : offset + limit]]
    organizations = [
        {"name": name, "count": count}
        for name, count in sorted(
            organization_counts.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    return {
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "organizations": organizations,
    }


def _sample_get(table_key: str) -> dict[str, Any] | None:
    for public, _ in _sample_load():
        if public["table_key"] == table_key:
            return public
    return None


# ── 공개 API ────────────────────────────────────────────────
def search_tables(
    query: str,
    *,
    limit: int = 20,
    offset: int = 0,
    organization: str = "",
) -> dict[str, Any]:
    if _sqlite_available():
        return _sqlite_search(
            query,
            limit=limit,
            offset=offset,
            organization=organization,
        )
    return _sample_search(
        query,
        limit=limit,
        offset=offset,
        organization=organization,
    )


def get_table(table_key: str) -> dict[str, Any] | None:
    if _sqlite_available():
        return _sqlite_get(table_key)
    return _sample_get(table_key)
