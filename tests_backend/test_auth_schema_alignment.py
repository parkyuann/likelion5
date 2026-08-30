from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from backend import auth_service, database
from backend.errors import BackendError


class FakeCursor:
    def __init__(self, row=None) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class FakeTransactionalConnection:
    def __init__(self, rows=None) -> None:
        self.rows = list(rows or [])
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, sql: str, params: tuple[object, ...]):
        self.calls.append((" ".join(sql.split()), params))
        row = self.rows.pop(0) if self.rows else None
        return FakeCursor(row)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _session_for(connection: FakeTransactionalConnection):
    @contextmanager
    def fake_session():
        yield connection
        connection.commit()

    return fake_session


def test_signup_uses_canonical_local_auth_columns_and_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeTransactionalConnection()
    monkeypatch.setattr(auth_service, "session", _session_for(connection))
    monkeypatch.setattr(
        auth_service,
        "hash_password",
        lambda password: "$argon2id$v=19$m=65536,t=3,p=4$test$hash",
    )
    monkeypatch.setattr(
        auth_service.uuid,
        "uuid4",
        lambda: "11111111-1111-4111-8111-111111111111",
    )

    user = auth_service.register_user(
        "  User@Example.COM ", "correct horse battery staple", " User "
    )

    assert user["primary_email"] == "user@example.com"
    assert len(connection.calls) == 2
    users_sql, users_params = connection.calls[0]
    auth_sql, auth_params = connection.calls[1]
    assert "INSERT INTO users" in users_sql
    assert users_params[0] == user["id"]
    assert "(user_id, provider, provider_user_id, provider_email, password_hash, created_at)" in auth_sql
    assert auth_params == (
        user["id"],
        "local",
        user["id"],
        "user@example.com",
        "$argon2id$v=19$m=65536,t=3,p=4$test$hash",
        users_params[4],
    )
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_login_joins_by_local_provider_email(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {
        "id": "11111111-1111-4111-8111-111111111111",
        "primary_email": "user@example.com",
        "display_name": "User",
        "status": "active",
        "created_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
        "last_login_at": None,
        "password_hash": "$argon2id$unused",
    }
    connection = FakeTransactionalConnection([row, None])
    monkeypatch.setattr(auth_service, "session", _session_for(connection))
    monkeypatch.setattr(auth_service, "verify_password", lambda *_: True)

    auth_service.authenticate(" User@Example.COM ", "correct horse battery staple")

    login_sql, login_params = connection.calls[0]
    assert "a.provider = %s" in login_sql
    assert "lower(a.provider_email) = lower(%s)" in login_sql
    assert "lower(u.primary_email)" not in login_sql
    assert login_params == ("local", "user@example.com")
    assert connection.commits == 2
    assert connection.closed is False


@pytest.mark.parametrize("status,revision", [("VERIFIED", ""), ("VERIFIED", "002_application_auth"), ("PENDING", "001_application_auth")])
def test_schema_ready_requires_exact_verified_001(
    monkeypatch: pytest.MonkeyPatch, status: str, revision: str
) -> None:
    monkeypatch.setenv(database.APPLICATION_SCHEMA_STATUS_ENV, status)
    monkeypatch.setenv(database.APPLICATION_SCHEMA_REVISION_ENV, revision)
    assert database.schema_ready() is False


def test_schema_ready_accepts_exact_verified_001(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(database.APPLICATION_SCHEMA_STATUS_ENV, "VERIFIED")
    monkeypatch.setenv(database.APPLICATION_SCHEMA_REVISION_ENV, "001_application_auth")
    assert database.schema_ready() is True


def test_sqlstate_details_are_not_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    error = database.RepositoryError(
        "PostgreSQL operation failed.", sqlstate="23505"
    )
    converted = auth_service._database_error(error)
    assert converted.status_code == 409
    assert "23505" not in converted.message
    assert "constraint" not in converted.message.lower()
