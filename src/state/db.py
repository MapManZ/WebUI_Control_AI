"""SQLite (WAL) state store with the 6 mandatory tables.

Tables: projects, job_contracts, job_states, job_attempts, qa_verifications,
audit_events.  ``audit_events`` is append-only (UPDATE/DELETE blocked by
trigger) and approved contract payloads are immutable (trigger).

The connection manager supports explicit ``close()`` so Windows file locks on
``*.db``/``-wal``/``-shm`` are released deterministically (tests delete temp dirs).
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

TABLES = (
    "projects",
    "job_contracts",
    "job_states",
    "job_attempts",
    "qa_verifications",
    "audit_events",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    canonical_root TEXT NOT NULL,
    protected_paths_json TEXT NOT NULL DEFAULT '[]',
    serena_policy TEXT NOT NULL DEFAULT 'READ_ONLY_NO_9121',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_contracts (
    job_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    batch_ordinal INTEGER NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    contract_json TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL,
    approval_state TEXT NOT NULL DEFAULT 'PENDING',
    approved_by TEXT,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS job_states (
    job_id TEXT PRIMARY KEY REFERENCES job_contracts(job_id),
    current_state TEXT NOT NULL,
    rework_count INTEGER NOT NULL DEFAULT 0,
    infra_retry_count INTEGER NOT NULL DEFAULT 0,
    active_attempt_id TEXT,
    lease_owner TEXT,
    lease_token TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES job_contracts(job_id),
    attempt_num INTEGER NOT NULL,
    worker_type TEXT,
    sandbox_path TEXT,
    pre_manifest_sha256 TEXT,
    post_manifest_sha256 TEXT,
    diff_patch_path TEXT,
    worker_status TEXT,
    failure_class TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    UNIQUE (job_id, attempt_num)
);

CREATE TABLE IF NOT EXISTS qa_verifications (
    verification_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES job_attempts(attempt_id),
    job_id TEXT NOT NULL REFERENCES job_contracts(job_id),
    commands_run_json TEXT NOT NULL,
    all_exit_zero INTEGER NOT NULL,
    allowlist_diff_ok INTEGER NOT NULL,
    protected_roots_untouched INTEGER NOT NULL,
    role_d_review_json TEXT,
    gate_verdict TEXT NOT NULL,
    evidence_manifest_sha256 TEXT,
    verified_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE,
    job_id TEXT,
    attempt_id TEXT,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contracts_batch ON job_contracts(batch_id, batch_ordinal);
CREATE INDEX IF NOT EXISTS idx_contracts_project ON job_contracts(project_id);
CREATE INDEX IF NOT EXISTS idx_attempts_job ON job_attempts(job_id);
CREATE INDEX IF NOT EXISTS idx_qa_attempt ON qa_verifications(attempt_id);
CREATE INDEX IF NOT EXISTS idx_audit_job ON audit_events(job_id);

CREATE TRIGGER IF NOT EXISTS trg_audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_contract_immutable
BEFORE UPDATE OF contract_json, contract_sha256, job_id, batch_id, batch_ordinal, project_id
ON job_contracts
BEGIN SELECT RAISE(ABORT, 'job_contracts payload is immutable'); END;
"""


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso(dt: _dt.datetime) -> str:
    """Fixed-width UTC ISO-8601 (lexicographically sortable)."""
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(text: str) -> _dt.datetime:
    return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=_dt.timezone.utc)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Fields that change over the life of a contract and are therefore excluded from
# the approved digest.
CONTRACT_VOLATILE_FIELDS = ("contract_sha256", "approval_state", "approved_by", "approved_at")


def contract_digest(contract: dict) -> str:
    """Canonical SHA-256 of a contract excluding volatile approval fields."""
    body = {k: v for k, v in contract.items() if k not in CONTRACT_VOLATILE_FIELDS}
    return sha256_text(canonical_json(body))


class StateDB:
    """Thread-safe SQLite connection wrapper (WAL + foreign keys)."""

    def __init__(self, path: str | Path, now_fn=utc_now):
        self.path = str(path)
        self.now_fn = now_fn
        self._lock = threading.RLock()
        self._closed = False
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self.init_schema()

    # ------------------------------------------------------------------ basics
    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def now_iso(self) -> str:
        return iso(self.now_fn())

    @property
    def conn(self) -> sqlite3.Connection:
        if self._closed:
            raise sqlite3.ProgrammingError("StateDB is closed")
        return self._conn

    def pragma(self, name: str) -> Any:
        row = self.conn.execute(f"PRAGMA {name}").fetchone()
        return row[0] if row else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic write transaction (BEGIN IMMEDIATE); re-entrant via savepoints."""
        with self._lock:
            conn = self.conn
            if conn.in_transaction:
                self._sp_counter = getattr(self, "_sp_counter", 0) + 1
                name = f"sp_{self._sp_counter}"
                conn.execute(f"SAVEPOINT {name}")
                try:
                    yield conn
                except BaseException:
                    conn.execute(f"ROLLBACK TO {name}")
                    conn.execute(f"RELEASE {name}")
                    raise
                else:
                    conn.execute(f"RELEASE {name}")
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                if self.path != ":memory:":
                    try:
                        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.DatabaseError:
                        pass
            finally:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "StateDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ audit
    def append_audit(
        self,
        event_type: str,
        *,
        job_id: str | None = None,
        attempt_id: str | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
        payload: dict | None = None,
        idempotency_key: str | None = None,
    ) -> int | None:
        """Append an audit event; returns event_seq, or None when the idempotency key already exists."""
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO audit_events(idempotency_key, job_id, attempt_id, event_type,"
                " from_state, to_state, payload_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    idempotency_key,
                    job_id,
                    attempt_id,
                    event_type,
                    from_state,
                    to_state,
                    canonical_json(payload or {}),
                    self.now_iso(),
                ),
            )
            return cur.lastrowid if cur.rowcount else None

    def audit_events(self, job_id: str | None = None) -> list[sqlite3.Row]:
        if job_id is None:
            return self.query("SELECT * FROM audit_events ORDER BY event_seq")
        return self.query("SELECT * FROM audit_events WHERE job_id=? ORDER BY event_seq", (job_id,))

    # ------------------------------------------------------------------ helpers
    def register_project(
        self,
        project_id: str,
        name: str,
        canonical_root: str,
        protected_paths: list[str] | None = None,
        serena_policy: str = "READ_ONLY_NO_9121",
    ) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO projects(project_id,name,canonical_root,protected_paths_json,"
                "serena_policy,created_at) VALUES (?,?,?,?,?,?)",
                (project_id, name, canonical_root, canonical_json(protected_paths or []), serena_policy, self.now_iso()),
            )

    def get_project(self, project_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM projects WHERE project_id=?", (project_id,))

    def get_contract_row(self, job_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM job_contracts WHERE job_id=?", (job_id,))

    def get_state_row(self, job_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM job_states WHERE job_id=?", (job_id,))

    def get_attempt(self, attempt_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM job_attempts WHERE attempt_id=?", (attempt_id,))

    def list_attempts(self, job_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_num", (job_id,))

    def table_names(self) -> list[str]:
        rows = self.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        return sorted(r["name"] for r in rows)
