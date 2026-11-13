"""Deterministic acceptance gate G0-G5. Trusts only DB + on-disk evidence, never worker claims."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field

from src.sandbox.manifest_hasher import changed_paths, diff_manifests, manifest_digest, verify_protected_untouched
from src.sandbox.path_guard import PathPolicy, PathViolation, is_within
from src.state.db import StateDB, canonical_json, contract_digest, sha256_text
from src.state.fsm import JobFSM

from .role_d_adapter import verify_review
from .runner import QAReport, contract_check_ids

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
NOT_VERIFIED = "NOT_VERIFIED"


@dataclass
class GateResult:
    verdict: str
    gates: dict[str, dict] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    hints: set[str] = field(default_factory=set)  # SCOPE | POLICY | STALE_QA | CHECKS_FAILED | NOT_VERIFIED | CONTRACT | LEASE | ROLE_D
    evidence_manifest_sha256: str = ""
    changed_files: list[str] = field(default_factory=list)
    verification_id: str | None = None

    @property
    def accepted(self) -> bool:
        return self.verdict == ACCEPTED

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "gates": self.gates, "reasons": self.reasons, "hints": sorted(self.hints),
                "evidence_manifest_sha256": self.evidence_manifest_sha256, "changed_files": self.changed_files}


def evidence_sha256(contract_sha256: str, attempt_id: str, pre_digest: str, post_digest: str,
                    qa_report_sha256: str, changed_files: list[str]) -> str:
    return sha256_text(canonical_json({
        "contract_sha256": contract_sha256, "attempt_id": attempt_id, "pre": pre_digest, "post": post_digest,
        "qa_report_sha256": qa_report_sha256, "changed_files": sorted(changed_files),
    }))


class AcceptanceGate:
    def __init__(self, db: StateDB, machine: JobFSM | None = None):
        self.db = db
        self.fsm = machine or JobFSM(db)

    def evaluate(
        self,
        job_id: str,
        attempt_id: str,
        *,
        lease_token: str | None,
        lease_generation: int | None,
        pre_manifest: dict[str, str],
        post_manifest: dict[str, str],
        qa_report: QAReport | dict | None,
        protected_before: dict | None = None,
        protected_after: dict | None = None,
        role_d_review: dict | None = None,
        preflight_passed: bool = True,
        contract: dict | None = None,
        record: bool = True,
    ) -> GateResult:
        gates: dict[str, dict] = {}
        reasons: list[str] = []
        hints: set[str] = set()

        def mark(name: str, ok: bool, why: str = "", hint: str | None = None) -> None:
            gates[name] = {"ok": ok, "reason": why or ("ok" if ok else "failed")}
            if not ok:
                reasons.append(f"{name}: {why}")
                if hint:
                    hints.add(hint)

        # ---------------- G0: approved contract integrity (DB is the source of truth)
        crow = self.db.get_contract_row(job_id)
        contract_obj: dict = {}
        contract_sha = ""
        if crow is None:
            mark("G0", False, "unknown job", "CONTRACT")
        else:
            contract_obj = json.loads(crow["contract_json"])
            contract_sha = crow["contract_sha256"]
            problems = []
            if crow["approval_state"] != "APPROVED":
                problems.append(f"approval_state={crow['approval_state']}")
            if contract_digest(contract_obj) != contract_sha:
                problems.append("stored contract_json no longer matches approved SHA-256 (tampered)")
            if contract is not None and contract_digest(contract) != contract_sha:
                problems.append("supplied contract differs from approved DB record")
            appr = self.db.query_one(
                "SELECT payload_json FROM audit_events WHERE job_id=? AND event_type='CONTRACT_APPROVED' "
                "ORDER BY event_seq DESC LIMIT 1", (job_id,))
            if appr is None or json.loads(appr["payload_json"]).get("sha256") != contract_sha:
                problems.append("approved SHA-256 in append-only audit log does not match contract record")
            mark("G0", not problems, "; ".join(problems), "CONTRACT")

        # ---------------- G1: lease generation + attempt binding + preflight
        srow = self.db.get_state_row(job_id)
        arow = self.db.get_attempt(attempt_id)
        problems = []
        ok, why = self.fsm.check_lease(job_id, lease_token, lease_generation)
        if not ok:
            problems.append(f"lease: {why}")
        if arow is None or arow["job_id"] != job_id:
            problems.append("attempt does not belong to job")
        elif srow is None or srow["active_attempt_id"] != attempt_id:
            problems.append("attempt is not the job's active attempt")
        if not preflight_passed:
            problems.append("workspace preflight did not pass")
        mark("G1", not problems, "; ".join(problems), "LEASE")

        # ---------------- G2: real on-disk diff within allowlist; protected roots untouched
        diff = diff_manifests(pre_manifest or {}, post_manifest or {})
        changed = changed_paths(diff)
        problems = []
        scope_ok = True
        sandbox_path = arow["sandbox_path"] if arow else None
        if arow is not None:
            if arow["pre_manifest_sha256"] != manifest_digest(pre_manifest or {}):
                problems.append("pre-manifest does not match attempt record")
            if arow["post_manifest_sha256"] != manifest_digest(post_manifest or {}):
                problems.append("post-manifest does not match attempt record")
        else:
            problems.append("no attempt record")
        try:
            policy = PathPolicy(contract_obj.get("allowed_write_paths") or [], contract_obj.get("forbidden_write_roots") or [])
            pol = policy.check_changed(changed, sandbox_path if sandbox_path and os.path.isdir(sandbox_path) else None)
            if not pol.ok:
                scope_ok = False
                problems.append("out-of-scope changes: " + ", ".join(sorted({f"{v['path']}[{v['code']}]" for v in pol.violations})[:10]))
        except PathViolation as exc:
            scope_ok = False
            problems.append(f"bad allowlist: {exc}")
        for p in changed:
            if (post_manifest or {}).get(p, "").startswith("LINK:"):
                scope_ok = False
                problems.append(f"{p} is a symlink/junction")
        prot_ok = True
        project = self.db.get_project(crow["project_id"]) if crow else None
        needs_prot = bool(project and json.loads(project["protected_paths_json"]))
        if protected_before is None or protected_after is None:
            if needs_prot:
                prot_ok = False
                problems.append("protected-root fingerprints missing; cannot prove 0 bytes changed")
        else:
            prot_ok, pp = verify_protected_untouched(protected_before, protected_after)
            problems.extend(pp)
        hint = "SCOPE" if not scope_ok or not prot_ok else None
        mark("G2", scope_ok and prot_ok and not problems, "; ".join(problems), hint or "SCOPE")

        # ---------------- G3: independent QA for THIS job + attempt (no stale/reused reports)
        report: QAReport | None = None
        if isinstance(qa_report, dict):
            try:
                report = QAReport.from_dict(qa_report)
            except Exception:
                report = None
        else:
            report = qa_report
        problems = []
        if report is None:
            problems.append("no independent QA report")
        else:
            if not report.verify_seal():
                problems.append("QA report seal invalid (modified after run)")
            if report.job_id != job_id:
                problems.append(f"QA report belongs to job {report.job_id}")
            if report.attempt_id != attempt_id:
                problems.append(f"QA report belongs to attempt {report.attempt_id} (stale/reused)")
            if sandbox_path and os.path.normcase(os.path.abspath(report.sandbox_dir)) != os.path.normcase(os.path.abspath(sandbox_path)):
                problems.append("QA ran in a different sandbox than this attempt")
            want = sorted(contract_check_ids(contract_obj))
            got = sorted(c.check_id for c in report.checks)
            if want != got:
                problems.append("QA executed checks differ from contract acceptance_checks")
        mark("G3", not problems, "; ".join(problems), "STALE_QA")

        # ---------------- G4: every acceptance check exit_code == 0
        nv = False
        if report is None:
            mark("G4", False, "no QA results", "CHECKS_FAILED")
        else:
            bad = []
            for c in report.checks:
                if c.status == "NOT_VERIFIED":
                    if c.required:
                        nv = True
                        bad.append(f"{c.command}: NOT_VERIFIED")
                    continue
                if c.exit_code != 0 or c.status != "PASSED":
                    bad.append(f"{c.command}: status={c.status} exit={c.exit_code}")
            hard_fail = bool(bad) and any("NOT_VERIFIED" not in b for b in bad)
            if report.security_violation:
                bad.append("security violation (secret leak / policy-blocked command)")
                hints.add("POLICY")
                hard_fail = True
            if bad:
                gates["G4"] = {"ok": False, "reason": "; ".join(bad)}
                reasons.append("G4: " + "; ".join(bad))
                hints.add("CHECKS_FAILED" if hard_fail else "NOT_VERIFIED")
            else:
                gates["G4"] = {"ok": True, "reason": "ok"}

        # ---------------- evidence manifest + G5
        evidence = ""
        if crow and arow and report is not None:
            evidence = evidence_sha256(contract_sha, attempt_id, manifest_digest(pre_manifest or {}),
                                       manifest_digest(post_manifest or {}), report.report_sha256, changed)
        if contract_obj.get("role_d_review_required"):
            ok5, why5 = verify_review(role_d_review, job_id=job_id, attempt_id=attempt_id, evidence_sha256=evidence,
                                      worker_type=arow["worker_type"] if arow else None)
            mark("G5", ok5, "; ".join(why5), "ROLE_D")
        else:
            gates["G5"] = {"ok": True, "reason": "not required by contract"}

        failed = [g for g, v in gates.items() if not v["ok"]]
        if not failed:
            verdict = ACCEPTED
        elif failed == ["G4"] and "NOT_VERIFIED" in hints and "CHECKS_FAILED" not in hints and "POLICY" not in hints:
            verdict = NOT_VERIFIED
        else:
            verdict = REJECTED
        result = GateResult(verdict, gates, reasons, hints, evidence, changed)
        if record and arow is not None and crow is not None:
            result.verification_id = self._record(job_id, attempt_id, result, report, gates, role_d_review,
                                                  scope_ok, prot_ok)
        return result

    def _record(self, job_id, attempt_id, result: GateResult, report, gates, review, scope_ok, prot_ok) -> str:
        vid = uuid.uuid4().hex
        commands = [
            {"check_id": c.check_id, "command": c.command, "exit_code": c.exit_code, "status": c.status,
             "duration_ms": c.duration_ms}
            for c in (report.checks if report else [])
        ]
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO qa_verifications(verification_id,attempt_id,job_id,commands_run_json,all_exit_zero,"
                "allowlist_diff_ok,protected_roots_untouched,role_d_review_json,gate_verdict,"
                "evidence_manifest_sha256,verified_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (vid, attempt_id, job_id, canonical_json(commands), int(gates["G4"]["ok"] and gates["G3"]["ok"]),
                 int(scope_ok), int(prot_ok), canonical_json(review) if review else None, result.verdict,
                 result.evidence_manifest_sha256 or None, self.db.now_iso()),
            )
            self.db.append_audit(
                "GATE_VERDICT", job_id=job_id, attempt_id=attempt_id,
                payload={"verdict": result.verdict, "gates": {k: v["ok"] for k, v in gates.items()},
                         "reasons": result.reasons[:10], "verification_id": vid,
                         "changed_files": result.changed_files[:500]},
            )
        return vid
