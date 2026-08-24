"""Crash-safe HTTP-attempt budgets for the operational audit shadow.

The ledger deliberately counts *attempts*, rather than logical requests.  It is
an append-only SQLite event log so pilot and batch processes can share one
file.  A reservation which is still open when a new process starts is changed
to ``UNKNOWN`` and remains consumed; this is the fail-closed crash rule.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import os
import uuid
from typing import Callable, Iterator, Any


DEFAULT_LIMITS = {
    "metadata": 6000,
    "hcx_l2": 12,
    "cell": 12,
    "answer": 168,
}


class BudgetError(RuntimeError):
    pass


class BudgetExhausted(BudgetError):
    pass


@dataclass(frozen=True)
class Reservation:
    run_id: str
    endpoint_class: str
    reservation_id: str
    owner_id: str = ""
    target_id: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HttpAttemptBudgetLedger:
    """Persistent, process-safe attempt ledger shared by pilot and batch."""

    def __init__(self, path: str | Path, limits: dict[str, int] | None = None, *, owner_id: str | None = None, lease_seconds: float = 60.0, per_target_limits: dict[str, int] | None = None, pilot_metadata_limit: int = 750) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.owner_id = str(owner_id or f"pid:{os.getpid()}:{uuid.uuid4().hex}")
        self.lease_seconds = float(lease_seconds)
        self.per_target_limits = {"hcx_l2": 1, "cell": 1, "answer": 2, **(per_target_limits or {})}
        self.pilot_metadata_limit = int(pilot_metadata_limit)
        self._phases: dict[str, str] = {}
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS budget_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    reservation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    endpoint_class TEXT NOT NULL,
                    state TEXT NOT NULL,
                    at TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    owner_id TEXT NOT NULL DEFAULT '',
                    lease_until TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_budget_reservation
                    ON budget_events(reservation_id, seq);
                """
            )
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(budget_events)")}
            if "owner_id" not in columns:
                db.execute("ALTER TABLE budget_events ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
            if "lease_until" not in columns:
                db.execute("ALTER TABLE budget_events ADD COLUMN lease_until TEXT NOT NULL DEFAULT ''")
            if "target_id" not in columns:
                db.execute("ALTER TABLE budget_events ADD COLUMN target_id TEXT NOT NULL DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _latest(self, db: sqlite3.Connection, reservation_id: str) -> tuple[str, str, str] | None:
        row = db.execute(
            "SELECT state,owner_id,lease_until FROM budget_events WHERE reservation_id=? ORDER BY seq DESC LIMIT 1",
            (reservation_id,),
        ).fetchone()
        return (str(row[0]), str(row[1]), str(row[2])) if row else None

    def _append(
        self, db: sqlite3.Connection, reservation: Reservation, state: str, detail: dict[str, Any] | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO budget_events(reservation_id,run_id,endpoint_class,state,at,detail,owner_id,lease_until,target_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (reservation.reservation_id, reservation.run_id, reservation.endpoint_class, state, _now(), json.dumps(detail or {}, sort_keys=True), reservation.owner_id, "", reservation.target_id),
        )

    def startup_recover(self, *, force: bool = False) -> int:
        """Explicitly consume stale reservations; normal open is read-only.

        A live concurrent owner is never consumed.  ``force`` is reserved for
        an operator that has independently established process death.
        """
        changed = 0
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT reservation_id,run_id,endpoint_class,owner_id,lease_until FROM budget_events e "
                "WHERE e.state='RESERVED' AND e.seq=(SELECT MAX(x.seq) FROM budget_events x WHERE x.reservation_id=e.reservation_id)"
            ).fetchall()
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            for reservation_id, run_id, endpoint_class, owner_id, lease_until in rows:
                stale = force or (str(lease_until) and str(lease_until) < now.isoformat())
                if not stale or str(owner_id) == self.owner_id:
                    continue
                self._append(db, Reservation(str(run_id), str(endpoint_class), str(reservation_id), str(owner_id)), "UNKNOWN", {"recovered": True, "force": force})
                changed += 1
            db.commit()
        return changed

    # Backward-compatible explicit spelling; unlike the old implementation it
    # never runs implicitly from __init__.
    recover_unknown = startup_recover

    def set_phase(self, run_id: str, phase: str) -> None:
        phase = str(phase)
        if phase not in {"pilot", "batch"}:
            raise BudgetError(f"UNKNOWN_BUDGET_PHASE:{phase}")
        self._phases[str(run_id)] = phase

    def current_phase(self, run_id: str) -> str:
        return self._phases.get(str(run_id), "batch")

    def used(self, endpoint_class: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM budget_events e WHERE e.endpoint_class=? AND e.state IN ('RESERVED','CONSUMED','UNKNOWN') "
                "AND e.seq=(SELECT MAX(x.seq) FROM budget_events x WHERE x.reservation_id=e.reservation_id)",
                (endpoint_class,),
            ).fetchone()
        return int(row[0] or 0)

    def remaining(self, endpoint_class: str) -> int:
        return int(self.limits.get(endpoint_class, 0)) - self.used(endpoint_class)

    def reserve(self, run_id: str, endpoint_class: str, *, target_id: str | None = None, detail: dict[str, Any] | None = None) -> Reservation:
        run_id, endpoint_class = str(run_id), str(endpoint_class)
        if endpoint_class not in self.limits:
            raise BudgetError(f"UNKNOWN_ENDPOINT_CLASS:{endpoint_class}")
        target_id = str(target_id or "")
        reservation = Reservation(run_id, endpoint_class, uuid.uuid4().hex, self.owner_id, target_id)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used = int(db.execute(
                "SELECT COUNT(*) FROM budget_events e WHERE e.endpoint_class=? AND e.state IN ('RESERVED','CONSUMED','UNKNOWN') "
                "AND e.seq=(SELECT MAX(x.seq) FROM budget_events x WHERE x.reservation_id=e.reservation_id)",
                (endpoint_class,),
            ).fetchone()[0] or 0)
            if used >= int(self.limits[endpoint_class]):
                db.rollback()
                raise BudgetExhausted(f"CALL_BUDGET_EXHAUSTED:{endpoint_class}")
            if endpoint_class == "metadata" and (detail or {}).get("phase") == "pilot":
                pilot_rows = db.execute(
                    "SELECT initial.reservation_id,initial.detail,latest.state FROM budget_events initial "
                    "JOIN budget_events latest ON latest.seq=(SELECT MAX(x.seq) FROM budget_events x WHERE x.reservation_id=initial.reservation_id) "
                    "WHERE initial.endpoint_class=? AND initial.run_id=? AND initial.state='RESERVED' "
                    "AND initial.seq=(SELECT MIN(x.seq) FROM budget_events x WHERE x.reservation_id=initial.reservation_id)",
                    (endpoint_class, run_id),
                ).fetchall()
                pilot_used = sum(1 for reservation_id, raw_detail, latest_state in pilot_rows if latest_state in {"RESERVED", "CONSUMED", "UNKNOWN"} and (json.loads(raw_detail or "{}")).get("phase") == "pilot")
                if pilot_used >= self.pilot_metadata_limit:
                    db.rollback()
                    raise BudgetExhausted(f"CALL_BUDGET_EXHAUSTED:metadata:pilot_limit={self.pilot_metadata_limit}")
            target_limit = self.per_target_limits.get(endpoint_class)
            if target_id and target_limit is not None:
                target_used = int(db.execute(
                    "SELECT COUNT(*) FROM budget_events e WHERE e.endpoint_class=? AND e.target_id=? "
                    "AND e.state IN ('RESERVED','CONSUMED','UNKNOWN') "
                    "AND e.seq=(SELECT MAX(x.seq) FROM budget_events x WHERE x.reservation_id=e.reservation_id)",
                    (endpoint_class, target_id),
                ).fetchone()[0] or 0)
                if target_used >= int(target_limit):
                    db.rollback()
                    raise BudgetExhausted(f"CALL_BUDGET_EXHAUSTED:{endpoint_class}:target={target_id}")
            from datetime import datetime, timedelta, timezone
            lease_until = (datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat()
            db.execute(
                "INSERT INTO budget_events(reservation_id,run_id,endpoint_class,state,at,detail,owner_id,lease_until,target_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (reservation.reservation_id, reservation.run_id, reservation.endpoint_class, "RESERVED", _now(), json.dumps(detail or {}, sort_keys=True), reservation.owner_id, lease_until, target_id),
            )
            db.commit()
        return reservation

    def _transition(self, reservation: Reservation, state: str, detail: dict[str, Any] | None = None) -> None:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._latest(db, reservation.reservation_id)
            if current is None or current[0] != "RESERVED":
                db.rollback()
                raise BudgetError(f"INVALID_RESERVATION_STATE:{reservation.reservation_id}:{current[0] if current else None}")
            if current[1] and current[1] != self.owner_id:
                db.rollback()
                raise BudgetError(f"RESERVATION_OWNER_MISMATCH:{reservation.reservation_id}")
            self._append(db, reservation, state, detail)
            db.commit()

    def complete(self, reservation: Reservation, *, detail: dict[str, Any] | None = None) -> None:
        self._transition(reservation, "CONSUMED", detail)

    def mark_unknown(self, reservation: Reservation, *, detail: dict[str, Any] | None = None) -> None:
        self._transition(reservation, "UNKNOWN", detail)

    def release(self, reservation: Reservation, *, detail: dict[str, Any] | None = None) -> None:
        """Release only an attempt known not to have reached the network."""
        self._transition(reservation, "RELEASED", detail)

    @contextmanager
    def attempt(self, run_id: str, endpoint_class: str, *, target_id: str | None = None, detail: dict[str, Any] | None = None) -> Iterator[Reservation]:
        reservation = self.reserve(run_id, endpoint_class, target_id=target_id, detail=detail)
        try:
            yield reservation
        except BaseException as exc:
            # A transport exception/timeout is an HTTP attempt and is consumed.
            self.mark_unknown(reservation, detail={"exception": type(exc).__name__})
            raise
        else:
            self.complete(reservation)

    def execute(self, run_id: str, endpoint_class: str, call: Callable[[], Any], *, target_id: str | None = None, detail: dict[str, Any] | None = None) -> Any:
        with self.attempt(run_id, endpoint_class, target_id=target_id, detail=detail):
            return call()

    def release_before_send(self, reservation: Reservation, error: BaseException | str) -> None:
        """Explicit escape hatch for a failure proven to occur before send."""
        self.release(reservation, detail={"error": str(error)[:240]})


class BudgetedCallable:
    """Small adapter shared by pilot/batch transports."""

    def __init__(self, ledger: HttpAttemptBudgetLedger, run_id: str, endpoint_class: str, call: Callable[..., Any], *, target_id: str | None = None, detail: dict[str, Any] | None = None, phase_provider: Callable[[], str] | None = None) -> None:
        self.ledger, self.run_id, self.endpoint_class, self.call, self.target_id, self.detail, self.phase_provider = ledger, str(run_id), str(endpoint_class), call, target_id, dict(detail or {}), phase_provider

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        detail = dict(self.detail)
        if self.phase_provider is not None:
            detail["phase"] = str(self.phase_provider())
        return self.ledger.execute(self.run_id, self.endpoint_class, lambda: self.call(*args, **kwargs), target_id=self.target_id, detail=detail)


class BudgetedAnswerer:
    """Wrap each draft/repair render with the target-specific answer limit."""

    def __init__(self, inner: Any, ledger: HttpAttemptBudgetLedger, run_id: str, target_id: str) -> None:
        self.inner, self.ledger, self.run_id, self.target_id = inner, ledger, str(run_id), str(target_id)

    def render(self, packet: Any, brief: Any, repair_code: str | None = None) -> Any:
        return self.ledger.execute(
            self.run_id,
            "answer",
            lambda: self.inner.render(packet, brief, repair_code),
            target_id=self.target_id,
        )


__all__ = ["BudgetError", "BudgetExhausted", "BudgetedAnswerer", "BudgetedCallable", "DEFAULT_LIMITS", "HttpAttemptBudgetLedger", "Reservation"]
