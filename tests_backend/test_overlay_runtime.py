from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import auth_service, database, session_store, table_catalog_service
from backend.errors import BackendError


CSRF_HEADERS = {
    "Origin": "https://testserver",
    "Sec-Fetch-Site": "same-origin",
}


class FakeRedis:
    """Small in-memory Redis contract double for the Lua boundary tests."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}
        self.sequences: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def eval(self, script: str, numkeys: int, *args: str) -> int | str:
        keys = args[:numkeys]
        argv = args[numkeys:]
        if script == session_store.CREATE_SESSION_LUA:
            session_key, user_key, sequence_key = keys
            value, _ttl, session_id, maximum, prefix = argv
            members = self.sorted_sets.setdefault(user_key, {})
            for member in list(members):
                if prefix + member not in self.values:
                    del members[member]
            if session_key in self.values:
                return 0
            self.values[session_key] = value
            self.sequences[sequence_key] = self.sequences.get(sequence_key, 0) + 1
            members[session_id] = float(self.sequences[sequence_key])
            while len(members) > int(maximum):
                oldest = min(members, key=lambda member: (members[member], member))
                del members[oldest]
                self.values.pop(prefix + oldest, None)
            return 1
        if script == session_store.DELETE_SESSION_LUA:
            session_key, user_key = keys
            session_id = argv[0]
            value = self.values.pop(session_key, None)
            if value is not None:
                self.sorted_sets.setdefault(user_key, {}).pop(session_id, None)
            return value or ""
        if script == session_store.DELETE_ALL_SESSIONS_LUA:
            user_key, sequence_key = keys
            prefix = argv[0]
            members = self.sorted_sets.pop(user_key, {})
            for member in members:
                self.values.pop(prefix + member, None)
            self.sequences.pop(sequence_key, None)
            return len(members)
        raise AssertionError("unexpected script")


def test_session_lua_keeps_newest_five_with_fixed_expiry() -> None:
    fake = FakeRedis()
    start = datetime(2026, 8, 27, tzinfo=timezone.utc)
    records = [
        session_store.create_session(
            "user-1",
            now=start + timedelta(seconds=index),
            client=fake,
        )
        for index in range(6)
    ]

    user_key = "auth:user-sessions:user-1"
    kept = fake.sorted_sets[user_key]
    assert len(kept) == 5
    assert set(kept) == {record.session_id for record in records[1:]}
    assert all(
        record.expires_at == start + timedelta(seconds=index, days=7)
        for index, record in enumerate(records)
    )
    assert "'EX', ARGV[2]" in session_store.CREATE_SESSION_LUA
    assert "redis.call('ZCARD'" in session_store.CREATE_SESSION_LUA


def test_same_timestamp_logins_still_keep_last_five() -> None:
    fake = FakeRedis()
    same_time = datetime(2026, 8, 27, tzinfo=timezone.utc)
    records = [
        session_store.create_session("user-2", now=same_time, client=fake)
        for _ in range(6)
    ]
    kept = fake.sorted_sets["auth:user-sessions:user-2"]
    assert set(kept) == {record.session_id for record in records[1:]}


def test_argon2id_hash_and_verify() -> None:
    encoded = auth_service.hash_password("correct horse battery staple")
    assert encoded.startswith("$argon2id$")
    assert auth_service.verify_password("correct horse battery staple", encoded)
    assert not auth_service.verify_password("wrong password", encoded)


@pytest.mark.parametrize("status", ["suspended", "withdrawn"])
def test_inactive_accounts_are_forbidden(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    row = {
        "id": "00000000-0000-0000-0000-000000000002",
        "primary_email": "person@example.com",
        "display_name": "Person",
        "status": status,
        "created_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
        "last_login_at": None,
        "password_hash": "$argon2id$unused",
    }
    monkeypatch.setattr(auth_service, "_find_local_account", lambda _: row)
    monkeypatch.setattr(auth_service, "verify_password", lambda *_: True)
    with pytest.raises(BackendError) as caught:
        auth_service.authenticate("person@example.com", "unused password")
    assert caught.value.status_code == 403
    assert caught.value.code == "ACCOUNT_INACTIVE"


def test_database_is_pending_without_explicit_schema_attestation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(database.APPLICATION_SCHEMA_STATUS_ENV, raising=False)
    monkeypatch.delenv(database.APPLICATION_SCHEMA_REVISION_ENV, raising=False)
    monkeypatch.delenv(database.APPLICATION_DATABASE_URL_ENV, raising=False)
    with pytest.raises(BackendError) as caught:
        with database.session():
            pass
    assert caught.value.code == "APPLICATION_SCHEMA_PENDING"
    assert caught.value.status_code == 503


def test_csrf_requires_fetch_metadata_and_exact_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "https://testserver")
    accepted = app_module.Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [
                (b"origin", b"https://testserver"),
                (b"sec-fetch-site", b"same-origin"),
            ],
        }
    )
    app_module.require_csrf(accepted)

    referer_fallback = app_module.Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [
                (b"referer", b"https://testserver/account/login"),
                (b"sec-fetch-site", b"same-site"),
            ],
        }
    )
    app_module.require_csrf(referer_fallback)

    rejected = app_module.Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [(b"origin", b"https://evil.example"), (b"sec-fetch-site", b"same-origin")],
        }
    )
    with pytest.raises(BackendError) as caught:
        app_module.require_csrf(rejected)
    assert caught.value.code == "CSRF_REJECTED"
    assert caught.value.status_code == 403

    missing_fetch_metadata = app_module.Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [(b"origin", b"https://testserver")],
        }
    )
    with pytest.raises(BackendError) as caught:
        app_module.require_csrf(missing_fetch_metadata)
    assert caught.value.status_code == 403


def test_auth_routes_use_cookie_contract_and_no_automatic_signup_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "https://testserver")
    created_at = datetime(2026, 8, 27, tzinfo=timezone.utc)
    user = {
        "id": "00000000-0000-0000-0000-000000000001",
        "primary_email": "person@example.com",
        "display_name": "Person",
        "status": "active",
        "created_at": created_at,
        "last_login_at": None,
    }
    session_id = "s_" + "x" * 40
    expires_at = created_at + timedelta(days=7)
    signup_calls: list[str] = []
    session_calls: list[str] = []
    monkeypatch.setattr(
        auth_service,
        "register_user",
        lambda email, password, display_name: signup_calls.append(email) or user,
    )
    monkeypatch.setattr(auth_service, "authenticate", lambda email, password: user)
    monkeypatch.setattr(
        auth_service,
        "create_session",
        lambda user_id: session_calls.append(user_id)
        or {"session_id": session_id, "expires_at": expires_at},
    )
    client = TestClient(app_module.app, base_url="https://testserver")

    signup = client.post(
        "/api/auth/signup",
        headers=CSRF_HEADERS,
        json={
            "email": "person@example.com",
            "password": "correct horse battery staple",
            "display_name": "Person",
        },
    )
    assert signup.status_code == 201
    assert signup.json() == {"user": {**user, "created_at": created_at.isoformat().replace("+00:00", "Z")}}
    assert signup_calls == ["person@example.com"]
    assert session_calls == []

    login = client.post(
        "/api/auth/login",
        headers=CSRF_HEADERS,
        json={"email": "person@example.com", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["id"] == user["id"]
    assert "session_id" not in login.json()
    assert "access_token" not in login.json()
    cookie = login.headers["set-cookie"].lower()
    assert "__host-kosis_session=" in cookie
    assert "max-age=604800" in cookie
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie
    assert "domain=" not in cookie


def test_auth_rejects_missing_csrf_and_does_not_use_header_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "https://testserver")
    client = TestClient(app_module.app, base_url="https://testserver")
    missing_csrf = client.post(
        "/api/auth/login",
        json={"email": "person@example.com", "password": "correct horse battery staple"},
    )
    assert missing_csrf.status_code == 403
    assert set(missing_csrf.json()) == {"code", "message"}

    header_only = client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer not-a-session"},
    )
    assert header_only.status_code == 401
    assert set(header_only.json()) == {"code", "message"}


def test_current_logout_invalidates_only_current_opaque_session(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    monkeypatch.setattr(session_store, "_client", fake)
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "https://testserver")
    first = session_store.create_session("user-logout", client=fake)
    second = session_store.create_session("user-logout", client=fake)
    monkeypatch.setattr(
        auth_service,
        "authenticate_session",
        lambda session_id: {
            "session_id": session_id,
            "user": {"id": "user-logout"},
            "expires_at": first.expires_at,
        },
    )
    client = TestClient(app_module.app, base_url="https://testserver")
    client.cookies.set(session_store.SESSION_COOKIE_NAME, first.session_id)
    response = client.post("/api/auth/logout", headers=CSRF_HEADERS)
    assert response.status_code == 204
    assert session_store.get_session(first.session_id, client=fake) is None
    assert session_store.get_session(second.session_id, client=fake) is not None


def test_logout_all_invalidates_all_user_opaque_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    monkeypatch.setattr(session_store, "_client", fake)
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "https://testserver")
    records = [session_store.create_session("user-logout-all", client=fake) for _ in range(3)]
    monkeypatch.setattr(
        auth_service,
        "authenticate_session",
        lambda session_id: {
            "session_id": session_id,
            "user": {"id": "user-logout-all"},
            "expires_at": records[-1].expires_at,
        },
    )
    client = TestClient(app_module.app, base_url="https://testserver")
    client.cookies.set(session_store.SESSION_COOKIE_NAME, records[-1].session_id)
    response = client.post("/api/auth/logout-all", headers=CSRF_HEADERS)
    assert response.status_code == 204
    assert all(session_store.get_session(record.session_id, client=fake) is None for record in records)


def test_non_auth_mutations_are_also_csrf_protected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "https://testserver")
    client = TestClient(app_module.app, base_url="https://testserver")
    for method, path in [
        ("post", "/api/v1/conversations"),
        ("delete", "/api/v1/conversations/conversation-1"),
        ("delete", "/api/v1/favorites/org:table"),
    ]:
        response = getattr(client, method)(path)
        assert response.status_code == 403
        assert response.json()["code"] == "CSRF_REJECTED"


@pytest.mark.parametrize(
    "path,kwargs",
    [
        ("/api/v1/tables", {"method": "get"}),
        ("/api/v1/analyze", {"method": "post", "json": {"text": "2025년 수치"}}),
        (
            "/api/v1/analyze/image",
            {"method": "post", "files": {"file": ("sample.png", b"not-an-image", "image/png")}},
        ),
        ("/api/v1/verify/develop", {"method": "post", "json": {"text": "기사 본문"}}),
        ("/api/v1/favorites", {"method": "post", "json": {"table_key": "org:table"}}),
    ],
)
def test_search_entry_points_fail_closed_before_underlying_calls(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    kwargs: dict[str, object],
) -> None:
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "https://testserver")
    client = TestClient(app_module.app, base_url="https://testserver")
    request_kwargs = {key: value for key, value in kwargs.items() if key != "method"}
    request_kwargs["headers"] = CSRF_HEADERS
    response = getattr(client, kwargs["method"])(path, **request_kwargs)
    assert response.status_code == 503
    expected_codes = {
        "/api/v1/tables": "KOSIS_RELEASE_CONFIGURATION_PENDING",
        "/api/v1/analyze": "PIPELINE_NATURAL_QUERY_PENDING",
        "/api/v1/analyze/image": "PIPELINE_IMAGE_PENDING",
        "/api/v1/verify/develop": "PIPELINE_RUNTIME_PENDING",
        "/api/v1/favorites": "APPLICATION_PRODUCT_STATE_PENDING",
    }
    assert response.json()["code"] == expected_codes[path]


def test_table_catalog_service_has_no_local_fallback() -> None:
    with pytest.raises(BackendError) as caught:
        table_catalog_service.get_table("org:table")
    assert caught.value.code == "KOSIS_RELEASE_CONFIGURATION_PENDING"
