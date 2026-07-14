"""
KOSIS 통계표 카탈로그 + 뉴스 주장 + 검증 결과를 저장하는 SQLite 스키마.

"종합" 단계(전체 시스템 연결 + 자동 비교·판정)에서 쓸 저장소 설계다. 지금까지
실전1 산출물은 CSV(느슨한 구조, 조인 불가)로만 쌓아왔는데, 실제 비교·판정을
하려면 통계표-분류값-항목-주기가 서로 참조 가능한 관계형 구조가 필요해서
미리 설계해뒀다. 최소 핵심 테이블 7개 + 원본 API 응답 보존용 1개로 구성:

  kosis_tables(통계표 카탈로그) --1:N--> kosis_dimensions(분류 축)
                                --1:N--> kosis_dimension_values(분류값)
                                --1:N--> kosis_items(지표·항목·단위)
                                --1:N--> kosis_periods(수록주기)
  claims(구조화된 뉴스 주장) --1:N--> verification_results(공식 통계 비교·판정)
  kosis_items --1:N--> verification_results(판정의 공식 근거)

원본 프로토타입(kosis_schema_design.ipynb)과의 차이:
  - 메타데이터 조회를 노트북 자체의 urllib 호출 대신 `kosis_client.get_meta()`를
    그대로 재사용한다 — KOSIS API 클라이언트가 두 벌 존재하면(재시도/에러 처리
    방식이 서로 달라) 한쪽만 고쳐도 다른 쪽은 그대로 깨진 채 남는 문제가 생기므로,
    이 프로젝트에서 KOSIS 호출은 항상 kosis_client를 거치도록 통일했다.
  - `claims` 테이블 필드명을 `claim_extraction_schema.md`/`llm_claim_extractor.py`의
    필드명(value/unit/time_ref/source_org_raw 등)에 맞춰 재작성했다(원본은
    metric/claim_value/claim_unit 등 다른 이름을 썼음) — 두 스키마 이름이 다르면
    LLM 추출기 출력을 DB에 넣을 때마다 매번 필드 매핑 코드를 새로 짜야 해서.
  - raw_api_responses는 원본과 동일하게 KOSIS 정규화 테이블과 FK로 묶지 않는다
    (API 실패 응답도 그대로 보존해야 하므로).

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/kosis_schema.py  # DB 생성 + 표 1개 적재 검증
"""
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.kosis_client import get_meta  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "kosis_poc.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kosis_tables (
    table_key TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    tbl_id TEXT NOT NULL,
    tbl_name TEXT NOT NULL,
    tbl_name_eng TEXT,
    stat_id TEXT,
    stat_name TEXT,
    category_path TEXT,
    retrieved_at TEXT NOT NULL,
    UNIQUE (org_id, tbl_id)
);

CREATE TABLE IF NOT EXISTS kosis_dimensions (
    table_key TEXT NOT NULL,
    dimension_id TEXT NOT NULL,
    dimension_name TEXT NOT NULL,
    dimension_name_eng TEXT,
    dimension_order INTEGER,
    PRIMARY KEY (table_key, dimension_id),
    FOREIGN KEY (table_key) REFERENCES kosis_tables(table_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS kosis_dimension_values (
    table_key TEXT NOT NULL,
    dimension_id TEXT NOT NULL,
    value_id TEXT NOT NULL,
    value_name TEXT NOT NULL,
    value_name_eng TEXT,
    parent_value_id TEXT,
    PRIMARY KEY (table_key, dimension_id, value_id),
    FOREIGN KEY (table_key, dimension_id)
        REFERENCES kosis_dimensions(table_key, dimension_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS kosis_items (
    table_key TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    item_name_eng TEXT,
    unit_id TEXT,
    unit_name TEXT,
    PRIMARY KEY (table_key, item_id),
    FOREIGN KEY (table_key) REFERENCES kosis_tables(table_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS kosis_periods (
    table_key TEXT NOT NULL,
    period_type TEXT NOT NULL,
    period_name TEXT NOT NULL,
    start_period TEXT,
    end_period TEXT,
    PRIMARY KEY (table_key, period_type),
    FOREIGN KEY (table_key) REFERENCES kosis_tables(table_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    indicator_raw TEXT,
    population TEXT,
    value TEXT,
    unit TEXT,
    time_ref TEXT,
    source_org_raw TEXT,
    extracted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_results (
    verification_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL,
    table_key TEXT,
    item_id TEXT,
    official_value REAL,
    official_unit TEXT,
    official_period TEXT,
    verdict TEXT NOT NULL CHECK (verdict IN
        ('MATCH','MOSTLY_MATCH','MISMATCH','MISSING_CONTEXT','WRONG_COMPARISON',
         'STATISTICS_NOT_FOUND','INSUFFICIENT_INFORMATION','NEEDS_REVIEW')),
    explanation TEXT,
    verified_at TEXT NOT NULL,
    FOREIGN KEY (claim_id) REFERENCES claims(claim_id) ON DELETE CASCADE,
    FOREIGN KEY (table_key, item_id) REFERENCES kosis_items(table_key, item_id)
);

CREATE TABLE IF NOT EXISTS raw_api_responses (
    response_id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    request_params_json TEXT NOT NULL CHECK (json_valid(request_params_json)),
    response_json TEXT NOT NULL CHECK (json_valid(response_json)),
    retrieved_at TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table_key(org_id: str, tbl_id: str) -> str:
    return f"{org_id}:{tbl_id}"


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def _log_raw_response(conn: sqlite3.Connection, endpoint: str, params: dict, payload) -> None:
    retrieved_at = _utc_now()
    digest = hashlib.sha256((endpoint + json.dumps(params, sort_keys=True) + retrieved_at).encode()).hexdigest()
    conn.execute(
        "INSERT OR REPLACE INTO raw_api_responses VALUES (?, ?, ?, ?, ?)",
        (digest, endpoint, json.dumps(params, ensure_ascii=False),
         json.dumps(payload, ensure_ascii=False), retrieved_at),
    )


def upsert_kosis_table(conn: sqlite3.Connection, org_id: str, tbl_id: str) -> str:
    """kosis_client.get_meta()로 표/수록주기/분류·항목을 조회해 DB에 적재한다."""
    key = _table_key(org_id, tbl_id)
    tbl_meta = get_meta(org_id, tbl_id, "TBL")
    prd_meta = get_meta(org_id, tbl_id, "PRD")
    itm_meta = get_meta(org_id, tbl_id, "ITM")
    for meta_type, payload in [("TBL", tbl_meta), ("PRD", prd_meta), ("ITM", itm_meta)]:
        _log_raw_response(conn, f"getMeta:{meta_type}", {"orgId": org_id, "tblId": tbl_id}, payload)

    title = tbl_meta[0] if tbl_meta else {}
    now = _utc_now()

    with conn:
        conn.execute("""
            INSERT INTO kosis_tables
            (table_key, org_id, tbl_id, tbl_name, tbl_name_eng, stat_id, stat_name, category_path, retrieved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(table_key) DO UPDATE SET
              tbl_name=excluded.tbl_name, tbl_name_eng=excluded.tbl_name_eng, retrieved_at=excluded.retrieved_at
        """, (key, org_id, tbl_id, title.get("TBL_NM", tbl_id), title.get("TBL_NM_ENG"),
              title.get("STAT_ID"), title.get("STAT_NM"), title.get("CATEGORY_PATH"), now))

        for p in prd_meta:
            code = p.get("PRD_SE", "IR")
            conn.execute("""
                INSERT OR REPLACE INTO kosis_periods (table_key, period_type, period_name, start_period, end_period)
                VALUES (?, ?, ?, ?, ?)
            """, (key, code, p.get("PRD_SE", code), p.get("STRT_PRD_DE"), p.get("END_PRD_DE")))

        dimensions_seen = set()
        for row in itm_meta:
            dim_id, dim_name = row.get("OBJ_ID"), row.get("OBJ_NM")
            if dim_id and dim_name and dim_id not in dimensions_seen:
                dimensions_seen.add(dim_id)
                order = int(row["OBJ_ID_SN"]) if str(row.get("OBJ_ID_SN", "")).isdigit() else None
                conn.execute("""
                    INSERT OR REPLACE INTO kosis_dimensions VALUES (?, ?, ?, ?, ?)
                """, (key, dim_id, dim_name, row.get("OBJ_NM_ENG"), order))

        for row in itm_meta:
            dim_id, value_id = row.get("OBJ_ID"), row.get("ITM_ID")
            if not dim_id or not value_id:
                continue
            if dim_id == "ITEM":
                conn.execute("""
                    INSERT OR REPLACE INTO kosis_items
                    (table_key, item_id, item_name, item_name_eng, unit_id, unit_name)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (key, value_id, row.get("ITM_NM"), row.get("ITM_NM_ENG"),
                      row.get("UNIT_ID"), row.get("UNIT_NM")))
            else:
                conn.execute("""
                    INSERT OR REPLACE INTO kosis_dimension_values
                    (table_key, dimension_id, value_id, value_name, value_name_eng, parent_value_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (key, dim_id, value_id, row.get("ITM_NM"), row.get("ITM_NM_ENG"), row.get("UP_ITM_ID")))
    return key


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    conn = init_db()
    key = upsert_kosis_table(conn, "101", "DT_1B040A3")

    fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert not fk_errors, fk_errors

    import pandas as pd
    summary = pd.read_sql_query("""
        SELECT t.table_key, t.tbl_name,
               COUNT(DISTINCT d.dimension_id) AS 분류_수,
               COUNT(DISTINCT i.item_id) AS 항목_수,
               COUNT(DISTINCT p.period_type) AS 수록주기_수
        FROM kosis_tables t
        LEFT JOIN kosis_dimensions d ON d.table_key = t.table_key
        LEFT JOIN kosis_items i ON i.table_key = t.table_key
        LEFT JOIN kosis_periods p ON p.table_key = t.table_key
        WHERE t.table_key = ?
        GROUP BY t.table_key
    """, conn, params=(key,))
    print("외래키 검증 통과, 적재 완료:", key)
    print(summary.to_string(index=False))
    print(f"\nDB 위치: {DB_PATH}")
