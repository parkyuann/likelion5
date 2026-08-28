"""Opaque Redis-backed browser sessions.

The session store is deliberately separate from any optional search cache.  It
stores only server-side session records and uses one Lua operation for issuing a
session and enforcing the per-user limit.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.errors import BackendError

try:  # redis-py is supplied by the API image; tests can inject a fake client.
    import redis
except ImportError:  # pragma: no cover - exercised by a dependency preflight
    redis = None  # type: ignore[assignment]


SESSION_TTL_SECONDS = 604800
MAX_ACTIVE_SESSIONS = 5
SESSION_COOKIE_NAME = "__Host-kosis_session"
SESSION_KEY_PREFIX = "auth:session:"
USER_SESSIONS_KEY_PREFIX = "auth:user-sessions:"
USER_SEQUENCE_KEY_PREFIX = "auth:user-session-sequence:"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,256}$")

# ARGV[1] value, ARGV[2] fixed TTL, ARGV[3] session id,
# ARGV[4] maximum active sessions, ARGV[5] session-key prefix.
CREATE_SESSION_LUA = """
local members = redis.call('ZRANGE', KEYS[2], 0, -1)
for _, member in ipairs(members) do
  if redis.call('EXISTS', ARGV[5] .. member) == 0 then
    redis.call('ZREM', KEYS[2], member)
  end
end
local inserted = redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX')
if not inserted then
  return 0
end
local sequence = redis.call('INCR', KEYS[3])
redis.call('ZADD', KEYS[2], sequence, ARGV[3])
while redis.call('ZCARD', KEYS[2]) > tonumber(ARGV[4]) do
  local oldest = redis.call('ZRANGE', KEYS[2], 0, 0)[1]
  if not oldest then
    break
  end
  redis.call('ZREM', KEYS[2], oldest)
  redis.call('DEL', ARGV[5] .. oldest)
end
return 1
"""

DELETE_SESSION_LUA = """
local value = redis.call('GET', KEYS[1])
if value then
  redis.call('DEL', KEYS[1])
  redis.call('ZREM', KEYS[2], ARGV[1])
end
return value or ''
"""

DELETE_ALL_SESSIONS_LUA = """
local members = redis.call('ZRANGE', KEYS[1], 0, -1)
for _, member in ipairs(members) do
  redis.call('DEL', ARGV[1] .. member)
end
redis.call('DEL', KEYS[1])
redis.call('DEL', KEYS[2])
return #members
"""


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    user_id: str
    expires_at: datetime


_client: Any | None = None


def _configuration() -> tuple[str, str]:
    ttl = os.getenv("AUTH_SESSION_TTL_SECONDS", str(SESSION_TTL_SECONDS)).strip()
    maximum = os.getenv("AUTH_MAX_ACTIVE_SESSIONS", str(MAX_ACTIVE_SESSIONS)).strip()
    cookie_name = os.getenv("AUTH_COOKIE_NAME", SESSION_COOKIE_NAME).strip()
    if ttl != str(SESSION_TTL_SECONDS) or maximum != str(MAX_ACTIVE_SESSIONS):
        raise BackendError(
            "AUTH_CONFIGURATION_INVALID",
            "세션 TTL과 사용자당 최대 세션 수는 고정 계약과 달라질 수 없습니다.",
            status_code=503,
        )
    if cookie_name != SESSION_COOKIE_NAME:
        raise BackendError(
            "AUTH_CONFIGURATION_INVALID",
            "인증 cookie 이름이 고정 계약과 다릅니다.",
            status_code=503,
        )
    url = os.getenv("REDIS_SESSION_URL", "").strip()
    if not url:
        raise BackendError(
            "SESSION_STORE_CONFIGURATION_PENDING",
            "redis-session 연결 설정이 없습니다.",
            status_code=503,
        )
    return url, cookie_name


def cookie_name() -> str:
    configured = os.getenv("AUTH_COOKIE_NAME", SESSION_COOKIE_NAME).strip()
    if configured != SESSION_COOKIE_NAME:
        raise BackendError(
            "AUTH_CONFIGURATION_INVALID",
            "인증 cookie 이름이 고정 계약과 다릅니다.",
            status_code=503,
        )
    return configured


def redis_client() -> Any:
    """Return the session-only Redis client; never read the cache URL."""

    global _client
    if _client is not None:
        return _client
    url, _ = _configuration()
    if redis is None:
        raise BackendError(
            "SESSION_STORE_DRIVER_UNAVAILABLE",
            "redis-session 드라이버를 사용할 수 없습니다.",
            status_code=503,
        )
    try:
        _client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
    except Exception as exc:
        raise BackendError(
            "SESSION_STORE_UNAVAILABLE",
            "redis-session에 연결할 수 없습니다.",
            status_code=503,
        ) from exc
    return _client


def _validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise BackendError(
            "INVALID_SESSION",
            "로그인이 만료되었거나 유효하지 않습니다.",
            status_code=401,
        )
    return session_id


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _session_key(session_id: str) -> str:
    return SESSION_KEY_PREFIX + _validate_session_id(session_id)


def _user_sessions_key(user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError("user_id is required")
    return USER_SESSIONS_KEY_PREFIX + user_id


def _user_sequence_key(user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError("user_id is required")
    return USER_SEQUENCE_KEY_PREFIX + user_id


def _session_value(user_id: str, expires_at: datetime) -> str:
    return json.dumps(
        {"user_id": user_id, "expires_at": expires_at.isoformat()},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def create_session(user_id: str, *, now: datetime | None = None, client: Any | None = None) -> SessionRecord:
    """Create a fixed-lifetime session and atomically retain only the newest five."""

    if not user_id:
        raise ValueError("user_id is required")
    client = client or redis_client()
    now = (now or _now()).astimezone(timezone.utc).replace(microsecond=0)
    expires_at = now + timedelta(seconds=SESSION_TTL_SECONDS)
    for _ in range(3):
        session_id = secrets.token_urlsafe(32)
        try:
            created = client.eval(
                CREATE_SESSION_LUA,
                3,
                _session_key(session_id),
                _user_sessions_key(user_id),
                _user_sequence_key(user_id),
                _session_value(user_id, expires_at),
                str(SESSION_TTL_SECONDS),
                session_id,
                str(MAX_ACTIVE_SESSIONS),
                SESSION_KEY_PREFIX,
            )
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(
                "SESSION_STORE_UNAVAILABLE",
                "redis-session 세션을 발급할 수 없습니다.",
                status_code=503,
            ) from exc
        if int(created or 0) == 1:
            return SessionRecord(session_id, user_id, expires_at)
    raise BackendError(
        "SESSION_STORE_UNAVAILABLE",
        "redis-session 세션 ID를 발급할 수 없습니다.",
        status_code=503,
    )


def _decode_record(session_id: str, raw: Any) -> SessionRecord | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
        user_id = payload["user_id"]
        expires_at = datetime.fromisoformat(payload["expires_at"])
        if expires_at.tzinfo is None:
            return None
        expires_at = expires_at.astimezone(timezone.utc)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if not isinstance(user_id, str) or not user_id:
        return None
    if expires_at <= _now():
        return None
    return SessionRecord(session_id, user_id, expires_at)


def get_session(session_id: str, *, client: Any | None = None) -> SessionRecord | None:
    """Read a session without changing its TTL (no sliding renewal)."""

    session_id = _validate_session_id(session_id)
    client = client or redis_client()
    try:
        raw = client.get(_session_key(session_id))
    except Exception as exc:
        raise BackendError(
            "SESSION_STORE_UNAVAILABLE",
            "redis-session 세션을 읽을 수 없습니다.",
            status_code=503,
        ) from exc
    return _decode_record(session_id, raw)


def delete_session(session_id: str, *, client: Any | None = None) -> str | None:
    """Delete one session and remove its sorted-set membership."""

    session_id = _validate_session_id(session_id)
    client = client or redis_client()
    try:
        raw = client.get(_session_key(session_id))
        if not raw:
            return None
        payload = json.loads(raw) if isinstance(raw, str) else raw
        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            return None
        client.eval(
            DELETE_SESSION_LUA,
            2,
            _session_key(session_id),
            _user_sessions_key(user_id),
            session_id,
        )
        return user_id
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(
            "SESSION_STORE_UNAVAILABLE",
            "redis-session 세션을 삭제할 수 없습니다.",
            status_code=503,
        ) from exc


def delete_all_sessions(user_id: str, *, client: Any | None = None) -> int:
    """Atomically delete every session referenced by a user's sorted set."""

    client = client or redis_client()
    try:
        removed = client.eval(
            DELETE_ALL_SESSIONS_LUA,
            2,
            _user_sessions_key(user_id),
            _user_sequence_key(user_id),
            SESSION_KEY_PREFIX,
        )
        return int(removed or 0)
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(
            "SESSION_STORE_UNAVAILABLE",
            "redis-session 전체 세션을 삭제할 수 없습니다.",
            status_code=503,
        ) from exc
