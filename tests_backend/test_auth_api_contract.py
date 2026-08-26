from datetime import datetime, timedelta, timezone

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


def test_all_search_routes_fail_closed_before_service_call():
    client = TestClient(app_module.app)
    assert client.get("/api/v1/tables").status_code == 503
    assert client.post("/api/v1/analyze", headers=CSRF, json={"text": "x"}).status_code == 503
    assert client.post("/api/v1/verify/develop", headers=CSRF, json={"text": "x"}).status_code == 503
    assert client.post("/api/v1/analyze/image", headers=CSRF).status_code == 503
    assert client.post("/api/v1/favorites", headers=CSRF, json={"table_key": "a:b"}).status_code == 503
