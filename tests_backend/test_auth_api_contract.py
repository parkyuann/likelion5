import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from backend import app as app_module


NOW = datetime(2026, 8, 27, tzinfo=timezone.utc)
USER = {
    "id": "11111111-1111-4111-8111-111111111111",
    "primary_email": "user@example.com",
    "display_name": "사용자",
    "status": "active",
    "created_at": NOW,
    "last_login_at": None,
}
CSRF = {
    "Origin": "http://localhost:5173",
    "Sec-Fetch-Site": "same-origin",
}


def test_signup_is_csrf_protected_and_does_not_issue_session(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5173")
    monkeypatch.setattr(app_module.auth_service, "register_user", lambda *_: USER)
    client = TestClient(app_module.app)
    denied = client.post(
        "/api/auth/signup",
        json={"email": "user@example.com", "password": "long-password", "display_name": "사용자"},
    )
    assert denied.status_code == 403
    assert denied.json() == {"code": "CSRF_REJECTED", "message": "요청 출처를 확인할 수 없습니다."}

    accepted = client.post(
        "/api/auth/signup",
        headers=CSRF,
        json={"email": "user@example.com", "password": "long-password", "display_name": "사용자"},
    )
    assert accepted.status_code == 201
    assert accepted.json()["user"]["id"] == USER["id"]
    assert "set-cookie" not in accepted.headers


def test_login_sets_host_cookie_without_returning_token(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5173")
    monkeypatch.setattr(app_module.auth_service, "authenticate", lambda *_: USER)
    monkeypatch.setattr(
        app_module.auth_service,
        "create_session",
        lambda *_: {"session_id": "opaque_session_id_1234567890", "expires_at": NOW + timedelta(days=7)},
    )
    client = TestClient(app_module.app)
    response = client.post(
        "/api/auth/login",
        headers=CSRF,
        json={"email": "user@example.com", "password": "long-password"},
    )
    assert response.status_code == 200
    assert set(response.json()) == {"user", "expires_at"}
    cookie = response.headers["set-cookie"]
    assert "__Host-kosis_session=" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "Max-Age=604800" in cookie and "Domain=" not in cookie


def test_bearer_header_does_not_authenticate():
    client = TestClient(app_module.app)
    response = client.get("/api/auth/me", headers={"Authorization": "Bearer legacy"})
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"


def test_all_search_routes_fail_closed_before_service_call(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5173")
    monkeypatch.delenv("KOSIS_RELEASE_ID", raising=False)
    client = TestClient(app_module.app)
    tables = client.get("/api/v1/tables")
    assert tables.status_code == 503
    assert tables.json()["code"] == "KOSIS_RELEASE_CONFIGURATION_PENDING"

    for path, payload, expected_code in [
        ("/api/v1/analyze", {"text": "x"}, "PIPELINE_NATURAL_QUERY_PENDING"),
        ("/api/v1/verify/develop", {"text": "x"}, "PIPELINE_RUNTIME_PENDING"),
    ]:
        response = client.post(path, headers=CSRF, json=payload)
        assert response.status_code == 503
        assert response.json()["code"] == expected_code

    image = client.post(
        "/api/v1/analyze/image",
        headers=CSRF,
        files={"file": ("sample.png", b"not-an-image", "image/png")},
    )
    assert image.status_code == 503
    assert image.json()["code"] == "PIPELINE_IMAGE_PENDING"

    favorite = client.post("/api/v1/favorites", headers=CSRF, json={"table_key": "a:b"})
    assert favorite.status_code == 503
    assert favorite.json()["code"] == "APPLICATION_PRODUCT_STATE_PENDING"


def test_tables_openapi_declares_candidate_response_contract():
    schema = app_module.app.openapi()
    response_schema = schema["paths"]["/api/v1/tables"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    response_name = response_schema["$ref"].rsplit("/", 1)[-1]
    response = schema["components"]["schemas"][response_name]
    candidate_name = response["properties"]["items"]["items"]["$ref"].rsplit("/", 1)[-1]
    candidate = schema["components"]["schemas"][candidate_name]
    assert {
        "table_key", "release_id", "source", "score", "org_id", "tbl_id", "org_name", "tbl_name",
        "status", "send_de", "kosis_url", "evidence", "metadata",
    }.issubset(set(candidate["required"]))
    response_required = set(response["required"])
    assert {"release_id", "items", "total", "total_relation", "limit", "offset", "organizations", "organizations_relation"}.issubset(response_required)
    assert response["properties"]["total_relation"]["enum"] == ["eq", "gte"]
    assert response["properties"]["organizations_relation"]["enum"] == ["eq", "gte"]


def test_runtime_auth_openapi_sha_is_pinned():
    path = Path(__file__).resolve().parents[1] / "contracts" / "auth-session-v1.yaml"
    # Git stores the contract with LF. Normalize checkout line endings so the
    # receipt is identical on Windows (CRLF) and EC2/Linux (LF).
    canonical_bytes = path.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(canonical_bytes).hexdigest().upper() == "589A0E24282BBDEC86D40F5CD3CFD8667B7537E448CB422C419AEE6FDE4FC357"
