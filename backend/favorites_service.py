"""로그인 사용자의 통계표 즐겨찾기(별표) 저장·조회."""

from __future__ import annotations

from typing import Any

from backend.auth_service import isoformat, utc_now
from backend.database import session
from backend.errors import BackendError
from backend.table_catalog_service import get_table, kosis_table_url


def add_favorite(user_id: str, table_key: str) -> dict[str, Any]:
    """카탈로그에서 메타데이터를 채워 즐겨찾기에 추가한다(멱등)."""
    table = get_table(table_key)
    if table is None:
        raise BackendError(
            "TABLE_NOT_FOUND",
            "해당 통계표를 찾을 수 없습니다.",
            status_code=404,
        )
    now = isoformat(utc_now())
    with session() as connection:
        connection.execute(
            "INSERT INTO favorites"
            "(user_id, table_key, org_id, org_name, tbl_id, tbl_name, category_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, table_key) DO NOTHING",
            (
                user_id,
                table["table_key"],
                table["org_id"],
                table["org_name"],
                table["tbl_id"],
                table["tbl_name"],
                table["category_path"],
                now,
            ),
        )
    return {**table, "favorited": True}


def remove_favorite(user_id: str, table_key: str) -> None:
    with session() as connection:
        connection.execute(
            "DELETE FROM favorites WHERE user_id = ? AND table_key = ?",
            (user_id, table_key),
        )


def list_favorites(user_id: str) -> dict[str, Any]:
    with session() as connection:
        rows = connection.execute(
            "SELECT table_key, org_id, org_name, tbl_id, tbl_name, category_path, created_at "
            "FROM favorites WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    items = [
        {
            "table_key": row["table_key"],
            "org_id": row["org_id"],
            "org_name": row["org_name"],
            "tbl_id": row["tbl_id"],
            "tbl_name": row["tbl_name"],
            "category_path": row["category_path"],
            "created_at": row["created_at"],
            "kosis_url": kosis_table_url(row["org_id"], row["tbl_id"]),
            "favorited": True,
        }
        for row in rows
    ]
    return {"items": items, "total": len(items)}


def favorite_keys(user_id: str) -> set[str]:
    """검색 결과에 별표 상태를 표시하기 위한 키 집합."""
    with session() as connection:
        rows = connection.execute(
            "SELECT table_key FROM favorites WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {row["table_key"] for row in rows}
