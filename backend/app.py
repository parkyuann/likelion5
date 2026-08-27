"""FastAPI application overlay: cookie auth and fail-closed pipeline routes."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend import auth_service, conversation_service, favorites_service, session_store, table_catalog_service
from backend.auth_dependencies import current_session, current_user, optional_user
from backend.database import RepositoryError
from backend.errors import BackendError
from backend.runtime_gate import (
    require_application_product_state,
    require_csrf,
    raise_pipeline_pending,
    require_pipeline_runtime,
    PIPELINE_IMAGE_PENDING,
    PIPELINE_NATURAL_QUERY_PENDING,
    PIPELINE_URL_PENDING,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignupRequest(StrictModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=12, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=100)


class LoginRequest(StrictModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=128)


class UserDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    primary_email: str | None = None
    display_name: str
    status: Literal["active", "suspended", "withdrawn"]
    created_at: datetime
    last_login_at: datetime | None = None


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: UserDTO


class AuthSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: UserDTO
    expires_at: datetime


class TableCandidate(BaseModel):
    model_config = ConfigDict(extra="allow")

    table_key: str
    release_id: str
    source: str
    score: float
    org_id: str | None
    tbl_id: str | None
    org_name: str | None
    tbl_name: str | None
    status: str | None
    send_de: str | None
    kosis_url: str
    evidence: dict[str, Any]
    metadata: dict[str, Any]


class TableOrganization(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    count: int


class TableSearchResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    release_id: str
    items: list[TableCandidate]
    total: int
    total_relation: Literal["eq", "gte"]
    limit: int
    offset: int
    organizations: list[TableOrganization]
    organizations_relation: Literal["eq", "gte"]


class ConversationCreateRequest(StrictModel):
    title: str = Field("새 대화", min_length=1, max_length=200)


class FavoriteCreateRequest(StrictModel):
    table_key: str = Field(..., min_length=1, max_length=120)


class AnalyzeRequest(StrictModel):
    text: str = Field(..., min_length=1, max_length=100_000)
    input_type: Literal["auto", "query", "url", "article"] = "auto"
    date: str | None = Field("", max_length=40)
    date_source: Literal["user_feedback", "url_metadata", "api_request"] | None = None
    max_claims: int = Field(10, ge=1, le=50)
    explain: bool = False
    focus_question: str = Field("", max_length=1000)
    conversation_id: str | None = None


class DevelopVerifyRequest(StrictModel):
    text: str = Field(..., min_length=1, max_length=100_000)
    title: str = Field("", max_length=500)
    date: str | None = Field("", max_length=40)
    date_source: Literal["user_feedback", "url_metadata", "api_request"] | None = None
    conversation_id: str | None = None


app = FastAPI(title="뉴스 사실검증 API", version="0.2.0")
_cors_origins = [
    origin.strip()
    for origin in (
        os.getenv("CORS_ALLOWED_ORIGINS", "")
        or os.getenv("AUTH_ALLOWED_ORIGINS", "")
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Origin", "Referer", "Sec-Fetch-Site"],
)


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


@app.exception_handler(BackendError)
def backend_error_handler(_: Request, exc: BackendError) -> JSONResponse:
    return _error(exc.code, exc.message, exc.status_code)


@app.exception_handler(RequestValidationError)
def validation_error_handler(_: Request, __: RequestValidationError) -> JSONResponse:
    return _error("VALIDATION_ERROR", "요청 형식이 올바르지 않습니다.", 422)


@app.exception_handler(RepositoryError)
def repository_error_handler(_: Request, __: RepositoryError) -> JSONResponse:
    return _error("DATABASE_UNAVAILABLE", "application PostgreSQL을 사용할 수 없습니다.", 503)


def _set_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(session_store.SESSION_COOKIE_NAME, session_id, max_age=604800, path="/", secure=True, httponly=True, samesite="lax")


def _clear_cookie(response: Response) -> None:
    response.set_cookie(session_store.SESSION_COOKIE_NAME, "", max_age=0, expires=0, path="/", secure=True, httponly=True, samesite="lax")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/signup", status_code=201, response_model=UserResponse, dependencies=[Depends(require_csrf)])
def signup(req: SignupRequest) -> dict:
    return {"user": auth_service.register_user(req.email, req.password, req.display_name)}


@app.post("/api/auth/login", response_model=AuthSessionResponse, dependencies=[Depends(require_csrf)])
def login(req: LoginRequest, response: Response) -> dict:
    user = auth_service.authenticate(req.email, req.password)
    record = auth_service.create_session(user["id"])
    _set_cookie(response, record["session_id"])
    return {"user": user, "expires_at": record["expires_at"]}


@app.get("/api/auth/me", response_model=AuthSessionResponse)
def me(authenticated: dict = Depends(current_session)) -> dict:
    return {"user": authenticated["user"], "expires_at": authenticated["expires_at"]}


@app.post("/api/auth/logout", status_code=204, dependencies=[Depends(require_csrf)])
def logout(authenticated: dict = Depends(current_session)) -> Response:
    auth_service.revoke_session(authenticated["session_id"])
    response = Response(status_code=204)
    _clear_cookie(response)
    return response


@app.post("/api/auth/logout-all", status_code=204, dependencies=[Depends(require_csrf)])
def logout_all(authenticated: dict = Depends(current_session)) -> Response:
    auth_service.revoke_all_sessions(authenticated["user"]["id"])
    response = Response(status_code=204)
    _clear_cookie(response)
    return response


@app.post("/api/v1/conversations", status_code=201, dependencies=[Depends(require_csrf), Depends(require_application_product_state)])
def create_conversation(req: ConversationCreateRequest, user: dict = Depends(current_user)) -> dict:
    return conversation_service.create_conversation(user["id"], req.title)


@app.get("/api/v1/conversations", dependencies=[Depends(require_application_product_state)])
def list_conversations(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), user: dict = Depends(current_user)) -> dict:
    return conversation_service.list_conversations(user["id"], limit=limit, offset=offset)


@app.get("/api/v1/conversations/{conversation_id}", dependencies=[Depends(require_application_product_state)])
def get_conversation(conversation_id: str, user: dict = Depends(current_user)) -> dict:
    return conversation_service.get_conversation(user["id"], conversation_id)


@app.delete("/api/v1/conversations/{conversation_id}", status_code=204, dependencies=[Depends(require_csrf), Depends(require_application_product_state)])
def delete_conversation(conversation_id: str, user: dict = Depends(current_user)) -> Response:
    conversation_service.delete_conversation(user["id"], conversation_id)
    return Response(status_code=204)


# @app.get("/api/v1/tables") route inventory marker; response_model is attached below.
@app.get("/api/v1/tables", response_model=TableSearchResponse)
def search_tables(q: str = Query("", max_length=200), org: str = Query("", max_length=200), limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict:
    return table_catalog_service.search_tables(q, limit=limit, offset=offset, organization=org)


@app.get("/api/v1/favorites", dependencies=[Depends(require_application_product_state)])
def list_favorites(user: dict = Depends(current_user)) -> dict:
    return favorites_service.list_favorites(user["id"])


@app.post("/api/v1/favorites", status_code=201, dependencies=[Depends(require_csrf), Depends(require_application_product_state)])
def add_favorite(req: FavoriteCreateRequest, user: dict = Depends(current_user)) -> dict:
    return favorites_service.add_favorite(user["id"], req.table_key)


@app.delete("/api/v1/favorites/{table_key}", status_code=204, dependencies=[Depends(require_csrf), Depends(require_application_product_state)])
def remove_favorite(table_key: str, user: dict = Depends(current_user)) -> Response:
    favorites_service.remove_favorite(user["id"], table_key)
    return Response(status_code=204)


@app.post("/api/v1/verify/develop", dependencies=[Depends(require_csrf)])
def verify_develop(req: DevelopVerifyRequest, user: dict | None = Depends(optional_user)) -> dict:
    del user
    from backend.develop_verify_service import (
        _looks_like_question,
        article_date_required_response,
        normalize_article_date,
        verify_article_develop,
    )

    if _looks_like_question(req.text.strip()):
        return {"type": "not_article", "reason": "question"}
    normalized_date = normalize_article_date(req.date, req.date_source)
    if normalized_date is None:
        return article_date_required_response()
    date, date_source = normalized_date
    require_pipeline_runtime()
    return verify_article_develop(req.text, title=req.title, date=date, date_source=date_source)


def _looks_like_url(value: str) -> bool:
    from urllib.parse import urlsplit

    parsed = urlsplit(value.strip())
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


@app.post("/api/v1/analyze", dependencies=[Depends(require_csrf)])
def analyze(req: AnalyzeRequest, user: dict | None = Depends(optional_user)) -> dict:
    del user
    if req.input_type == "url" or (req.input_type == "auto" and _looks_like_url(req.text)):
        raise_pipeline_pending(PIPELINE_URL_PENDING, "URL 기사 추출 경로가 아직 연결되지 않았습니다.")
    if req.input_type != "article":
        raise_pipeline_pending(PIPELINE_NATURAL_QUERY_PENDING, "자연어·자동 질의 경로가 아직 연결되지 않았습니다.")

    from backend.develop_verify_service import article_date_required_response, normalize_article_date, verify_article_develop

    normalized_date = normalize_article_date(req.date, req.date_source)
    if normalized_date is None:
        return article_date_required_response()
    date, date_source = normalized_date
    require_pipeline_runtime()
    return verify_article_develop(req.text, date=date, date_source=date_source)


@app.post("/api/v1/analyze/image", dependencies=[Depends(require_csrf)])
def analyze_image(file: UploadFile = File(...), conversation_id: str | None = Form(None), focus_question: str = Form(""), user: dict | None = Depends(optional_user)) -> dict:
    del file, conversation_id, focus_question, user
    raise_pipeline_pending(PIPELINE_IMAGE_PENDING, "이미지 입력 경로가 아직 연결되지 않았습니다.")
