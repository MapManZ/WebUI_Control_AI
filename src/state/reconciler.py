"""Crash / expired-lease reconciler. Never auto-accepts partial output."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import fsm
from .db import StateDB, parse_iso
from .fsm import INTERRUPTIBLE_STATES, JobFSM


@dataclass
class ReconcileFinding:
    job_id: str
    prior_state: str
    reason: str
    attempt_id: str | None
    stale_generation: int
    new_generation: int | None = None
    new_state: str | None = None


@dataclass
class ReconcileReport:
    findings: list[ReconcileFinding] = field(default_factory=list)

    @property
    def paused(self) -> list[str]:
        return [f.job_id for f in self.findings if f.new_state == fsm.PAUSED_NEEDS_RECONCILIATION]


class Reconciler:
    def __init__(self, db: StateDB, machine: JobFSM | None = None):
        self.db = db
        self.fsm = machine or JobFSM(db)

    def find_interrupted(self, *, assume_all_stale: bool = False) -> list[ReconcileFinding]:
        """Jobs in a lease-held state whose lease expired / vanished (or all of them after a restart)."""
        now = self.db.now_fn()
        marks = ",".join("?" for _ in INTERRUPTIBLE_STATES)
        rows = self.db.query(
            f"SELECT * FROM job_states WHERE current_state IN ({marks}) ORDER BY job_id", tuple(sorted(INTERRUPTIBLE_STATES))
        )
        out: list[ReconcileFinding] = []
        for r in rows:
            reason = None
            if assume_all_stale:
                reason = "process restart: in-flight run has no live owner"
            elif not r["lease_token"]:
                reason = "in-flight state without lease token"
            elif r["lease_expires_at"] and parse_iso(r["lease_expires_at"]) < now:
                reason = "lease expired"
            if reason:
                out.append(ReconcileFinding(r["job_id"], r["current_state"], reason, r["active_attempt_id"],
                                            int(r["lease_generation"])))
        return out

    def reconcile(self, *, assume_all_stale: bool = False) -> ReconcileReport:
        report = ReconcileReport()
        for f in self.find_interrupted(assume_all_stale=assume_all_stale):
            # re-read inside transaction to avoid racing a live worker that just renewed
            with self.db.transaction():
                row = self.db.get_state_row(f.job_id)
                if row is None or row["current_state"] not in INTERRUPTIBLE_STATES:
                    continue
                if not assume_all_stale and row["lease_token"] and row["lease_expires_at"] \
                        and parse_iso(row["lease_expires_at"]) >= self.db.now_fn():
                    continue
                res = self.fsm.transition(
                    f.job_id,
                    fsm.PAUSED_NEEDS_RECONCILIATION,
                    fence=True,
                    actor="reconciler",
                    payload={
                        "reason": f.reason,
                        "prior_state": f.prior_state,
                        "stale_generation": f.stale_generation,
                        "policy": "partial outputs are never auto-accepted",
                    },
                )
                new_row = self.db.get_state_row(f.job_id)
                f.new_generation = int(new_row["lease_generation"])
                f.new_state = res.to_state
                if f.attempt_id:
                    self.fsm.finish_attempt(f.attempt_id, worker_status="INTERRUPTED", failure_class="AMBIGUOUS")
                self.db.append_audit(
                    "RECONCILED_PAUSED", job_id=f.job_id, attempt_id=f.attempt_id,
                    from_state=f.prior_state, to_state=fsm.PAUSED_NEEDS_RECONCILIATION,
                    payload={"reason": f.reason, "fenced_generation": f.stale_generation,
                             "new_generation": f.new_generation},
                )
            report.findings.append(f)
        return report

    def resolve(self, job_id: str, decision: str, *, actor: str = "operator") -> fsm.TransitionResult:
        """Operator decision on a paused job: 'requeue' (fresh attempt), 'block' or 'fail'."""
        target = {"requeue": fsm.QUEUED, "block": fsm.BLOCKED, "fail": fsm.FAILED}.get(decision)
        if target is None:
            raise ValueError("decision must be requeue|block|fail")
        return self.fsm.transition(job_id, target, actor=actor, payload={"resolution": decision})
