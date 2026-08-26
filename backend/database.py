"""Application PostgreSQL repository boundary.

The application database is owned by the EC2 data tier.  This module deliberately
does not create tables, run migrations, or open a local file database.  Until the
canonical application migration has been reconciled with the runtime SQL contract,
all repository access is fail-closed with ``APPLICATION_SCHEMA_PENDING``.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from backend.errors import BackendError

try:  # The CPU API image supplies psycopg; local static checks may not.
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - exercised by a dependency preflight
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment,misc]


APPLICATION_SCHEMA_STATUS_ENV = "APPLICATION_SCHEMA_STATUS"
APPLICATION_SCHEMA_REVISION_ENV = "APPLICATION_SCHEMA_REVISION"
APPLICATION_DATABASE_URL_ENV = "APPLICATION_DATABASE_URL"
SCHEMA_STATUS_PENDING = "PENDING_APPLICATION_SCHEMA_RECONCILIATION"


class RepositoryError(RuntimeError):
    """A PostgreSQL operation failed after the application boundary was opened."""

    def __init__(self, message: str, *, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def schema_ready() -> bool:
    """Return whether an operator has explicitly attested the reconciled schema.

    The actual revision is intentionally not guessed here.  A non-empty revision
    supplied by deployment configuration is required in addition to the explicit
    ``VERIFIED`` status.  The default remains pending.
    """

    return (
        os.getenv(APPLICATION_SCHEMA_STATUS_ENV, SCHEMA_STATUS_PENDING).strip().upper()
        == "VERIFIED"
        and bool(os.getenv(APPLICATION_SCHEMA_REVISION_ENV, "").strip())
    )


def _require_runtime_configuration() -> str:
    if not schema_ready():
        raise BackendError(
            "APPLICATION_SCHEMA_PENDING",
            "application DB 스키마 대조가 완료되지 않았습니다.",
            status_code=503,
        )
    dsn = os.getenv(APPLICATION_DATABASE_URL_ENV, "").strip()
    if not dsn:
        raise BackendError(
            "DATABASE_CONFIGURATION_PENDING",
            "application PostgreSQL 연결 설정이 없습니다.",
            status_code=503,
        )
    return dsn


def connect() -> Any:
    """Open one PostgreSQL connection without any schema side effect."""

    dsn = _require_runtime_configuration()
    if psycopg is None or dict_row is None:
        raise BackendError(
            "DATABASE_DRIVER_UNAVAILABLE",
            "PostgreSQL 드라이버를 사용할 수 없습니다.",
            status_code=503,
        )
    try:
        return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)
    except Exception as exc:  # psycopg has several connection-error subclasses.
        raise BackendError(
            "DATABASE_UNAVAILABLE",
            "application PostgreSQL에 연결할 수 없습니다.",
            status_code=503,
        ) from exc


def _sqlstate(exc: BaseException) -> str | None:
    value = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    return str(value) if value else None


def as_jsonb(value: Any) -> Any:
    """Adapt a Python value to PostgreSQL JSONB when psycopg is present."""

    return Jsonb(value) if Jsonb is not None else value


@contextmanager
def session() -> Iterator[Any]:
    """Yield a transactional PostgreSQL connection and close it afterwards.

    DDL, migration execution, and implicit table creation are intentionally absent.
    SQLSTATE-bearing driver errors are wrapped so API callers can return a safe 503
    while preserving the state for conflict handling in the auth repository.
    """

    connection = connect()
    try:
        yield connection
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        if isinstance(exc, (BackendError, RepositoryError)):
            raise
        state = _sqlstate(exc)
        if state:
            raise RepositoryError("PostgreSQL operation failed.", sqlstate=state) from exc
        raise
    else:
        try:
            connection.commit()
        except Exception as exc:
            try:
                connection.rollback()
            except Exception:
                pass
            state = _sqlstate(exc)
            if state:
                raise RepositoryError("PostgreSQL commit failed.", sqlstate=state) from exc
            raise
    finally:
        try:
            connection.close()
        except Exception:
            pass
