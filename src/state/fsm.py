"""Strict job finite-state machine with exclusive per-project writer lease + generation fencing."""
from __future__ import annotations

import datetime as _dt
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .db import StateDB, canonical_json, contract_digest, iso, parse_iso

# ---------------------------------------------------------------- states
DRAFT = "DRAFT"
PLAN_APPROVED = "PLAN_APPROVED"
QUEUED = "QUEUED"
LEASED = "LEASED"
PREFLIGHT_PASSED = "PREFLIGHT_PASSED"
PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
RUNNING = "RUNNING"
QA_RUNNING = "QA_RUNNING"
REVIEWING = "REVIEWING"
ACCEPTED = "ACCEPTED"
HANDOFF_VERIFIED = "HANDOFF_VERIFIED"
CLOSED = "CLOSED"
REWORK_REQUIRED = "REWORK_REQUIRED"
WAITING_APPROVAL = "WAITING_APPROVAL"
PAUSED_NEEDS_RECONCILIATION = "PAUSED_NEEDS_RECONCILIATION"
BLOCKED = "BLOCKED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

ALL_STATES = frozenset(
    {
        DRAFT, PLAN_APPROVED, QUEUED, LEASED, PREFLIGHT_PASSED, PREFLIGHT_FAILED, RUNNING, QA_RUNNING,
        REVIEWING, ACCEPTED, HANDOFF_VERIFIED, CLOSED, REWORK_REQUIRED, WAITING_APPROVAL,
        PAUSED_NEEDS_RECONCILIATION, BLOCKED, FAILED, CANCELLED,
    }
)

# States in which a worker/supervisor holds the exclusive writer lease.
LEASE_HELD_STATES = frozenset({LEASED, PREFLIGHT_PASSED, RUNNING, QA_RUNNING, REVIEWING})
# States a crash can interrupt (reconciler scope).
INTERRUPTIBLE_STATES = LEASE_HELD_STATES
TERMINAL_STATES = frozenset({CLOSED, FAILED, CANCELLED})
DONE_STATES = frozenset({HANDOFF_VERIFIED, CLOSED})

TRANSITIONS: dict[str, frozenset[str]] = {
    DRAFT: frozenset({PLAN_APPROVED, WAITING_APPROVAL, CANCELLED}),
    WAITING_APPROVAL: frozenset({PLAN_APPROVED, BLOCKED, CANCELLED}),
    PLAN_APPROVED: frozenset({QUEUED, CANCELLED}),
    QUEUED: frozenset({LEASED, BLOCKED, CANCELLED}),
    LEASED: frozenset({PREFLIGHT_PASSED, PREFLIGHT_FAILED, PAUSED_NEEDS_RECONCILIATION, QUEUED, CANCELLED}),
    PREFLIGHT_PASSED: frozenset({RUNNING, PAUSED_NEEDS_RECONCILIATION, CANCELLED}),
    PREFLIGHT_FAILED: frozenset({QUEUED, BLOCKED, FAILED, CANCELLED}),
    RUNNING: frozenset({QA_RUNNING, REWORK_REQUIRED, BLOCKED, FAILED, PAUSED_NEEDS_RECONCILIATION, CANCELLED}),
    QA_RUNNING: frozenset({REVIEWING, REWORK_REQUIRED, BLOCKED, FAILED, PAUSED_NEEDS_RECONCILIATION, CANCELLED}),
    REVIEWING: frozenset({ACCEPTED, REWORK_REQUIRED, BLOCKED, FAILED, PAUSED_NEEDS_RECONCILIATION, CANCELLED}),
    ACCEPTED: frozenset({HANDOFF_VERIFIED, FAILED}),
    HANDOFF_VERIFIED: frozenset({CLOSED}),
    REWORK_REQUIRED: frozenset({QUEUED, BLOCKED, FAILED, CANCELLED}),
    PAUSED_NEEDS_RECONCILIATION: frozenset({QUEUED, WAITING_APPROVAL, BLOCKED, FAILED, CANCELLED}),
    BLOCKED: frozenset({WAITING_APPROVAL, CANCELLED, FAILED}),
    CLOSED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}
assert set(TRANSITIONS) == set(ALL_STATES)


# ---------------------------------------------------------------- errors
class FSMError(Exception):
    """Base class."""


class IllegalTransition(FSMError):
    pass


class StaleLeaseError(FSMError):
    pass


class LeaseConflict(FSMError):
    pass


class GuardError(FSMError):
    pass


@dataclass
class Lease:
    job_id: str
    owner: str
    token: str
    generation: int
    expires_at: str


@dataclass
class TransitionResult:
    job_id: str
    from_state: str
    to_state: str
    applied: bool
    duplicate: bool = False
    event_seq: int | None = None


def is_allowed(from_state: str, to_state: str) -> bool:
    return to_state in TRANSITIONS.get(from_state, frozenset())


class JobFSM:
    """Atomic state transitions over :class:`StateDB`."""

    def __init__(self, db: StateDB):
        self.db = db

    # ------------------------------------------------------------ creation
    def create_job(self, contract: dict, *, project_id: str | None = None) -> str:
        """Insert contract + DRAFT state atomically. Returns the contract SHA-256."""
        job_id = contract["job_id"]
        sha = contract_digest(contract)
        # NOTE: a caller-claimed contract_sha256 is validated by the Supervisor (it may legitimately use the
        # backlog's digest convention); the DB always stores/enforces the volatile-field-free digest.
        stored = {k: v for k, v in contract.items()}
        stored["contract_sha256"] = sha
        pid = project_id or contract["project_id"]
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO job_contracts(job_id,batch_id,batch_ordinal,project_id,depends_on_json,"
                "contract_json,contract_sha256,approval_state) VALUES (?,?,?,?,?,?,?,'PENDING')",
                (
                    job_id,
                    str(contract.get("batch_id", "")),
                    int(contract.get("batch_ordinal", 1)),
                    pid,
                    canonical_json(list(contract.get("dependencies") or contract.get("depends_on") or [])),
                    canonical_json(stored),
                    sha,
                ),
            )
            c.execute(
                "INSERT INTO job_states(job_id,current_state,updated_at) VALUES (?,?,?)",
                (job_id, DRAFT, self.db.now_iso()),
            )
            self.db.append_audit(
                "JOB_CREATED", job_id=job_id, to_state=DRAFT, payload={"contract_sha256": sha},
                idempotency_key=f"create:{job_id}:{sha}",
            )
        return sha

    def approve_contract(self, job_id: str, approved_by: str) -> None:
        with self.db.transaction() as c:
            row = self.db.get_contract_row(job_id)
            if row is None:
                raise GuardError(f"unknown job {job_id}")
            if contract_digest(json.loads(row["contract_json"])) != row["contract_sha256"]:
                raise GuardError("contract payload does not match stored SHA-256; refusing approval")
            c.execute(
                "UPDATE job_contracts SET approval_state='APPROVED', approved_by=?, approved_at=? WHERE job_id=?",
                (approved_by, self.db.now_iso(), job_id),
            )
            self.db.append_audit(
                "CONTRACT_APPROVED", job_id=job_id, payload={"approved_by": approved_by, "sha256": row["contract_sha256"]},
                idempotency_key=f"approve:{job_id}:{row['contract_sha256']}",
            )

    # ------------------------------------------------------------ queries
    def state(self, job_id: str) -> str:
        row = self.db.get_state_row(job_id)
        if row is None:
            raise FSMError(f"unknown job {job_id}")
        return row["current_state"]

    def get(self, job_id: str):
        return self.db.get_state_row(job_id)

    # ------------------------------------------------------------ leases
    def _project_of(self, job_id: str) -> str:
        row = self.db.get_contract_row(job_id)
        if row is None:
            raise FSMError(f"unknown job {job_id}")
        return row["project_id"]

    def acquire_lease(self, job_id: str, owner: str, ttl_seconds: float = 600.0) -> Lease:
        """QUEUED -> LEASED with an exclusive per-project writer lease and a fresh fencing generation."""
        now = self.db.now_fn()
        with self.db.transaction() as c:
            row = self.db.get_state_row(job_id)
            if row is None:
                raise FSMError(f"unknown job {job_id}")
            if row["current_state"] != QUEUED:
                raise IllegalTransition(f"{job_id}: lease can only be acquired from QUEUED (is {row['current_state']})")
            project_id = self._project_of(job_id)
            holder = c.execute(
                "SELECT s.job_id, s.lease_expires_at FROM job_states s JOIN job_contracts k ON k.job_id=s.job_id "
                "WHERE k.project_id=? AND s.job_id<>? AND s.lease_token IS NOT NULL",
                (project_id, job_id),
            ).fetchone()
            if holder is not None:
                raise LeaseConflict(
                    f"project {project_id} writer lease held by {holder['job_id']} (expires {holder['lease_expires_at']});"
                    " reconcile or release first"
                )
            max_gen = c.execute(
                "SELECT COALESCE(MAX(s.lease_generation),0) FROM job_states s JOIN job_contracts k ON k.job_id=s.job_id "
                "WHERE k.project_id=?",
                (project_id,),
            ).fetchone()[0]
            lease = Lease(
                job_id=job_id,
                owner=owner,
                token=uuid.uuid4().hex,
                generation=max_gen + 1,
                expires_at=iso(now + _dt.timedelta(seconds=ttl_seconds)),
            )
            c.execute(
                "UPDATE job_states SET current_state=?, lease_owner=?, lease_token=?, lease_generation=?,"
                " lease_expires_at=?, updated_at=? WHERE job_id=?",
                (LEASED, owner, lease.token, lease.generation, lease.expires_at, self.db.now_iso(), job_id),
            )
            self.db.append_audit(
                "LEASE_ACQUIRED", job_id=job_id, from_state=QUEUED, to_state=LEASED,
                payload={"owner": owner, "generation": lease.generation, "expires_at": lease.expires_at},
            )
        return lease

    def check_lease(self, job_id: str, token: str | None, generation: int | None) -> tuple[bool, str]:
        row = self.db.get_state_row(job_id)
        if row is None:
            return False, "unknown job"
        if not row["lease_token"]:
            return False, "no active lease (fenced or released)"
        if token is None or token != row["lease_token"]:
            return False, "lease token mismatch (stale or forged writer)"
        if generation is None or int(generation) != int(row["lease_generation"]):
            return False, f"lease generation mismatch (got {generation}, current {row['lease_generation']})"
        if row["lease_expires_at"] and parse_iso(row["lease_expires_at"]) < self.db.now_fn():
            return False, "lease expired"
        return True, "ok"

    def renew_lease(self, job_id: str, token: str, generation: int, ttl_seconds: float = 600.0) -> Lease:
        with self.db.transaction() as c:
            ok, why = self.check_lease(job_id, token, generation)
            if not ok:
                raise StaleLeaseError(f"{job_id}: cannot renew: {why}")
            exp = iso(self.db.now_fn() + _dt.timedelta(seconds=ttl_seconds))
            c.execute("UPDATE job_states SET lease_expires_at=?, updated_at=? WHERE job_id=?",
                      (exp, self.db.now_iso(), job_id))
            row = self.db.get_state_row(job_id)
            return Lease(job_id, row["lease_owner"], token, int(generation), exp)

    def fence(self, job_id: str, reason: str) -> int:
        """Invalidate the current lease token (writer fence); bumps generation. Returns new generation."""
        with self.db.transaction() as c:
            row = self.db.get_state_row(job_id)
            new_gen = int(row["lease_generation"]) + 1
            c.execute(
                "UPDATE job_states SET lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,"
                " lease_generation=?, updated_at=? WHERE job_id=?",
                (new_gen, self.db.now_iso(), job_id),
            )
            self.db.append_audit("LEASE_FENCED", job_id=job_id, payload={"reason": reason, "new_generation": new_gen})
            return new_gen

    # ------------------------------------------------------------ transitions
    def transition(
        self,
        job_id: str,
        to_state: str,
        *,
        lease_token: str | None = None,
        lease_generation: int | None = None,
        idempotency_key: str | None = None,
        attempt_id: str | None = None,
        payload: dict | None = None,
        actor: str = "supervisor",
        fence: bool = False,
        bump_rework: bool = False,
        bump_infra: bool = False,
        _internal: bool = False,
    ) -> TransitionResult:
        try:
            return self._transition(
                job_id, to_state, lease_token, lease_generation, idempotency_key, attempt_id, payload, actor,
                fence, bump_rework, bump_infra, _internal,
            )
        except FSMError as exc:
            try:
                cur = self.db.get_state_row(job_id)
                self.db.append_audit(
                    "TRANSITION_REJECTED", job_id=job_id, attempt_id=attempt_id,
                    from_state=cur["current_state"] if cur else None, to_state=to_state,
                    payload={"error": type(exc).__name__, "detail": str(exc), "actor": actor},
                )
            except Exception:  # pragma: no cover - audit best effort
                pass
            raise

    def _transition(self, job_id, to_state, token, generation, idem, attempt_id, payload, actor, fence,
                    bump_rework, bump_infra, internal) -> TransitionResult:
        if to_state not in ALL_STATES:
            raise IllegalTransition(f"unknown state {to_state}")
        with self.db.transaction() as c:
            if idem:
                dup = c.execute("SELECT event_seq, from_state, to_state FROM audit_events WHERE idempotency_key=?",
                                (idem,)).fetchone()
                if dup is not None:
                    cur = self.db.get_state_row(job_id)
                    return TransitionResult(job_id, dup["from_state"] or "", dup["to_state"] or to_state, False, True,
                                            dup["event_seq"])
            row = self.db.get_state_row(job_id)
            if row is None:
                raise FSMError(f"unknown job {job_id}")
            from_state = row["current_state"]
            if not is_allowed(from_state, to_state):
                raise IllegalTransition(f"{job_id}: {from_state} -> {to_state} is forbidden")
            if from_state == QUEUED and to_state == LEASED and not internal:
                raise IllegalTransition("QUEUED -> LEASED must go through acquire_lease()")
            # --- guards
            crow = self.db.get_contract_row(job_id)
            if to_state == PLAN_APPROVED and crow["approval_state"] != "APPROVED":
                raise GuardError(f"{job_id}: contract is not APPROVED")
            if to_state == QUEUED and crow["approval_state"] != "APPROVED":
                raise GuardError(f"{job_id}: contract is not APPROVED")
            if from_state in LEASE_HELD_STATES and not fence:
                ok, why = self.check_lease(job_id, token, generation)
                if not ok:
                    raise StaleLeaseError(f"{job_id}: {from_state} -> {to_state} rejected: {why}")
            if to_state == ACCEPTED:
                self._guard_accept(job_id, row)
            # --- apply
            sets = ["current_state=?", "updated_at=?"]
            args: list[Any] = [to_state, self.db.now_iso()]
            if attempt_id is not None:
                sets.append("active_attempt_id=?")
                args.append(attempt_id)
            if bump_rework:
                sets.append("rework_count=rework_count+1")
            if bump_infra:
                sets.append("infra_retry_count=infra_retry_count+1")
            if to_state not in LEASE_HELD_STATES:
                sets += ["lease_owner=NULL", "lease_token=NULL", "lease_expires_at=NULL"]
            if fence:
                sets.append("lease_generation=lease_generation+1")
                if to_state in LEASE_HELD_STATES:
                    sets += ["lease_owner=NULL", "lease_token=NULL", "lease_expires_at=NULL"]
            args.append(job_id)
            c.execute(f"UPDATE job_states SET {', '.join(sets)} WHERE job_id=?", args)
            body = dict(payload or {})
            body.setdefault("actor", actor)
            seq = self.db.append_audit(
                "STATE_TRANSITION", job_id=job_id, attempt_id=attempt_id or row["active_attempt_id"],
                from_state=from_state, to_state=to_state, payload=body, idempotency_key=idem,
            )
            return TransitionResult(job_id, from_state, to_state, True, False, seq)

    def _guard_accept(self, job_id: str, state_row) -> None:
        attempt_id = state_row["active_attempt_id"]
        if not attempt_id:
            raise GuardError(f"{job_id}: ACCEPTED requires an active attempt")
        v = self.db.query_one(
            "SELECT * FROM qa_verifications WHERE job_id=? AND attempt_id=? ORDER BY verified_at DESC, rowid DESC LIMIT 1",
            (job_id, attempt_id),
        )
        if v is None:
            raise GuardError(f"{job_id}: ACCEPTED requires an independent QA verification for {attempt_id}")
        if (
            v["gate_verdict"] != "ACCEPTED"
            or not v["all_exit_zero"]
            or not v["allowlist_diff_ok"]
            or not v["protected_roots_untouched"]
        ):
            raise GuardError(f"{job_id}: QA gate verdict is {v['gate_verdict']}; ACCEPTED forbidden")

    # ------------------------------------------------------------ attempts
    def create_attempt(self, job_id: str, *, worker_type: str, lease_token: str, lease_generation: int,
                       sandbox_path: str | None = None) -> str:
        with self.db.transaction() as c:
            ok, why = self.check_lease(job_id, lease_token, lease_generation)
            if not ok:
                raise StaleLeaseError(f"{job_id}: cannot create attempt: {why}")
            n = c.execute("SELECT COALESCE(MAX(attempt_num),0) FROM job_attempts WHERE job_id=?", (job_id,)).fetchone()[0] + 1
            attempt_id = f"{job_id}-A{n}"
            c.execute(
                "INSERT INTO job_attempts(attempt_id,job_id,attempt_num,worker_type,sandbox_path,started_at)"
                " VALUES (?,?,?,?,?,?)",
                (attempt_id, job_id, n, worker_type, sandbox_path, self.db.now_iso()),
            )
            c.execute("UPDATE job_states SET active_attempt_id=?, updated_at=? WHERE job_id=?",
                      (attempt_id, self.db.now_iso(), job_id))
            self.db.append_audit("ATTEMPT_CREATED", job_id=job_id, attempt_id=attempt_id,
                                 payload={"attempt_num": n, "worker_type": worker_type})
            return attempt_id

    _ATTEMPT_COLS = {
        "sandbox_path", "pre_manifest_sha256", "post_manifest_sha256", "diff_patch_path", "worker_status",
        "failure_class", "ended_at", "worker_type",
    }

    def update_attempt(self, attempt_id: str, **fields) -> None:
        bad = set(fields) - self._ATTEMPT_COLS
        if bad:
            raise GuardError(f"unknown attempt columns {bad}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE job_attempts SET {sets} WHERE attempt_id=?", [*fields.values(), attempt_id])

    def finish_attempt(self, attempt_id: str, **fields) -> None:
        fields.setdefault("ended_at", self.db.now_iso())
        self.update_attempt(attempt_id, **fields)

    def bump_counter(self, job_id: str, *, rework: bool = False, infra: bool = False) -> None:
        sets = []
        if rework:
            sets.append("rework_count=rework_count+1")
        if infra:
            sets.append("infra_retry_count=infra_retry_count+1")
        if sets:
            self.db.execute(f"UPDATE job_states SET {', '.join(sets)} WHERE job_id=?", (job_id,))
