"""Role-D (independent semantic reviewer) adapter. Review is only valid when bound to job/attempt/evidence."""
from __future__ import annotations

import hashlib
import json
from typing import Callable

VERDICTS = ("APPROVE", "REJECT")


def _digest(review: dict) -> str:
    body = {k: v for k, v in review.items() if k != "review_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def seal_review(review: dict) -> dict:
    review = dict(review)
    review["review_sha256"] = _digest(review)
    return review


def build_review_request(job_id: str, attempt_id: str, evidence_sha256: str, contract_sha256: str,
                         changed_files: list[str]) -> dict:
    return {
        "role": "D",
        "job_id": job_id,
        "attempt_id": attempt_id,
        "evidence_sha256": evidence_sha256,
        "contract_sha256": contract_sha256,
        "changed_files": sorted(changed_files),
        "instructions": "Independently review the changed files against the contract goal/out_of_scope. "
                        "Reply with verdict APPROVE|REJECT, findings, and echo job_id/attempt_id/evidence_sha256.",
    }


def verify_review(review: dict | None, *, job_id: str, attempt_id: str, evidence_sha256: str,
                  worker_type: str | None = None) -> tuple[bool, list[str]]:
    """Machine-verify a Role-D review. Returns (ok, reasons)."""
    if not review:
        return False, ["Role-D review required by contract but missing"]
    reasons: list[str] = []
    if review.get("role", "D") != "D":
        reasons.append("review role is not D")
    if review.get("job_id") != job_id:
        reasons.append(f"review job_id {review.get('job_id')!r} != {job_id!r}")
    if review.get("attempt_id") != attempt_id:
        reasons.append(f"review attempt_id {review.get('attempt_id')!r} != {attempt_id!r}")
    if review.get("evidence_sha256") != evidence_sha256:
        reasons.append("review evidence_sha256 does not match evidence manifest")
    if review.get("review_sha256") != _digest(review):
        reasons.append("review_sha256 tampered or missing")
    if review.get("verdict") not in VERDICTS:
        reasons.append("review verdict malformed")
    elif review["verdict"] != "APPROVE":
        reasons.append("Role-D verdict is REJECT")
    if worker_type and str(review.get("reviewer", "")).lower() == worker_type.lower():
        reasons.append("reviewer is the same agent as the worker (not independent)")
    return (not reasons), reasons


class RoleDAdapter:
    """Wraps a reviewer callable ``fn(request:dict)->dict`` (e.g. WebUI packet round-trip or CLI harness)."""

    def __init__(self, reviewer_fn: Callable[[dict], dict] | None = None, reviewer_name: str = "role-d"):
        self.reviewer_fn = reviewer_fn
        self.reviewer_name = reviewer_name

    @property
    def available(self) -> bool:
        return self.reviewer_fn is not None

    def review(self, request: dict) -> dict | None:
        if not self.reviewer_fn:
            return None
        out = dict(self.reviewer_fn(request))
        out.setdefault("role", "D")
        out.setdefault("reviewer", self.reviewer_name)
        out.setdefault("job_id", request["job_id"])
        out.setdefault("attempt_id", request["attempt_id"])
        out.setdefault("evidence_sha256", request["evidence_sha256"])
        return seal_review(out)
