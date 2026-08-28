"""FastAPI dependencies for the opaque server-session cookie."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request

from backend import auth_service, session_store
from backend.errors import BackendError


def session_id_from_request(request: Request) -> str:
    session_id = request.cookies.get(session_store.SESSION_COOKIE_NAME)
    if not session_id:
        raise BackendError("AUTH_REQUIRED", "로그인이 필요합니다.", status_code=401)
    return session_id


def current_session(
    session_id: str = Depends(session_id_from_request),
) -> dict[str, Any]:
    return auth_service.authenticate_session(session_id)


def optional_user(request: Request) -> dict[str, Any] | None:
    """Read only the session cookie; other client credentials are ignored."""

    session_id = request.cookies.get(session_store.SESSION_COOKIE_NAME)
    if not session_id:
        return None
    return auth_service.authenticate_session(session_id)["user"]


def current_user(
    authenticated: dict[str, Any] = Depends(current_session),
) -> dict[str, Any]:
    return authenticated["user"]
