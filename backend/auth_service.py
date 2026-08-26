"""이메일·비밀번호 인증과 취소 가능한 Bearer 세션 토큰을 관리한다."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.database import session
from backend.errors import BackendError

PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = int(os.getenv("AUTH_PBKDF2_ITERATIONS", "310000"))
SESSION_TTL_HOURS = int(os.getenv("AUTH_SESSION_TTL_HOURS", str(24 * 7)))
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def isoformat(value: datetime) -> str:
    return value.isoformat()


def normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    if len(normalized) > 320 or not _EMAIL_RE.fullmatch(normalized):
        raise BackendError("INVALID_EMAIL", "올바른 이메일 주소를 입력해주세요.", status_code=422)
    return normalized


def validate_password(password: str) -> None:
    if len(password) < 8:
        raise BackendError(
            "WEAK_PASSWORD",
            "비밀번호는 8자 이상이어야 합니다.",
            status_code=422,
        )
    if len(password) > 128:
        raise BackendError(
            "PASSWORD_TOO_LONG",
            "비밀번호는 128자를 초과할 수 없습니다.",
            status_code=422,
        )


def hash_password(password: str, *, iterations: int = PASSWORD_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        (
            PASSWORD_ALGORITHM,
            str(iterations),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


_DUMMY_PASSWORD_HASH = hash_password("not-a-real-user-password")


def _user_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "created_at": row["created_at"],
    }


def register_user(email: str, password: str, display_name: str | None = None) -> dict[str, Any]:
    normalized_email = normalize_email(email)
    validate_password(password)
    name = (display_name or normalized_email.split("@", 1)[0]).strip()
    if not name or len(name) > 80:
        raise BackendError(
            "INVALID_DISPLAY_NAME",
            "이름은 1자 이상 80자 이하여야 합니다.",
            status_code=422,
        )
    user_id = str(uuid.uuid4())
    created_at = isoformat(utc_now())
    try:
        with session() as connection:
            connection.execute(
                "INSERT INTO users(id, email, display_name, password_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, normalized_email, name, hash_password(password), created_at),
            )
    except sqlite3.IntegrityError as exc:
        raise BackendError(
            "EMAIL_ALREADY_REGISTERED",
            "이미 가입된 이메일입니다.",
            status_code=409,
        ) from exc
    return {
        "id": user_id,
        "email": normalized_email,
        "display_name": name,
        "created_at": created_at,
    }


def authenticate(email: str, password: str) -> dict[str, Any]:
    normalized_email = normalize_email(email)
    with session() as connection:
        row = connection.execute(
            "SELECT id, email, display_name, password_hash, created_at FROM users WHERE email = ?",
            (normalized_email,),
        ).fetchone()
    encoded = row["password_hash"] if row is not None else _DUMMY_PASSWORD_HASH
    password_matches = verify_password(password, encoded)
    if row is None or not password_matches:
        raise BackendError(
            "INVALID_CREDENTIALS",
            "이메일 또는 비밀번호가 올바르지 않습니다.",
            status_code=401,
        )
    return _user_dict(row)


def get_or_create_social_user(
    provider: str,
    provider_uid: str,
    email: str | None,
    display_name: str | None,
) -> dict[str, Any]:
    """소셜 로그인(카카오/네이버 등) 사용자를 조회하거나 새로 만든다.

    이메일 동의가 없으면 ``{provider}_{uid}@social.local`` 형태로 대체 식별자를
    만든다. 비밀번호가 없으므로 임의의 강한 문자열을 해시해 저장한다(이메일
    로그인으로는 로그인 불가).
    """
    if email:
        normalized_email = normalize_email(email)
    else:
        normalized_email = f"{provider}_{provider_uid}@social.local"

    with session() as connection:
        row = connection.execute(
            "SELECT id, email, display_name, password_hash, created_at "
            "FROM users WHERE email = ?",
            (normalized_email,),
        ).fetchone()
        if row is not None:
            return _user_dict(row)

        name = (display_name or normalized_email.split("@", 1)[0]).strip()[:80] or "사용자"
        user_id = str(uuid.uuid4())
        created_at = isoformat(utc_now())
        random_password = secrets.token_urlsafe(24)
        connection.execute(
            "INSERT INTO users(id, email, display_name, password_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, normalized_email, name, hash_password(random_password), created_at),
        )
    return {
        "id": user_id,
        "email": normalized_email,
        "display_name": name,
        "created_at": created_at,
    }


def create_session(user_id: str) -> dict[str, Any]:
    raw_token = "l5_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = utc_now()
    expires_at = now + timedelta(hours=SESSION_TTL_HOURS)
    with session() as connection:
        connection.execute(
            "DELETE FROM auth_sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL",
            (isoformat(now),),
        )
        connection.execute(
            "INSERT INTO auth_sessions(id, user_id, token_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                user_id,
                token_hash,
                isoformat(now),
                isoformat(expires_at),
            ),
        )
    return {
        "access_token": raw_token,
        "token_type": "bearer",
        "expires_at": isoformat(expires_at),
    }


def user_from_token(raw_token: str) -> dict[str, Any]:
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with session() as connection:
        row = connection.execute(
            "SELECT u.id, u.email, u.display_name, u.created_at "
            "FROM auth_sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?",
            (token_hash, isoformat(utc_now())),
        ).fetchone()
    if row is None:
        raise BackendError(
            "INVALID_AUTH_TOKEN",
            "로그인이 만료되었거나 유효하지 않습니다.",
            status_code=401,
        )
    return _user_dict(row)


def revoke_session(raw_token: str) -> None:
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with session() as connection:
        cursor = connection.execute(
            "UPDATE auth_sessions SET revoked_at = ? "
            "WHERE token_hash = ? AND revoked_at IS NULL",
            (isoformat(utc_now()), token_hash),
        )
    if cursor.rowcount == 0:
        raise BackendError(
            "INVALID_AUTH_TOKEN",
            "로그인이 만료되었거나 유효하지 않습니다.",
            status_code=401,
        )
