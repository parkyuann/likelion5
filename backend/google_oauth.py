"""구글 로그인(OAuth 2.0 Authorization Code) 연동.

흐름(카카오·네이버와 동일):
    1) 프론트 "구글로 계속하기" → GET /v1/auth/google/login
       → 백엔드가 구글 인가 페이지로 리다이렉트(state 포함)
    2) 사용자가 동의 → 구글이 GET /v1/auth/google/callback?code=...&state=... 로 복귀
    3) 백엔드가 code→토큰 교환, 사용자 정보 조회, 우리 세션 발급
       → 프론트로 리다이렉트(?access_token=...&provider=google)

필요 환경변수(.env):
    GOOGLE_CLIENT_ID      구글 OAuth 클라이언트 ID(필수)
    GOOGLE_CLIENT_SECRET  구글 OAuth 클라이언트 시크릿(필수)
    GOOGLE_REDIRECT_URI   콜백 URL. 구글 콘솔의 '승인된 리디렉션 URI'와 정확히 일치해야 함
                          (예: http://localhost:8000/v1/auth/google/callback)
    FRONTEND_ORIGIN       로그인 완료 후 돌아갈 프론트 주소(예: http://localhost:5173)

구글 클라이언트는 Google Cloud Console → API 및 서비스 → 사용자 인증 정보에서
'OAuth 2.0 클라이언트 ID(웹 애플리케이션)'로 발급한다.
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

router = APIRouter(prefix="/v1/auth/google", tags=["auth"])

_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_SCOPE = "openid email profile"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _require_config() -> tuple[str, str, str]:
    client_id = _env("GOOGLE_CLIENT_ID")
    client_secret = _env("GOOGLE_CLIENT_SECRET")
    redirect_uri = _env("GOOGLE_REDIRECT_URI")
    if not client_id or not client_secret or not redirect_uri:
        raise BackendError(
            "GOOGLE_NOT_CONFIGURED",
            "구글 로그인 환경변수(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI)가 "
            "설정되지 않았습니다.",
            status_code=503,
        )
    return client_id, client_secret, redirect_uri


@router.get("/login")
def google_login() -> RedirectResponse:
    """구글 인가 페이지로 리다이렉트한다. (state는 CSRF 방지용 난수)"""
    client_id, _, redirect_uri = _require_config()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": _SCOPE,
        "state": secrets.token_urlsafe(16),
        # 계정 선택 화면을 항상 보여줘 다른 계정으로도 로그인할 수 있게 한다.
        "prompt": "select_account",
    }
    return RedirectResponse(f"{_AUTHORIZE_URL}?{urlencode(params)}")


@router.get("/callback")
def google_callback(
    code: str = Query(..., description="구글이 발급한 인가 코드"),
    state: str = Query(..., description="인가 시 전달한 state"),
) -> RedirectResponse:
    """인가 코드로 토큰·사용자 정보를 받아 세션을 발급하고 프론트로 복귀한다."""
    client_id, client_secret, redirect_uri = _require_config()
    frontend = _env("FRONTEND_ORIGIN", "http://localhost:5173")

    token_data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }

    with httpx.Client(timeout=10.0) as client:
        # 구글 토큰 엔드포인트는 POST(form-encoded)로 교환한다.
        token_res = client.post(_TOKEN_URL, data=token_data)
        if token_res.status_code != 200:
            raise BackendError(
                "GOOGLE_TOKEN_FAILED",
                "구글 토큰 발급에 실패했습니다.",
                status_code=502,
            )
        access_token = token_res.json().get("access_token")
        if not access_token:
            raise BackendError("GOOGLE_TOKEN_FAILED", "구글 액세스 토큰이 비어 있습니다.", status_code=502)

        me_res = client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if me_res.status_code != 200:
            raise BackendError(
                "GOOGLE_USERINFO_FAILED",
                "구글 사용자 정보를 가져오지 못했습니다.",
                status_code=502,
            )

    profile = me_res.json()
    google_id = str(profile.get("sub") or "")
    email = profile.get("email")  # openid+email 스코프이므로 보통 존재
    display_name = (
        profile.get("name")
        or (email.split("@", 1)[0] if email else None)
        or (f"구글{google_id[-4:]}" if google_id else "구글 사용자")
    )

    user = auth_service.get_or_create_social_user("google", google_id, email, display_name)
    session = auth_service.create_session(user["id"])

    params = urlencode({"access_token": session["access_token"], "provider": "google"})
    return RedirectResponse(f"{frontend}/?{params}")
