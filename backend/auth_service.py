"""Local account authentication backed by PostgreSQL and Redis sessions."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from backend import session_store
from backend.database import RepositoryError, session
from backend.errors import BackendError

try:  # Supplied by the CPU API image; there is no password-hash fallback.
    from argon2 import PasswordHasher, Type
    from argon2.exceptions import VerificationError, VerifyMismatchError
except ImportError:  # pragma: no cover - exercised by a dependency preflight
    class _MissingArgon2Error(Exception):
        pass

    PasswordHasher = None  # type: ignore[assignment,misc]
    Type = None  # type: ignore[assignment,misc]
    VerificationError = _MissingArgon2Error  # type: ignore[assignment,misc]
    VerifyMismatchError = _MissingArgon2Error  # type: ignore[assignment,misc]


PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128
LOCAL_PROVIDER = "local"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

if PasswordHasher is not None and Type is not None:
    _PASSWORD_HASHER = PasswordHasher(
        time_cost=3,
        memory_cost=65536,
        parallelism=4,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )
else:  # pragma: no cover - guarded by _require_password_hasher
    _PASSWORD_HASHER = None

_DUMMY_PASSWORD_HASH: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    if len(normalized) > 320 or not _EMAIL_RE.fullmatch(normalized):
        raise BackendError("INVALID_EMAIL", "올바른 이메일 주소를 입력해주세요.", status_code=422)
    return normalized


def validate_password(password: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise BackendError(
            "WEAK_PASSWORD",
            f"비밀번호는 {PASSWORD_MIN_LENGTH}자 이상이어야 합니다.",
            status_code=422,
        )
    if len(password) > PASSWORD_MAX_LENGTH:
        raise BackendError(
            "PASSWORD_TOO_LONG",
            f"비밀번호는 {PASSWORD_MAX_LENGTH}자를 초과할 수 없습니다.",
            status_code=422,
        )


def _require_password_hasher() -> Any:
    if _PASSWORD_HASHER is None:
        raise BackendError(
            "PASSWORD_HASHER_UNAVAILABLE",
            "Argon2id 비밀번호 검증기를 사용할 수 없습니다.",
            status_code=503,
        )
    return _PASSWORD_HASHER


def hash_password(password: str) -> str:
    validate_password(password)
    return str(_require_password_hasher().hash(password))


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(encoded, str) or not encoded.startswith("$argon2id$"):
        return False
    try:
        return bool(_require_password_hasher().verify(encoded, password))
    except (VerifyMismatchError, VerificationError, ValueError, TypeError):
        return False


def _dummy_hash() -> str:
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = hash_password("not-a-real-user-password")
    return _DUMMY_PASSWORD_HASH


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    return row[key] if key in row else default


def _user_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "primary_email": _row_value(row, "primary_email"),
        "display_name": row["display_name"],
        "status": _row_value(row, "status", "active"),
        "created_at": row["created_at"],
        "last_login_at": _row_value(row, "last_login_at"),
    }


def _database_error(exc: RepositoryError) -> BackendError:
    if exc.sqlstate == "23505":
        return BackendError(
            "EMAIL_ALREADY_REGISTERED",
            "이미 가입된 이메일입니다.",
            status_code=409,
        )
    return BackendError(
        "DATABASE_UNAVAILABLE",
        "application PostgreSQL을 사용할 수 없습니다.",
        status_code=503,
    )


def register_user(email: str, password: str, display_name: str) -> dict[str, Any]:
    """Create a local account without issuing a browser session."""

    normalized_email = normalize_email(email)
    validate_password(password)
    name = display_name.strip() if isinstance(display_name, str) else ""
    if not name or len(name) > 100:
        raise BackendError(
            "INVALID_DISPLAY_NAME",
            "이름은 1자 이상 100자 이하여야 합니다.",
            status_code=422,
        )
    user_id = str(uuid.uuid4())
    created_at = utc_now()
    password_hash = hash_password(password)
    try:
        with session() as connection:
            connection.execute(
                "INSERT INTO users "
                "(id, primary_email, display_name, status, created_at, last_login_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, normalized_email, name, "active", created_at, None),
            )
            connection.execute(
                "INSERT INTO auth_accounts "
                "(user_id, provider, provider_user_id, provider_email, password_hash, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, LOCAL_PROVIDER, user_id, normalized_email, password_hash, created_at),
            )
    except RepositoryError as exc:
        raise _database_error(exc) from exc
    return {
        "id": user_id,
        "primary_email": normalized_email,
        "display_name": name,
        "status": "active",
        "created_at": created_at,
        "last_login_at": None,
    }


def _find_local_account(email: str) -> Mapping[str, Any] | None:
    with session() as connection:
        return connection.execute(
            "SELECT u.id, u.primary_email, u.display_name, u.status, "
            "u.created_at, u.last_login_at, a.password_hash "
            "FROM users AS u "
            "JOIN auth_accounts AS a ON a.user_id = u.id "
            "WHERE a.provider = %s AND lower(a.provider_email) = lower(%s)",
            (LOCAL_PROVIDER, email),
        ).fetchone()


def authenticate(email: str, password: str) -> dict[str, Any]:
    normalized_email = normalize_email(email)
    try:
        row = _find_local_account(normalized_email)
    except RepositoryError as exc:
        raise _database_error(exc) from exc
    encoded = row["password_hash"] if row is not None else _dummy_hash()
    password_matches = verify_password(password, encoded)
    if row is None or not password_matches:
        raise BackendError(
            "INVALID_CREDENTIALS",
            "이메일 또는 비밀번호가 올바르지 않습니다.",
            status_code=401,
        )
    if _row_value(row, "status", "active") != "active":
        raise BackendError(
            "ACCOUNT_INACTIVE",
            "사용할 수 없는 계정입니다.",
            status_code=403,
        )
    now = utc_now()
    try:
        with session() as connection:
            connection.execute(
                "UPDATE users SET last_login_at = %s WHERE id = %s",
                (now, row["id"]),
            )
    except RepositoryError as exc:
        raise _database_error(exc) from exc
    updated = dict(row)
    updated["last_login_at"] = now
    return _user_dict(updated)


def create_session(user_id: str) -> dict[str, Any]:
    record = session_store.create_session(user_id)
    return {"session_id": record.session_id, "expires_at": record.expires_at}


def authenticate_session(session_id: str) -> dict[str, Any]:
    record = session_store.get_session(session_id)
    if record is None:
        raise BackendError(
            "INVALID_SESSION",
            "로그인이 만료되었거나 유효하지 않습니다.",
            status_code=401,
        )
    try:
        with session() as connection:
            row = connection.execute(
                "SELECT id, primary_email, display_name, status, created_at, last_login_at "
                "FROM users WHERE id = %s",
                (record.user_id,),
            ).fetchone()
    except RepositoryError as exc:
        raise _database_error(exc) from exc
    if row is None:
        # A dangling Redis record cannot authenticate and is removed best-effort.
        session_store.delete_session(record.session_id)
        raise BackendError(
            "INVALID_SESSION",
            "로그인이 만료되었거나 유효하지 않습니다.",
            status_code=401,
        )
    if _row_value(row, "status", "active") != "active":
        raise BackendError(
            "ACCOUNT_INACTIVE",
            "사용할 수 없는 계정입니다.",
            status_code=403,
        )
    return {
        "session_id": record.session_id,
        "user": _user_dict(row),
        "expires_at": record.expires_at,
    }


def user_from_session(session_id: str) -> dict[str, Any]:
    return authenticate_session(session_id)["user"]


def revoke_session(session_id: str) -> None:
    if session_store.delete_session(session_id) is None:
        raise BackendError(
            "INVALID_SESSION",
            "로그인이 만료되었거나 유효하지 않습니다.",
            status_code=401,
        )


def revoke_all_sessions(user_id: str) -> None:
    session_store.delete_all_sessions(user_id)
