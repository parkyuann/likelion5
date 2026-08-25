"""카카오 로그인(OAuth 2.0 Authorization Code) 연동.

흐름:
    1) 프론트 "카카오로 시작하기" → GET /v1/auth/kakao/login
       → 백엔드가 카카오 인가 페이지로 리다이렉트
    2) 사용자가 동의 → 카카오가 GET /v1/auth/kakao/callback?code=... 로 복귀
    3) 백엔드가 code→토큰 교환, 사용자 정보 조회, 우리 세션 발급
       → 프론트로 리다이렉트(?access_token=... 를 붙여서)

필요 환경변수(.env):
    KAKAO_REST_API_KEY   카카오 앱의 REST API 키(필수)
    KAKAO_REDIRECT_URI   콜백 URL. 카카오 콘솔의 Redirect URI와 정확히 일치해야 함
                         (예: http://localhost:8000/v1/auth/kakao/callback)
    KAKAO_CLIENT_SECRET  (선택) 보안 탭에서 발급한 Client Secret
    FRONTEND_ORIGIN      로그인 완료 후 돌아갈 프론트 주소(예: http://localhost:5173)
"""
from __future__ import annotations

import os
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse

from backend import auth_service
from backend.errors import BackendError

router = APIRouter(prefix="/v1/auth/kakao", tags=["auth"])

_AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
_USERINFO_URL = "https://kapi.kakao.com/v2/user/me"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _require_config() -> tuple[str, str]:
    client_id = _env("KAKAO_REST_API_KEY")
    redirect_uri = _env("KAKAO_REDIRECT_URI")
    if not client_id or not redirect_uri:
        raise BackendError(
            "KAKAO_NOT_CONFIGURED",
            "카카오 로그인 환경변수(KAKAO_REST_API_KEY, KAKAO_REDIRECT_URI)가 설정되지 않았습니다.",
            status_code=503,
        )
    return client_id, redirect_uri


@router.get("/login")
def kakao_login() -> RedirectResponse:
    """카카오 인가 페이지로 리다이렉트한다."""
    client_id, redirect_uri = _require_config()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }
    return RedirectResponse(f"{_AUTHORIZE_URL}?{urlencode(params)}")


@router.get("/callback")
def kakao_callback(code: str = Query(..., description="카카오가 발급한 인가 코드")) -> RedirectResponse:
    """인가 코드로 토큰·사용자 정보를 받아 세션을 발급하고 프론트로 복귀한다."""
    client_id, redirect_uri = _require_config()
    client_secret = _env("KAKAO_CLIENT_SECRET")
    frontend = _env("FRONTEND_ORIGIN", "http://localhost:5173")

    token_data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    if client_secret:
        token_data["client_secret"] = client_secret

    with httpx.Client(timeout=10.0) as client:
        token_res = client.post(
            _TOKEN_URL,
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        )
        if token_res.status_code != 200:
            raise BackendError(
                "KAKAO_TOKEN_FAILED",
                "카카오 토큰 발급에 실패했습니다.",
                status_code=502,
            )
        access_token = token_res.json().get("access_token")
        if not access_token:
            raise BackendError("KAKAO_TOKEN_FAILED", "카카오 액세스 토큰이 비어 있습니다.", status_code=502)

        me_res = client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if me_res.status_code != 200:
            raise BackendError(
                "KAKAO_USERINFO_FAILED",
                "카카오 사용자 정보를 가져오지 못했습니다.",
                status_code=502,
            )

    me = me_res.json()
    kakao_id = str(me.get("id") or "")
    account = me.get("kakao_account") or {}
    profile = account.get("profile") or {}
    email = account.get("email")  # 동의 안 했으면 없음
    nickname = profile.get("nickname") or (f"카카오{kakao_id[-4:]}" if kakao_id else "카카오 사용자")

    user = auth_service.get_or_create_social_user("kakao", kakao_id, email, nickname)
    session = auth_service.create_session(user["id"])

    params = urlencode({"access_token": session["access_token"], "provider": "kakao"})
    return RedirectResponse(f"{frontend}/?{params}")
