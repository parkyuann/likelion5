"""PostgreSQL conversation repository; no local-store fallback."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Mapping

from backend.auth_service import isoformat, utc_now
from backend.database import session
from backend.errors import BackendError


def default_title(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text)).strip()
    return (value[:57] + "...") if len(value) > 60 else (value or "새 대화")


def _owned(connection: Any, conversation_id: str, user_id: str) -> Mapping[str, Any]:
    row = connection.execute("SELECT id, user_id, title, created_at, updated_at FROM conversations WHERE id = %s AND user_id = %s", (conversation_id, user_id)).fetchone()
    if row is None:
        raise BackendError("CONVERSATION_NOT_FOUND", "대화를 찾을 수 없습니다.", status_code=404)
    return row


def create_conversation(user_id: str, title: str = "새 대화") -> dict[str, Any]:
    conversation_id, now, clean = str(uuid.uuid4()), utc_now(), default_title(title)
    with session() as connection:
        row = connection.execute("INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (%s, %s, %s, %s, %s) RETURNING id, title, created_at, updated_at", (conversation_id, user_id, clean, now, now)).fetchone()
    return {"id": str(row["id"]), "title": row["title"], "created_at": isoformat(row["created_at"]), "updated_at": isoformat(row["updated_at"])}


def ensure_conversation(user_id: str, conversation_id: str | None, title_source: str) -> str:
    if conversation_id:
        with session() as connection:
            _owned(connection, conversation_id, user_id)
        return conversation_id
    return str(create_conversation(user_id, title_source)["id"])


def add_message(user_id: str, conversation_id: str, *, role: str, kind: str, content: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if role not in {"user", "assistant", "system"}:
        raise ValueError(f"unsupported message role: {role}")
    message_id, now = str(uuid.uuid4()), utc_now()
    payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    with session() as connection:
        _owned(connection, conversation_id, user_id)
        connection.execute("INSERT INTO messages (id, conversation_id, role, kind, content, payload_json, created_at) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)", (message_id, conversation_id, role, kind, content, payload_json, now))
        connection.execute("UPDATE conversations SET updated_at = %s WHERE id = %s", (now, conversation_id))
    return {"id": message_id, "role": role, "kind": kind, "content": content, "payload": payload, "created_at": isoformat(now)}


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
        total = int(connection.execute("SELECT COUNT(*) AS count FROM conversations WHERE user_id = %s", (user_id,)).fetchone()["count"])
        rows = connection.execute("SELECT c.id, c.title, c.created_at, c.updated_at, COUNT(m.id) AS message_count, (SELECT content FROM messages lm WHERE lm.conversation_id = c.id ORDER BY lm.created_at DESC, lm.id DESC LIMIT 1) AS last_message FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id WHERE c.user_id = %s GROUP BY c.id, c.title, c.created_at, c.updated_at ORDER BY c.updated_at DESC, c.id DESC LIMIT %s OFFSET %s", (user_id, limit, offset)).fetchall()
    return {"items": [{"id": str(r["id"]), "title": r["title"], "created_at": isoformat(r["created_at"]), "updated_at": isoformat(r["updated_at"]), "message_count": int(r["message_count"]), "last_message": r["last_message"]} for r in rows], "total": total, "limit": limit, "offset": offset}


def get_conversation(user_id: str, conversation_id: str) -> dict[str, Any]:
    with session() as connection:
        conversation = _owned(connection, conversation_id, user_id)
        rows = connection.execute("SELECT id, role, kind, content, payload_json, created_at FROM messages WHERE conversation_id = %s ORDER BY created_at ASC, id ASC", (conversation_id,)).fetchall()
    messages = []
    for row in rows:
        raw = row.get("payload_json")
        try:
            payload = raw if isinstance(raw, (dict, list)) else (json.loads(raw) if raw else None)
        except (TypeError, json.JSONDecodeError):
            payload = None
        messages.append({"id": str(row["id"]), "role": row["role"], "kind": row["kind"], "content": row["content"], "payload": payload, "created_at": isoformat(row["created_at"])})
    return {"id": str(conversation["id"]), "title": conversation["title"], "created_at": isoformat(conversation["created_at"]), "updated_at": isoformat(conversation["updated_at"]), "messages": messages}


def delete_conversation(user_id: str, conversation_id: str) -> None:
    with session() as connection:
        _owned(connection, conversation_id, user_id)
        connection.execute("DELETE FROM conversations WHERE id = %s AND user_id = %s", (conversation_id, user_id))
