"""app.py — 뉴스 사실검증 백엔드 (FastAPI).

KOSIS 검증 파이프라인을 웹에서 호출할 수 있게 감싼 서버.
기사 검증은 develop 파이프라인(src/develop) 경로로 일원화되어 있다.

  POST /v1/verify/develop  기사 본문 검증(src/develop 파이프라인)
  POST /v1/analyze         입력 라우팅(질의→KOSIS MCP / 기사→검증)

실행(저장소 루트에서):
    ./.venv/Scripts/python.exe -m uvicorn backend.app:app --reload --port 8000

켜지면 http://localhost:8000/docs 에서 클릭만으로 시험할 수 있다.
전제: 로컬 Qdrant(6333) 실행 + .env 의 API 키. (docs/BACKEND_GUIDE.md 6번 참고)
"""
from __future__ import annotations

import os
import sqlite3
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import (
    analysis_service,
    auth_service,
    conversation_service,
    develop_verify_service,
    favorites_service,
    google_oauth,
    image_ocr_service,
    kakao_oauth,
    naver_oauth,
    table_catalog_service,
)
from backend.auth_dependencies import bearer_scheme, current_user, optional_user
from backend.errors import BackendError

app = FastAPI(
    title="뉴스 사실검증 API",
    description="뉴스 수치 주장을 KOSIS 통계로 검증하는 백엔드",
    version="0.1.0",
)

_cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 소셜 로그인(OAuth) 라우터: /v1/auth/{kakao,naver}/login, /callback
app.include_router(kakao_oauth.router)
app.include_router(naver_oauth.router)
app.include_router(google_oauth.router)


# --- 요청 본문 스키마 (프론트가 보낼 JSON 모양) ------------------------------
class AnalyzeRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=100_000,
                      description="간단한 통계 질문, 기사 URL 또는 기사 본문")
    input_type: Literal["auto", "query", "url", "article"] = Field(
        "auto", description="명시적 입력 유형. auto일 때 백엔드가 판별"
    )
    max_claims: int = Field(10, ge=1, le=50)
    explain: bool = False
    focus_question: str = Field(
        "", max_length=1000,
        description="기사·URL에서 우선 확인할 사용자 질문(선택)",
    )
    conversation_id: str | None = Field(
        None, description="로그인 사용자가 기존 대화를 이어갈 때 전달하는 대화 ID"
    )


class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str | None = Field(None, min_length=1, max_length=80)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=128)


class ConversationCreateRequest(BaseModel):
    title: str = Field("새 대화", min_length=1, max_length=200)


class FavoriteCreateRequest(BaseModel):
    table_key: str = Field(..., min_length=1, max_length=120,
                           description="즐겨찾기할 통계표 키 (org_id:tbl_id)")


@app.exception_handler(BackendError)
def backend_error_handler(_: Request, exc: BackendError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict(), headers=headers)


@app.exception_handler(sqlite3.Error)
def database_error_handler(_: Request, __: sqlite3.Error) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error_code": "DATABASE_UNAVAILABLE",
            "message": "사용자 또는 대화 저장소를 사용할 수 없습니다.",
        },
    )


def _record_error(user_id: str, conversation_id: str, exc: Exception) -> None:
    if isinstance(exc, BackendError):
        content = exc.message
        payload = exc.to_dict()
    else:
        content = "요청 처리 중 오류가 발생했습니다."
        payload = {"error_code": "INTERNAL_ERROR", "message": content}
    conversation_service.add_message(
        user_id,
        conversation_id,
        role="assistant",
        kind="error",
        content=content,
        payload=payload,
    )


# --- 엔드포인트 --------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    """서버가 살아있는지 확인용(프론트 연결 테스트에 유용)."""
    return {"status": "ok"}


@app.post("/v1/auth/register", status_code=201)
def register(req: RegisterRequest) -> dict:
    """이메일 계정을 생성한다. 비밀번호 원문은 저장하지 않는다."""
    user = auth_service.register_user(req.email, req.password, req.display_name)
    session = auth_service.create_session(user["id"])
    return {"user": user, **session}


@app.post("/v1/auth/login")
def login(req: LoginRequest) -> dict:
    """이메일·비밀번호를 확인하고 취소 가능한 Bearer 토큰을 발급한다."""
    user = auth_service.authenticate(req.email, req.password)
    session = auth_service.create_session(user["id"])
    return {"user": user, **session}


@app.post("/v1/auth/logout", status_code=204)
def logout(
    credentials=Depends(bearer_scheme),
    _: dict = Depends(current_user),
) -> Response:
    """현재 Bearer 토큰을 폐기한다."""
    auth_service.revoke_session(credentials.credentials)
    return Response(status_code=204)


@app.get("/v1/auth/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {"user": user}


@app.post("/v1/conversations", status_code=201)
def create_conversation(
    req: ConversationCreateRequest,
    user: dict = Depends(current_user),
) -> dict:
    return conversation_service.create_conversation(user["id"], req.title)


@app.get("/v1/conversations")
def list_conversations(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict = Depends(current_user),
) -> dict:
    return conversation_service.list_conversations(user["id"], limit=limit, offset=offset)


@app.get("/v1/conversations/{conversation_id}")
def get_conversation(conversation_id: str, user: dict = Depends(current_user)) -> dict:
    return conversation_service.get_conversation(user["id"], conversation_id)


@app.delete("/v1/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, user: dict = Depends(current_user)) -> Response:
    conversation_service.delete_conversation(user["id"], conversation_id)
    return Response(status_code=204)


@app.get("/v1/tables")
def search_tables(
    q: str = Query("", max_length=200, description="검색어(비우면 제목순)"),
    org: str = Query("", max_length=200, description="작성 기관 필터"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict | None = Depends(optional_user),
) -> dict:
    """KOSIS 통계표 카탈로그를 통계표 이름으로 검색한다. 로그인 시 즐겨찾기 여부를 표시."""
    result = table_catalog_service.search_tables(
        q,
        limit=limit,
        offset=offset,
        organization=org,
    )
    if user is not None:
        keys = favorites_service.favorite_keys(user["id"])
        for item in result["items"]:
            item["favorited"] = item["table_key"] in keys
    return result


@app.get("/v1/favorites")
def list_favorites(user: dict = Depends(current_user)) -> dict:
    return favorites_service.list_favorites(user["id"])


@app.post("/v1/favorites", status_code=201)
def add_favorite(req: FavoriteCreateRequest, user: dict = Depends(current_user)) -> dict:
    return favorites_service.add_favorite(user["id"], req.table_key)


@app.delete("/v1/favorites/{table_key}", status_code=204)
def remove_favorite(table_key: str, user: dict = Depends(current_user)) -> Response:
    favorites_service.remove_favorite(user["id"], table_key)
    return Response(status_code=204)


class DevelopVerifyRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=100_000, description="검증할 기사 본문 전체")
    title: str = Field("", max_length=500, description="기사 제목(선택)")
    date: str = Field("", max_length=40, description="작성일 YYYY-MM-DD(시점 해석, 선택)")
    conversation_id: str | None = Field(
        None, description="로그인 사용자가 기존 대화를 이어갈 때 전달하는 대화 ID"
    )


@app.post("/v1/verify/develop")
def verify_develop(
    req: DevelopVerifyRequest, user: dict | None = Depends(optional_user)
) -> dict:
    """develop 배포 파이프라인(run_trace)으로 기사 본문을 검증한다.

    인프라(Qdrant·인코더·리랭커 URL)가 연결돼 있으면 라이브 판정까지, 없으면
    구조화·라우팅까지 수행하고 프론트 표시 계약(results=segments)으로 반환한다.
    로그인 사용자는 요청/결과가 대화 기록에 저장된다.
    """
    if req.conversation_id and user is None:
        raise BackendError("AUTH_REQUIRED", "대화를 이어가려면 로그인이 필요합니다.", status_code=401)

    def _persist_user_message() -> str:
        conversation_id = conversation_service.ensure_conversation(
            user["id"], req.conversation_id, req.text
        )
        conversation_service.add_message(
            user["id"],
            conversation_id,
            role="user",
            kind="article",
            content=req.text,
            payload={"title": req.title, "date": req.date},
        )
        return conversation_id

    try:
        result = develop_verify_service.verify_article_develop(
            req.text, title=req.title, date=req.date
        )
    except BackendError as exc:
        if user is not None:
            conversation_id = _persist_user_message()
            _record_error(user["id"], conversation_id, exc)
            exc.detail = {**(exc.detail or {}), "conversation_id": conversation_id}
        raise
    except Exception as exc:
        if user is not None:
            _record_error(user["id"], _persist_user_message(), exc)
        raise

    if user is not None:
        conversation_id = _persist_user_message()
        conversation_service.add_message(
            user["id"],
            conversation_id,
            role="assistant",
            kind="article",
            content=conversation_service.assistant_content(result),
            payload=result,
        )
        result = {**result, "conversation_id": conversation_id}
    return result


@app.post("/v1/analyze")
def analyze(req: AnalyzeRequest, user: dict | None = Depends(optional_user)) -> dict:
    """간단한 통계 질의는 KOSIS MCP, 기사 URL/본문은 기사 검증 경로로 보낸다.

    로그인 사용자의 모든 대화를 기록한다 — 성공한 검증, 오류로 끝난 질문,
    범위 밖(OUT_OF_SCOPE) 잡담까지 전부 대화 기록에 남긴다.
    """
    if req.conversation_id and user is None:
        raise BackendError("AUTH_REQUIRED", "대화를 이어가려면 로그인이 필요합니다.", status_code=401)

    # 사용자 입력을 대화에 남기고 conversation_id를 돌려준다.
    def _persist_user_message() -> str:
        conversation_id = conversation_service.ensure_conversation(
            user["id"], req.conversation_id, req.text
        )
        conversation_service.add_message(
            user["id"],
            conversation_id,
            role="user",
            kind=req.input_type,
            content=req.text,
            payload={
                "input_type": req.input_type,
                "max_claims": req.max_claims,
                "explain": req.explain,
                "focus_question": req.focus_question.strip(),
            },
        )
        return conversation_id

    try:
        result = analysis_service.analyze(
            req.text,
            input_type=req.input_type,
            max_claims=req.max_claims,
            explain=req.explain,
        )
        if req.focus_question.strip():
            result = {**result, "focus_question": req.focus_question.strip()}
    except BackendError as exc:
        # 모든 대화를 기록한다(잡담·OUT_OF_SCOPE 포함).
        if user is not None:
            conversation_id = _persist_user_message()
            _record_error(user["id"], conversation_id, exc)
            # 프론트가 목록을 갱신·대화를 이어갈 수 있게 conversation_id를 실어준다.
            exc.detail = {**(exc.detail or {}), "conversation_id": conversation_id}
        raise
    except Exception as exc:
        if user is not None:
            _record_error(user["id"], _persist_user_message(), exc)
        raise

    if user is not None:
        conversation_id = _persist_user_message()
        conversation_service.add_message(
            user["id"],
            conversation_id,
            role="assistant",
            kind=str(result.get("type") or "result"),
            content=conversation_service.assistant_content(result),
            payload=result,
        )
        result = {**result, "conversation_id": conversation_id}
    return result


@app.post("/v1/analyze/image")
def analyze_image(
    file: UploadFile = File(..., description="OCR할 PNG, JPEG 또는 WebP 이미지"),
    conversation_id: str | None = Form(None),
    focus_question: str = Form(""),
    user: dict | None = Depends(optional_user),
) -> dict:
    """업로드 이미지의 전체 텍스트를 OCR해 ArticleDocument로 반환한다."""
    if conversation_id and user is None:
        raise BackendError("AUTH_REQUIRED", "대화를 이어가려면 로그인이 필요합니다.", status_code=401)
    data = file.file.read(image_ocr_service.MAX_UPLOAD_BYTES + 1)

    def _persist_user_image() -> str:
        conversation_id_ = conversation_service.ensure_conversation(
            user["id"], conversation_id, file.filename or "이미지 분석"
        )
        conversation_service.add_message(
            user["id"],
            conversation_id_,
            role="user",
            kind="image",
            content=f"[이미지] {file.filename or 'upload'}",
            payload={
                "filename": file.filename,
                "content_type": file.content_type,
                "focus_question": focus_question.strip()[:1000],
            },
        )
        return conversation_id_

    try:
        result = image_ocr_service.prepare_image_article(
            data,
            filename=file.filename,
            declared_content_type=file.content_type,
        )
        if focus_question.strip():
            result = {**result, "focus_question": focus_question.strip()[:1000]}
    except Exception as exc:
        if user is not None:
            _record_error(user["id"], _persist_user_image(), exc)
        raise

    if user is not None:
        saved_conversation_id = _persist_user_image()
        conversation_service.add_message(
            user["id"],
            saved_conversation_id,
            role="assistant",
            kind="article_document",
            content=conversation_service.assistant_content(result),
            payload=result,
        )
        result = {**result, "conversation_id": saved_conversation_id}
    return result
