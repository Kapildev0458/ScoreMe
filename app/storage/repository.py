"""SQLite repository.

Design notes
* One short-lived connection per unit of work (thread-safe under FastAPI's threadpool).
* ``BEGIN IMMEDIATE`` takes the write lock up-front -> no lock-upgrade deadlocks.
* WAL mode lets readers proceed while a writer commits.
* ``UNIQUE(workflow_id, idempotency_key)`` is the idempotency guarantee: even two
  concurrent identical submissions can only create one request row.
* ``claim()`` is a compare-and-set (UPDATE ... WHERE status IN (...)) used as an
  optimistic lock so a request is never processed by two workers at once.
* Commit failures can be injected per transaction label to test partial-save handling.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.domain.errors import PersistenceError

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_versions (
    workflow_id TEXT NOT NULL,
    version     INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (workflow_id, version)
);
CREATE TABLE IF NOT EXISTS requests (
    id               TEXT PRIMARY KEY,
    workflow_id      TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    idempotency_key  TEXT,
    payload_hash     TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    status           TEXT NOT NULL,
    decision         TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (workflow_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_requests_status ON requests (workflow_id, status);
CREATE TABLE IF NOT EXISTS state_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL REFERENCES requests(id),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    reason      TEXT,
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_transitions_request ON state_transitions (request_id);
CREATE TABLE IF NOT EXISTS decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id       TEXT NOT NULL REFERENCES requests(id),
    attempt          INTEGER NOT NULL,
    decision         TEXT NOT NULL,
    reasons_json     TEXT NOT NULL,
    trace_json       TEXT NOT NULL,
    context_json     TEXT NOT NULL,
    final_stage      TEXT,
    workflow_version INTEGER NOT NULL,
    config_hash      TEXT NOT NULL,
    decided_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_request ON decisions (request_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT,
    workflow_id  TEXT,
    event        TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_request ON audit_log (request_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


class Repository:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._fault_lock = threading.Lock()
        self._commit_faults: dict[str, int] = {}
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ infrastructure
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def inject_commit_failure(self, label: str, times: int = 1) -> None:
        """Test/demo hook: make the next ``times`` commits of transaction ``label`` fail."""
        with self._fault_lock:
            self._commit_faults[label] = times

    def _maybe_fail(self, label: str) -> None:
        with self._fault_lock:
            remaining = self._commit_faults.get(label, 0)
            if remaining > 0:
                self._commit_faults[label] = remaining - 1
                raise PersistenceError(f"simulated storage failure during '{label}' commit")

    @contextmanager
    def transaction(self, label: str = "default") -> Iterator[sqlite3.Connection]:
        """Atomic unit of work: everything inside commits together or not at all."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            self._maybe_fail(label)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # ------------------------------------------------------------------ workflow versions
    def save_workflow_version(self, workflow_id: str, version: int, config_hash: str, config: dict) -> None:
        with self.transaction("workflow_version") as conn:
            conn.execute(
                "INSERT INTO workflow_versions VALUES (?,?,?,?,?)",
                (workflow_id, version, config_hash, _dumps(config), utcnow()),
            )
            self.add_audit(conn, None, "WORKFLOW_VERSION_REGISTERED",
                           {"version": version, "config_hash": config_hash}, workflow_id=workflow_id)

    def load_workflow_versions(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM workflow_versions ORDER BY workflow_id, version")
        return [{**dict(r), "config": json.loads(r["config_json"])} for r in rows]

    # ------------------------------------------------------------------ requests
    def insert_request(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        now = utcnow()
        conn.execute(
            """INSERT INTO requests (id, workflow_id, workflow_version, idempotency_key, payload_hash,
                                     payload_json, status, decision, attempts, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,0,?,?)""",
            (row["id"], row["workflow_id"], row["workflow_version"], row.get("idempotency_key"),
             row["payload_hash"], _dumps(row["payload"]), row["status"], row.get("decision"), now, now),
        )

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM requests WHERE id = ?", (request_id,))
        return self._request_row(rows[0]) if rows else None

    def get_request_by_key(self, workflow_id: str, key: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM requests WHERE workflow_id = ? AND idempotency_key = ?",
                           (workflow_id, key))
        return self._request_row(rows[0]) if rows else None

    def list_requests(self, workflow_id: str | None = None, status: str | None = None,
                      limit: int = 50) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM requests WHERE 1=1", []
        if workflow_id:
            sql, params = sql + " AND workflow_id = ?", params + [workflow_id]
        if status:
            sql, params = sql + " AND status = ?", params + [status]
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [self._request_row(r) for r in self._query(sql, tuple(params + [limit]))]

    @staticmethod
    def _request_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        return data

    def compare_and_set_status(self, conn: sqlite3.Connection, request_id: str, expected: tuple[str, ...],
                               new_status: str, decision: str | None = None,
                               increment_attempts: bool = False) -> bool:
        """Optimistic lock: only moves the request if it is still in one of ``expected``."""
        placeholders = ",".join("?" * len(expected))
        cur = conn.execute(
            f"""UPDATE requests SET status = ?, decision = COALESCE(?, decision),
                       attempts = attempts + ?, updated_at = ?
                WHERE id = ? AND status IN ({placeholders})""",
            (new_status, decision, 1 if increment_attempts else 0, utcnow(), request_id, *expected),
        )
        return cur.rowcount == 1

    # ------------------------------------------------------------------ history / audit
    def add_transition(self, conn: sqlite3.Connection, request_id: str, from_status: str | None,
                       to_status: str, reason: str, actor: str = "system") -> None:
        conn.execute(
            "INSERT INTO state_transitions (request_id, from_status, to_status, reason, actor, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (request_id, from_status, to_status, reason, actor, utcnow()),
        )

    def add_audit(self, conn: sqlite3.Connection, request_id: str | None, event: str,
                  details: dict[str, Any], workflow_id: str | None = None) -> None:
        conn.execute(
            "INSERT INTO audit_log (request_id, workflow_id, event, details_json, created_at) VALUES (?,?,?,?,?)",
            (request_id, workflow_id, event, _dumps(details), utcnow()),
        )

    def add_decision(self, conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        conn.execute(
            """INSERT INTO decisions (request_id, attempt, decision, reasons_json, trace_json, context_json,
                                      final_stage, workflow_version, config_hash, decided_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (record["request_id"], record["attempt"], record["decision"], _dumps(record["reasons"]),
             _dumps(record["trace"]), _dumps(record["context"]), record.get("final_stage"),
             record["workflow_version"], record["config_hash"], record["decided_by"], utcnow()),
        )

    def list_transitions(self, request_id: str) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM state_transitions WHERE request_id = ? ORDER BY id", (request_id,))
        return [dict(r) for r in rows]

    def list_audit(self, request_id: str | None = None, workflow_id: str | None = None) -> list[dict[str, Any]]:
        if request_id:
            rows = self._query("SELECT * FROM audit_log WHERE request_id = ? ORDER BY id", (request_id,))
        else:
            rows = self._query("SELECT * FROM audit_log WHERE workflow_id = ? AND request_id IS NULL ORDER BY id",
                               (workflow_id,))
        return [{**{k: r[k] for k in r.keys() if k != "details_json"}, "details": json.loads(r["details_json"])}
                for r in rows]

    def list_decisions(self, request_id: str) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM decisions WHERE request_id = ? ORDER BY id", (request_id,))
        out = []
        for r in rows:
            d = dict(r)
            for key in ("reasons", "trace", "context"):
                d[key] = json.loads(d.pop(f"{key}_json"))
            out.append(d)
        return out
