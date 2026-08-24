"""로그인 사용자의 대화와 메시지를 저장하고 소유권을 검사한다."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

from backend.auth_service import isoformat, utc_now
from backend.database import session
from backend.errors import BackendError


def default_title(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return (normalized[:57] + "...") if len(normalized) > 60 else (normalized or "새 대화")


def _require_owned_conversation(
    connection: sqlite3.Connection,
    conversation_id: str,
    user_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT id, user_id, title, created_at, updated_at FROM conversations "
        "WHERE id = ? AND user_id = ?",
        (conversation_id, user_id),
    ).fetchone()
    if row is None:
        raise BackendError(
            "CONVERSATION_NOT_FOUND",
            "대화를 찾을 수 없습니다.",
            status_code=404,
        )
    return row


def create_conversation(user_id: str, title: str = "새 대화") -> dict[str, Any]:
    clean_title = default_title(title)
    conversation_id = str(uuid.uuid4())
    now = isoformat(utc_now())
    with session() as connection:
        connection.execute(
            "INSERT INTO conversations(id, user_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, user_id, clean_title, now, now),
        )
    return {
        "id": conversation_id,
        "title": clean_title,
        "created_at": now,
        "updated_at": now,
    }


def ensure_conversation(user_id: str, conversation_id: str | None, title_source: str) -> str:
    if conversation_id:
        with session() as connection:
            _require_owned_conversation(connection, conversation_id, user_id)
        return conversation_id
    return str(create_conversation(user_id, default_title(title_source))["id"])


def add_message(
    user_id: str,
    conversation_id: str,
    *,
    role: str,
    kind: str,
    content: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if role not in {"user", "assistant", "system"}:
        raise ValueError(f"unsupported message role: {role}")
    message_id = str(uuid.uuid4())
    now = isoformat(utc_now())
    payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    with session() as connection:
        _require_owned_conversation(connection, conversation_id, user_id)
        connection.execute(
            "INSERT INTO messages(id, conversation_id, role, kind, content, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, conversation_id, role, kind, content, payload_json, now),
        )
        connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
    return {
        "id": message_id,
        "role": role,
        "kind": kind,
        "content": content,
        "payload": payload,
        "created_at": now,
    }


def assistant_content(result: dict[str, Any]) -> str:
    answer = result.get("answer")
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    document = result.get("article_document")
    if isinstance(document, dict) and isinstance(document.get("text"), str):
        return f"본문 준비가 완료되었습니다. ({len(document['text']):,}자)"
    return "분석 결과가 생성되었습니다."


def list_conversations(user_id: str, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    with session() as connection:
        total = int(
            connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        )
        rows = connection.execute(
            "SELECT c.id, c.title, c.created_at, c.updated_at, "
            "COUNT(m.id) AS message_count, "
            "(SELECT content FROM messages lm WHERE lm.conversation_id = c.id "
            " ORDER BY lm.created_at DESC, lm.rowid DESC LIMIT 1) AS last_message "
            "FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id "
            "WHERE c.user_id = ? GROUP BY c.id "
            "ORDER BY c.updated_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()
    items = [
        {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": int(row["message_count"]),
            "last_message": row["last_message"],
        }
        for row in rows
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_conversation(user_id: str, conversation_id: str) -> dict[str, Any]:
    with session() as connection:
        conversation = _require_owned_conversation(connection, conversation_id, user_id)
        rows = connection.execute(
            "SELECT id, role, kind, content, payload_json, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
            (conversation_id,),
        ).fetchall()
    messages = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else None
        except json.JSONDecodeError:
            payload = None
        messages.append(
            {
                "id": row["id"],
                "role": row["role"],
                "kind": row["kind"],
                "content": row["content"],
                "payload": payload,
                "created_at": row["created_at"],
            }
        )
    return {
        "id": conversation["id"],
        "title": conversation["title"],
        "created_at": conversation["created_at"],
        "updated_at": conversation["updated_at"],
        "messages": messages,
    }


def delete_conversation(user_id: str, conversation_id: str) -> None:
    with session() as connection:
        _require_owned_conversation(connection, conversation_id, user_id)
        connection.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )
