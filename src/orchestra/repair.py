"""Failure classification and bounded repair loop."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from src.state import fsm
from src.state.db import StateDB, canonical_json
from src.state.fsm import JobFSM

CODE_DEFECT = "CODE_DEFECT"
TEST_DEFECT = "TEST_DEFECT"
INFRA_TRANSIENT = "INFRA_TRANSIENT"
TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
SCOPE_VIOLATION = "SCOPE_VIOLATION"
POLICY_BLOCK = "POLICY_BLOCK"
AMBIGUOUS = "AMBIGUOUS"

FAILURE_CLASSES = (CODE_DEFECT, TEST_DEFECT, INFRA_TRANSIENT, TOOL_UNAVAILABLE, SCOPE_VIOLATION, POLICY_BLOCK, AMBIGUOUS)

DEFAULT_MAX_REWORK = 3
DEFAULT_MAX_INFRA = 2

_TEST_DEFECT_RE = re.compile(r"(?is)test[^\n]*\.py[^\n]*\n.*?(SyntaxError|ImportError|ModuleNotFoundError)|"
                             r"ERROR: (?:setUpClass|setUpModule)")


class RepairViolation(Exception):
    pass


@dataclass
class RepairDecision:
    failure_class: str
    action: str  # REWORK | INFRA_RETRY | BLOCK | FAIL | PAUSE
    to_state: str
    reason: str
    rework_count: int
    infra_retry_count: int


def classify_failure(*, worker_status: str | None = None, gate=None, qa_report=None, exception: BaseException | None = None,
                     scope_violation: bool = False, policy_block: bool = False) -> str:
    hints = set(getattr(gate, "hints", None) or [])
    if scope_violation or "SCOPE" in hints:
        return SCOPE_VIOLATION
    if policy_block or "POLICY" in hints or (qa_report is not None and getattr(qa_report, "security_violation", False)):
        return POLICY_BLOCK
    if worker_status == "BLOCKED_TOOL_UNAVAILABLE" or "NOT_VERIFIED" in hints:
        return TOOL_UNAVAILABLE
    if worker_status == "TIMEOUT" or isinstance(exception, (TimeoutError, OSError, ConnectionError)):
        return INFRA_TRANSIENT
    if qa_report is not None and any(getattr(c, "timed_out", False) for c in getattr(qa_report, "checks", [])):
        return INFRA_TRANSIENT
    if hints & {"CONTRACT", "LEASE", "STALE_QA", "ROLE_D"}:
        return AMBIGUOUS
    if "CHECKS_FAILED" in hints or worker_status in ("FAILED",):
        if qa_report is not None:
            for c in getattr(qa_report, "checks", []):
                if c.status == "FAILED" and _TEST_DEFECT_RE.search((c.stdout or "") + "\n" + (c.stderr or "")):
                    return TEST_DEFECT
        return CODE_DEFECT
    if worker_status == "CANCELLED" or exception is not None:
        return AMBIGUOUS
    return AMBIGUOUS


class RepairController:
    def __init__(self, db: StateDB, machine: JobFSM | None = None):
        self.db = db
        self.fsm = machine or JobFSM(db)

    def _limits(self, job_id: str) -> tuple[int, int]:
        contract = json.loads(self.db.get_contract_row(job_id)["contract_json"])
        return (int(contract.get("max_rework_cycles", DEFAULT_MAX_REWORK)),
                int(contract.get("max_infra_retries", DEFAULT_MAX_INFRA)))

    def decide(self, job_id: str, failure_class: str) -> RepairDecision:
        if failure_class not in FAILURE_CLASSES:
            raise ValueError(f"unknown failure class {failure_class}")
        row = self.db.get_state_row(job_id)
        max_rework, max_infra = self._limits(job_id)
        rw, inf = int(row["rework_count"]), int(row["infra_retry_count"])

        def d(action, to_state, reason):
            return RepairDecision(failure_class, action, to_state, reason, rw, inf)

        if failure_class == SCOPE_VIOLATION:
            return d("BLOCK", fsm.BLOCKED, "scope violation: immediate block, no rework")
        if failure_class == POLICY_BLOCK:
            return d("BLOCK", fsm.BLOCKED, "policy block: immediate block, no rework")
        if failure_class == TOOL_UNAVAILABLE:
            return d("BLOCK", fsm.BLOCKED, "required tool unavailable: cannot be fixed by rework")
        if failure_class == AMBIGUOUS:
            return d("PAUSE", fsm.PAUSED_NEEDS_RECONCILIATION, "ambiguous outcome: needs reconciliation")
        if failure_class == INFRA_TRANSIENT:
            if inf < max_infra:
                return d("INFRA_RETRY", fsm.REWORK_REQUIRED, f"infra retry {inf + 1}/{max_infra}")
            return d("FAIL", fsm.FAILED, f"infra retry budget exhausted ({max_infra})")
        # CODE_DEFECT / TEST_DEFECT
        if rw < max_rework:
            return d("REWORK", fsm.REWORK_REQUIRED, f"rework cycle {rw + 1}/{max_rework}")
        return d("FAIL", fsm.FAILED, f"rework budget exhausted ({max_rework})")

    def apply(self, job_id: str, attempt_id: str | None, failure_class: str, *, lease_token: str | None,
              lease_generation: int | None, detail: dict | None = None) -> RepairDecision:
        decision = self.decide(job_id, failure_class)
        payload = {"failure_class": failure_class, "action": decision.action, "reason": decision.reason}
        payload.update(detail or {})
        if attempt_id:
            self.fsm.finish_attempt(attempt_id, failure_class=failure_class)
        self.fsm.transition(
            job_id, decision.to_state, lease_token=lease_token, lease_generation=lease_generation,
            attempt_id=attempt_id, payload=payload, actor="repair",
            bump_rework=decision.action == "REWORK", bump_infra=decision.action == "INFRA_RETRY",
            fence=decision.action == "PAUSE",
        )
        return decision

    # -- acceptance_checks are frozen at approval; rework may never rewrite them
    def assert_checks_immutable(self, job_id: str, proposed_contract: dict) -> None:
        approved = json.loads(self.db.get_contract_row(job_id)["contract_json"])
        if canonical_json(proposed_contract.get("acceptance_checks")) != canonical_json(approved.get("acceptance_checks")):
            self.db.append_audit("ACCEPTANCE_CHECKS_MUTATION_REJECTED", job_id=job_id,
                                 payload={"detail": "rework attempted to alter acceptance_checks"})
            raise RepairViolation("acceptance_checks cannot be mutated during rework; a new approved contract is required")

    def requeue_for_rework(self, job_id: str, proposed_contract: dict | None = None) -> fsm.TransitionResult:
        if proposed_contract is not None:
            self.assert_checks_immutable(job_id, proposed_contract)
        return self.fsm.transition(job_id, fsm.QUEUED, actor="repair", payload={"requeue": "rework"})
