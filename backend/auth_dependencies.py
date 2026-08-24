"""FastAPI Bearer 인증 의존성."""

from __future__ import annotations

from typing import Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.auth_service import user_from_token
from backend.errors import BackendError

bearer_scheme = HTTPBearer(auto_error=False)


def optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, Any] | None:
    if credentials is None:
        return None
    if credentials.scheme.casefold() != "bearer":
        raise BackendError("INVALID_AUTH_TOKEN", "Bearer 인증이 필요합니다.", status_code=401)
    return user_from_token(credentials.credentials)


def current_user(user: dict[str, Any] | None = Depends(optional_user)) -> dict[str, Any]:
    if user is None:
        raise BackendError("AUTH_REQUIRED", "로그인이 필요합니다.", status_code=401)
    return user
