"""KOSIS 카탈로그 v5(1.8GB, 28.7만 통계표)를 검색용 SQLite 인덱스로 1회 변환한다.

메모리에 통째로 올리지 않고 스트리밍으로 읽어, 통계표 탐색에 필요한 슬림 필드만
data/kosis_catalog/tables_v5.sqlite 에 적재한다. 검색은 haystack 컬럼 LIKE 스캔.

실행(저장소 루트에서):
    ./.venv/Scripts/python.exe backend/build_table_index.py
옵션:
    --src <path>   원본 jsonl (기본: data/kosis_catalog/kosis_catalog_v5_260817.jsonl)
    --out <path>   출력 sqlite (기본: data/kosis_catalog/tables_v5.sqlite)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = ROOT / "data" / "kosis_catalog" / "kosis_catalog_v5_260817.jsonl"
DEFAULT_OUT = ROOT / "data" / "kosis_catalog" / "tables_v5.sqlite"

SCHEMA = """
DROP TABLE IF EXISTS stat_tables;
CREATE TABLE stat_tables (
    table_key TEXT PRIMARY KEY,
    org_id TEXT,
    org_name TEXT,
    tbl_id TEXT,
    tbl_name TEXT,
    category_path TEXT,
    units TEXT,
    period_types TEXT,
    latest_period INTEGER,
    haystack TEXT
);
"""


def _category(record: dict) -> str:
    """v5는 primary_path(list)를 우선 사용, 없으면 category_paths(dict) 첫 경로."""
    primary = record.get("primary_path")
    if isinstance(primary, list) and primary:
        return " > ".join(str(p) for p in primary)
    paths = record.get("category_paths")
    if isinstance(paths, dict):
        for entries in paths.values():
            if isinstance(entries, list) and entries:
                names = entries[0].get("names") if isinstance(entries[0], dict) else None
                if names:
                    return " > ".join(str(n) for n in names)
    elif isinstance(paths, list) and paths and isinstance(paths[0], list):
        return " > ".join(str(p) for p in paths[0])
    return ""


def _latest_period(record: dict) -> int | None:
    value = record.get("latest_period")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row(record: dict) -> tuple | None:
    org_id = str(record.get("org_id") or "")
    tbl_id = str(record.get("tbl_id") or "")
    table_key = record.get("table_key") or f"{org_id}:{tbl_id}"
    if not table_key:
        return None
    org_name = str(record.get("org_name") or "")
    tbl_name = str(record.get("tbl_name") or "")
    category = _category(record)
    units = record.get("units") or []
    period_types = record.get("period_types") or []
    item_names = " ".join(str(it.get("itm_nm", "")) for it in (record.get("items") or []))
    haystack = " ".join(
        [tbl_name, org_name, category, item_names, str(record.get("doc_item_index", ""))]
    ).lower()
    return (
        table_key,
        org_id,
        org_name,
        tbl_id,
        tbl_name,
        category,
        json.dumps(units, ensure_ascii=False),
        json.dumps(period_types, ensure_ascii=False),
        _latest_period(record),
        haystack,
    )


def build(src: Path, out: Path) -> int:
    if not src.exists():
        print(f"[에러] 원본을 찾을 수 없음: {src}", file=sys.stderr)
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".sqlite.tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")

    inserted = 0
    batch: list[tuple] = []
    with src.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            row = _row(record)
            if row is None:
                continue
            batch.append(row)
            if len(batch) >= 5000:
                conn.executemany(
                    "INSERT OR IGNORE INTO stat_tables VALUES (?,?,?,?,?,?,?,?,?,?)", batch
                )
                inserted += len(batch)
                batch.clear()
                if inserted % 50000 == 0:
                    print(f"  ...{inserted:,}건")
    if batch:
        conn.executemany(
            "INSERT OR IGNORE INTO stat_tables VALUES (?,?,?,?,?,?,?,?,?,?)", batch
        )
        inserted += len(batch)

    conn.execute("CREATE INDEX idx_stat_tables_period ON stat_tables(latest_period DESC)")
    conn.commit()
    conn.close()
    if out.exists():
        out.unlink()
    tmp.rename(out)
    return inserted


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default=str(DEFAULT_SRC))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    print(f"src={args.src}\nout={args.out}\n변환 시작...")
    total = build(Path(args.src), Path(args.out))
    print(f"완료: {total:,}개 통계표 인덱싱 → {args.out}")
