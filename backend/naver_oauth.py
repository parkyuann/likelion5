"""네이버 로그인(OAuth 2.0 Authorization Code) 연동.

흐름(카카오와 동일):
    1) 프론트 "네이버로 시작하기" → GET /v1/auth/naver/login
       → 백엔드가 네이버 인가 페이지로 리다이렉트(state 포함)
    2) 사용자가 동의 → 네이버가 GET /v1/auth/naver/callback?code=...&state=... 로 복귀
    3) 백엔드가 code→토큰 교환, 사용자 정보 조회, 우리 세션 발급
       → 프론트로 리다이렉트(?access_token=...&provider=naver)

필요 환경변수(.env):
    NAVER_CLIENT_ID      네이버 앱 Client ID(필수)
    NAVER_CLIENT_SECRET  네이버 앱 Client Secret(필수)
    NAVER_REDIRECT_URI   콜백 URL. 네이버 콘솔의 Callback URL과 정확히 일치해야 함
                         (예: http://localhost:8000/v1/auth/naver/callback)
    FRONTEND_ORIGIN      로그인 완료 후 돌아갈 프론트 주소(예: http://localhost:5173)
"""
from __future__ import annotations

import os
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse

from backend import auth_service
from backend.errors import BackendError

router = APIRouter(prefix="/v1/auth/naver", tags=["auth"])

_AUTHORIZE_URL = "https://nid.naver.com/oauth2.0/authorize"
_TOKEN_URL = "https://nid.naver.com/oauth2.0/token"
_USERINFO_URL = "https://openapi.naver.com/v1/nid/me"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _require_config() -> tuple[str, str, str]:
    client_id = _env("NAVER_CLIENT_ID")
    client_secret = _env("NAVER_CLIENT_SECRET")
    redirect_uri = _env("NAVER_REDIRECT_URI")
    if not client_id or not client_secret or not redirect_uri:
        raise BackendError(
            "NAVER_NOT_CONFIGURED",
            "네이버 로그인 환경변수(NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, NAVER_REDIRECT_URI)가 "
            "설정되지 않았습니다.",
            status_code=503,
        )
    return client_id, client_secret, redirect_uri


@router.get("/login")
def naver_login() -> RedirectResponse:
    """네이버 인가 페이지로 리다이렉트한다. (state는 CSRF 방지용 난수)"""
    client_id, _, redirect_uri = _require_config()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": secrets.token_urlsafe(16),
    }
    return RedirectResponse(f"{_AUTHORIZE_URL}?{urlencode(params)}")


@router.get("/callback")
def naver_callback(
    code: str = Query(..., description="네이버가 발급한 인가 코드"),
    state: str = Query(..., description="인가 시 전달한 state"),
) -> RedirectResponse:
    """인가 코드로 토큰·사용자 정보를 받아 세션을 발급하고 프론트로 복귀한다."""
    client_id, client_secret, _ = _require_config()
    frontend = _env("FRONTEND_ORIGIN", "http://localhost:5173")

    token_params = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "state": state,
    }

    with httpx.Client(timeout=10.0) as client:
        token_res = client.get(_TOKEN_URL, params=token_params)
        if token_res.status_code != 200:
            raise BackendError(
                "NAVER_TOKEN_FAILED",
                "네이버 토큰 발급에 실패했습니다.",
                status_code=502,
            )
        access_token = token_res.json().get("access_token")
        if not access_token:
            raise BackendError("NAVER_TOKEN_FAILED", "네이버 액세스 토큰이 비어 있습니다.", status_code=502)

        me_res = client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if me_res.status_code != 200:
            raise BackendError(
                "NAVER_USERINFO_FAILED",
                "네이버 사용자 정보를 가져오지 못했습니다.",
                status_code=502,
            )

    body = me_res.json()
    profile = body.get("response") or {}
    naver_id = str(profile.get("id") or "")
    email = profile.get("email")  # 동의 안 했으면 없음
    nickname = (
        profile.get("nickname")
        or profile.get("name")
        or (f"네이버{naver_id[-4:]}" if naver_id else "네이버 사용자")
    )

    user = auth_service.get_or_create_social_user("naver", naver_id, email, nickname)
    session = auth_service.create_session(user["id"])

    params = urlencode({"access_token": session["access_token"], "provider": "naver"})
    return RedirectResponse(f"{frontend}/?{params}")
