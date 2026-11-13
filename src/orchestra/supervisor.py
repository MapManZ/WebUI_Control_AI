"""End-to-end job lifecycle coordinator (submit -> approve -> run attempt -> QA gate -> handoff).

Enforces G7 (two-job batch gate): max 2 jobs per batch, and a job may only start when every dependency and every
lower-ordinal job of its batch is HANDOFF_VERIFIED (or CLOSED).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.handoff import generator as handoff_gen
from src.handoff import verifier as handoff_ver
from src.qa.gate import AcceptanceGate, evidence_sha256
from src.qa.role_d_adapter import RoleDAdapter, build_review_request
from src.qa.runner import QARunner, contains_secret, redact
from src.sandbox.manifest_hasher import (
    build_manifest, changed_paths, diff_manifests, fingerprint_roots, manifest_digest, verify_protected_untouched,
)
from src.sandbox.path_guard import PathPolicy, PathViolation, scan_links, validate_relative_path
from src.sandbox.worktree_manager import SandboxError, WorktreeManager
from src.state import fsm
from src.state.db import StateDB, canonical_json, contract_digest, utc_now
from src.state.fsm import JobFSM, StaleLeaseError, FSMError
from src.state.reconciler import Reconciler

from .repair import (
    AMBIGUOUS, INFRA_TRANSIENT, POLICY_BLOCK, SCOPE_VIOLATION, RepairController, classify_failure,
)

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
MAX_JOBS_PER_BATCH = 2


class ContractRejected(Exception):
    pass


class BatchGateError(Exception):
    """G7 violation."""


class SimulatedCrash(Exception):
    """Raised by fault-injection hooks to emulate a hard process death mid-run (no cleanup)."""


@dataclass
class AttemptOutcome:
    job_id: str
    attempt_id: str | None
    final_state: str
    gate_verdict: str | None = None
    failure_class: str | None = None
    repair_action: str | None = None
    reasons: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def accepted(self) -> bool:
        return self.final_state == fsm.ACCEPTED


class Supervisor:
    def __init__(
        self,
        repo_root: str | os.PathLike,
        *,
        runtime_root: str | os.PathLike | None = None,
        db_path: str | os.PathLike | None = None,
        now_fn: Callable = utc_now,
        hmac_key: bytes | None = None,
        qa_runner: QARunner | None = None,
        role_d: RoleDAdapter | None = None,
        lease_ttl_seconds: float = 900.0,
        protected_fingerprint_content: bool = False,
    ):
        self.repo_root = Path(os.path.abspath(str(repo_root)))
        self.runtime_root = Path(os.path.abspath(str(runtime_root or self.repo_root / ".runtime")))
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.db = StateDB(db_path or self.runtime_root / "state" / "orchestra.db", now_fn=now_fn)
        self.fsm = JobFSM(self.db)
        self.reconciler = Reconciler(self.db, self.fsm)
        self.repair = RepairController(self.db, self.fsm)
        self.gate = AcceptanceGate(self.db, self.fsm)
        self.qa = qa_runner or QARunner()
        self.role_d = role_d
        self.worktrees = WorktreeManager(self.runtime_root)
        self.lease_ttl = lease_ttl_seconds
        self.protected_content = protected_fingerprint_content
        self.hmac_key = hmac_key or self._load_or_create_key()
        self.fault_hooks: dict[str, Callable[..., None]] = {}
        self._workers: dict[str, Any] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ infra
    def _load_or_create_key(self) -> bytes:
        kp = self.runtime_root / "keys" / "handoff.key"
        if kp.is_file():
            return kp.read_bytes()
        kp.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        kp.write_bytes(key)
        return key

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Supervisor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _hook(self, name: str, **ctx) -> None:
        fn = self.fault_hooks.get(name)
        if fn:
            fn(**ctx)

    # ------------------------------------------------------------------ submit / approve
    def validate_contract(self, c: dict) -> list[str]:
        errs: list[str] = []
        for f in ("job_id", "batch_id", "project_id", "goal"):
            if not str(c.get(f, "")).strip():
                errs.append(f"missing required field {f}")
        if c.get("job_id") and not _JOB_ID_RE.match(str(c["job_id"])):
            errs.append("job_id contains unsafe characters")
        if not isinstance(c.get("acceptance_checks"), list) or not c.get("acceptance_checks"):
            errs.append("acceptance_checks must be a non-empty list")
        awp = c.get("allowed_write_paths")
        if not isinstance(awp, list) or not awp:
            errs.append("allowed_write_paths must be a non-empty list")
        else:
            for p in awp:
                try:
                    validate_relative_path(str(p), allow_glob=True)
                except PathViolation as exc:
                    errs.append(f"allowed_write_paths entry {p!r} rejected: {exc.code}")
        try:
            if int(c.get("batch_ordinal", 1)) < 1:
                errs.append("batch_ordinal must be >= 1")
        except (TypeError, ValueError):
            errs.append("batch_ordinal must be an integer")
        for k in ("max_rework_cycles", "max_infra_retries"):
            if k in c and (not isinstance(c[k], int) or c[k] < 0):
                errs.append(f"{k} must be a non-negative integer")
        return errs

    def submit_contract(self, contract: Any, *, canonical_root: str | None = None,
                        protected_paths: list[str] | None = None, project_name: str | None = None,
                        serena_policy: str = "READ_ONLY_NO_9121") -> dict:
        c = contract.to_dict() if hasattr(contract, "to_dict") else dict(contract)
        errs = self.validate_contract(c)
        if errs:
            raise ContractRejected("; ".join(errs))
        claimed = c.get("contract_sha256")
        if claimed:
            accepted = {contract_digest(c)}
            try:
                from src.contracts.validator import compute_contract_sha256

                accepted.add(compute_contract_sha256(c))
            except Exception:
                pass
            if claimed not in accepted:
                raise ContractRejected(f"contract_sha256 mismatch for {c['job_id']}")
        batch = str(c["batch_id"])
        with self.db.transaction():
            n = self.db.query_one("SELECT COUNT(*) AS n FROM job_contracts WHERE batch_id=?", (batch,))["n"]
            if n >= MAX_JOBS_PER_BATCH:
                raise BatchGateError(f"G7: batch {batch} already holds {n} jobs (max {MAX_JOBS_PER_BATCH})")
            dup = self.db.query_one("SELECT job_id FROM job_contracts WHERE batch_id=? AND batch_ordinal=?",
                                    (batch, int(c.get("batch_ordinal", 1))))
            if dup:
                raise BatchGateError(f"G7: batch {batch} ordinal {c.get('batch_ordinal', 1)} already used by {dup['job_id']}")
            self.db.register_project(c["project_id"], project_name or c["project_id"],
                                     canonical_root or str(self.repo_root), protected_paths or [], serena_policy)
            sha = self.fsm.create_job(c)
        return {"job_id": c["job_id"], "contract_sha256": sha, "state": fsm.DRAFT}

    def approve_contract(self, job_id: str, approved_by: str) -> str:
        if not approved_by or not approved_by.strip():
            raise ContractRejected("approved_by is required")
        self.fsm.approve_contract(job_id, approved_by)
        self.fsm.transition(job_id, fsm.PLAN_APPROVED, actor=approved_by)
        self.fsm.transition(job_id, fsm.QUEUED, actor=approved_by)
        return self.fsm.state(job_id)

    # ------------------------------------------------------------------ G7
    def check_batch_gate(self, job_id: str) -> None:
        crow = self.db.get_contract_row(job_id)
        if crow is None:
            raise BatchGateError(f"unknown job {job_id}")
        n = self.db.query_one("SELECT COUNT(*) AS n FROM job_contracts WHERE batch_id=?", (crow["batch_id"],))["n"]
        if n > MAX_JOBS_PER_BATCH:
            raise BatchGateError(f"G7: batch {crow['batch_id']} has {n} jobs")
        needed = set(json.loads(crow["depends_on_json"]))
        for r in self.db.query("SELECT job_id FROM job_contracts WHERE batch_id=? AND batch_ordinal<?",
                               (crow["batch_id"], crow["batch_ordinal"])):
            needed.add(r["job_id"])
        for dep in sorted(needed):
            srow = self.db.get_state_row(dep)
            if srow is None or srow["current_state"] not in fsm.DONE_STATES:
                cur = srow["current_state"] if srow else "MISSING"
                raise BatchGateError(
                    f"G7: {job_id} cannot start; predecessor {dep} is {cur} (needs HANDOFF_VERIFIED)")

    # ------------------------------------------------------------------ run
    def run_job_attempt(self, job_id: str, worker: Any, *, source_root: str | os.PathLike | None = None,
                        include: list[str] | None = None, worker_mode: str | None = None,
                        owner: str | None = None) -> AttemptOutcome:
        """One full attempt: lease -> preflight -> worker -> independent QA -> gate -> (ACCEPTED | repair)."""
        self.check_batch_gate(job_id)
        crow = self.db.get_contract_row(job_id)
        contract = json.loads(crow["contract_json"])
        owner = owner or f"supervisor:{os.getpid()}"
        lease = self.fsm.acquire_lease(job_id, owner, self.lease_ttl)
        try:
            return self._run_locked(job_id, crow, contract, worker, source_root, include, worker_mode,
                                    lease.token, lease.generation)
        except StaleLeaseError as exc:
            self.db.append_audit("STALE_WRITER_REJECTED", job_id=job_id, payload={"detail": str(exc)})
            srow = self.db.get_state_row(job_id)
            return AttemptOutcome(job_id, srow["active_attempt_id"], srow["current_state"],
                                  error=f"stale lease: {exc}", reasons=[str(exc)])
        finally:
            self._workers.pop(job_id, None)

    def run_until_done(self, job_id: str, workers: Any, *, source_root=None, include=None,
                       worker_mode: str | None = None, max_attempts: int = 10) -> list[AttemptOutcome]:
        """Bounded repair loop: re-run after REWORK_REQUIRED until ACCEPTED / BLOCKED / FAILED / PAUSED."""
        outcomes: list[AttemptOutcome] = []
        for i in range(max_attempts):
            w = workers(i + 1) if callable(workers) and not hasattr(workers, "start") else (
                workers[min(i, len(workers) - 1)] if isinstance(workers, (list, tuple)) else workers)
            out = self.run_job_attempt(job_id, w, source_root=source_root, include=include, worker_mode=worker_mode)
            outcomes.append(out)
            if out.final_state != fsm.REWORK_REQUIRED:
                break
            self.repair.requeue_for_rework(job_id)
        return outcomes

    def _run_locked(self, job_id, crow, contract, worker, source_root, include, worker_mode, tok, gen) -> AttemptOutcome:
        attempt_id = self.fsm.create_attempt(job_id, worker_type=type(worker).__name__, lease_token=tok,
                                             lease_generation=gen)
        num = self.db.get_attempt(attempt_id)["attempt_num"]
        project = self.db.get_project(crow["project_id"])
        protected = json.loads(project["protected_paths_json"])
        evidence_dir = self.runtime_root / "evidence" / job_id / f"attempt-{num}"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        st: dict[str, Any] = {"job_id": job_id, "attempt_id": attempt_id, "tok": tok, "gen": gen, "contract": contract}

        # ---------------- preflight (G0/G1 inputs)
        problems: list[str] = []
        sandbox = None
        pre_manifest: dict[str, str] = {}
        policy = None
        try:
            if contract_digest(contract) != crow["contract_sha256"] or crow["approval_state"] != "APPROVED":
                problems.append("contract SHA-256 / approval mismatch")
            policy = PathPolicy(contract.get("allowed_write_paths") or [], contract.get("forbidden_write_roots") or [])
            sandbox = self.worktrees.create_attempt(job_id, num, source_root=source_root, include=include)
            links = scan_links(sandbox.path)
            if links:
                problems.append(f"links present in sandbox: {[x['path'] for x in links][:5]}")
            pre_manifest = build_manifest(sandbox.path)
        except (SandboxError, PathViolation, OSError) as exc:
            problems.append(f"{type(exc).__name__}: {exc}")
        if problems or sandbox is None:
            self.fsm.finish_attempt(attempt_id, worker_status="PREFLIGHT_FAILED", failure_class=INFRA_TRANSIENT,
                                    sandbox_path=sandbox.path if sandbox else None)
            self.fsm.transition(job_id, fsm.PREFLIGHT_FAILED, lease_token=tok, lease_generation=gen,
                                attempt_id=attempt_id, payload={"problems": problems}, actor="preflight")
            return AttemptOutcome(job_id, attempt_id, fsm.PREFLIGHT_FAILED, reasons=problems)
        prot_before = fingerprint_roots(protected, content=self.protected_content)
        self.fsm.update_attempt(attempt_id, sandbox_path=sandbox.path,
                                pre_manifest_sha256=manifest_digest(pre_manifest))
        st.update(sandbox=sandbox.path, pre_manifest=pre_manifest)
        self.fsm.transition(job_id, fsm.PREFLIGHT_PASSED, lease_token=tok, lease_generation=gen,
                            attempt_id=attempt_id, payload={"sandbox": sandbox.path, "mode": sandbox.mode})
        self._hook("after_preflight", state=st)
        self.fsm.transition(job_id, fsm.RUNNING, lease_token=tok, lease_generation=gen, attempt_id=attempt_id)

        # ---------------- worker
        self._workers[job_id] = worker
        wr = None
        worker_exc: BaseException | None = None
        try:
            kwargs = {"mode": worker_mode} if worker_mode else {}
            wr = worker.start(dict(contract), sandbox.path, attempt_id, **kwargs)
        except SimulatedCrash:
            raise
        except Exception as exc:  # worker blew up -> infra class, never accepted
            worker_exc = exc
        worker_status = getattr(wr, "status", "EXCEPTION" if worker_exc else "FAILED")
        w_out = (getattr(wr, "stdout", "") or "") + "\n" + (getattr(wr, "stderr", "") or "")
        leaked = contains_secret(w_out)
        leak_blocks = leaked and contract.get("disallow_secret_leak", True)
        (evidence_dir / "worker_result.json").write_text(json.dumps({
            "status": worker_status, "exit_code": getattr(wr, "exit_code", None),
            "claimed_changed_files": list(getattr(wr, "changed_files", []) or []),
            "stdout": redact(getattr(wr, "stdout", "") or "")[-20000:],
            "stderr": redact(getattr(wr, "stderr", "") or "")[-20000:],
            "exception": f"{type(worker_exc).__name__}: {worker_exc}" if worker_exc else None,
            "secret_leak_detected": leaked,
        }, indent=2), encoding="utf-8")
        st.update(wr=wr, worker_status=worker_status)
        self.fsm.update_attempt(attempt_id, worker_status=worker_status)
        self._hook("after_worker", state=st)

        # ---------------- independent scope check on the REAL on-disk diff (worker claims ignored)
        mid_manifest = build_manifest(sandbox.path)
        mid_changed = changed_paths(diff_manifests(pre_manifest, mid_manifest))
        claimed_files = list(getattr(wr, "changed_files", []) or [])
        all_to_check = sorted(set(mid_changed) | set(claimed_files))
        pol = policy.check_changed(all_to_check, sandbox.path)
        early_stop = (not pol.ok) or leak_blocks or worker_status != "COMPLETED"
        if early_stop:
            return self._finish_early(job_id, attempt_id, contract, st, pre_manifest, mid_manifest, protected, prot_before,
                                      pol, leak_blocks, worker_status, worker_exc, source_root, evidence_dir)

        # ---------------- QA (independent of the worker)
        self.fsm.transition(job_id, fsm.QA_RUNNING, lease_token=tok, lease_generation=gen, attempt_id=attempt_id)
        self._hook("before_qa", state=st)
        report = self.qa.run_checks(contract, sandbox.path, job_id, attempt_id,
                                    disallow_secret_leak=contract.get("disallow_secret_leak", True))
        st["report"] = report
        self._hook("after_qa", state=st)
        post_manifest = build_manifest(sandbox.path)
        prot_after = fingerprint_roots(protected, content=self.protected_content)
        patch_path = self._write_evidence(evidence_dir, pre_manifest, post_manifest, source_root, sandbox.path)
        self.fsm.update_attempt(attempt_id, post_manifest_sha256=manifest_digest(post_manifest),
                                diff_patch_path=str(patch_path))
        st.update(post_manifest=post_manifest)
        self.fsm.transition(job_id, fsm.REVIEWING, lease_token=tok, lease_generation=gen, attempt_id=attempt_id)
        changed = changed_paths(diff_manifests(pre_manifest, post_manifest))
        review = None
        if contract.get("role_d_review_required") and self.role_d is not None and st["report"] is not None:
            ev = evidence_sha256(crow["contract_sha256"], attempt_id, manifest_digest(pre_manifest),
                                 manifest_digest(post_manifest), st["report"].report_sha256, changed)
            review = self.role_d.review(build_review_request(job_id, attempt_id, ev, crow["contract_sha256"], changed))
        st["review"] = review
        self._hook("before_gate", state=st)
        gate = self.gate.evaluate(
            job_id, attempt_id, lease_token=tok, lease_generation=gen, pre_manifest=pre_manifest,
            post_manifest=post_manifest, qa_report=st["report"], protected_before=prot_before,
            protected_after=prot_after, role_d_review=st["review"], preflight_passed=True,
        )
        (evidence_dir / "gate.json").write_text(json.dumps(gate.to_dict(), indent=2), encoding="utf-8")
        (evidence_dir / "qa_report.json").write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        if gate.accepted:
            self.fsm.finish_attempt(attempt_id, worker_status=worker_status)
            self.fsm.transition(job_id, fsm.ACCEPTED, lease_token=tok, lease_generation=gen, attempt_id=attempt_id,
                                payload={"verdict": gate.verdict, "evidence": gate.evidence_manifest_sha256})
            self._seal(job_id, num)
            return AttemptOutcome(job_id, attempt_id, fsm.ACCEPTED, gate.verdict, changed_files=gate.changed_files)
        fc = classify_failure(worker_status=worker_status, gate=gate, qa_report=st["report"])
        dec = self.repair.apply(job_id, attempt_id, fc, lease_token=tok, lease_generation=gen,
                                detail={"reasons": gate.reasons[:10], "verdict": gate.verdict})
        self.fsm.update_attempt(attempt_id, ended_at=self.db.now_iso())
        self._seal(job_id, num)
        return AttemptOutcome(job_id, attempt_id, dec.to_state, gate.verdict, fc, dec.action, gate.reasons,
                              gate.changed_files)

    def _finish_early(self, job_id, attempt_id, contract, st, pre_manifest, post_manifest, protected, prot_before, pol,
                      leak_blocks, worker_status, worker_exc, source_root, evidence_dir) -> AttemptOutcome:
        tok, gen = st["tok"], st["gen"]
        num = self.db.get_attempt(attempt_id)["attempt_num"]
        patch_path = self._write_evidence(evidence_dir, pre_manifest, post_manifest, source_root, st["sandbox"])
        self.fsm.update_attempt(attempt_id, post_manifest_sha256=manifest_digest(post_manifest),
                                diff_patch_path=str(patch_path))
        scope_bad = not pol.ok
        gate = None
        reasons: list[str] = []
        if scope_bad or leak_blocks:
            prot_after = fingerprint_roots(protected, content=self.protected_content)
            gate = self.gate.evaluate(
                job_id, attempt_id, lease_token=tok, lease_generation=gen, pre_manifest=pre_manifest,
                post_manifest=post_manifest, qa_report=None, protected_before=prot_before,
                protected_after=prot_after, preflight_passed=True)
            reasons = list(gate.reasons)
            if scope_bad:
                reasons.append("scope: " + ", ".join(sorted({f"{v['path']}[{v['code']}]" for v in pol.violations})[:10]))
            if leak_blocks:
                reasons.append("worker output leaked a raw secret (redacted in evidence)")
        fc = classify_failure(worker_status=worker_status, gate=gate, exception=worker_exc,
                              scope_violation=scope_bad, policy_block=bool(leak_blocks))
        dec = self.repair.apply(job_id, attempt_id, fc, lease_token=tok, lease_generation=gen,
                                detail={"reasons": reasons[:10], "worker_status": worker_status})
        self.fsm.update_attempt(attempt_id, ended_at=self.db.now_iso())
        self._seal(job_id, num)
        return AttemptOutcome(job_id, attempt_id, dec.to_state, gate.verdict if gate else None, fc, dec.action,
                              reasons or [f"worker_status={worker_status}"],
                              changed_paths(diff_manifests(pre_manifest, post_manifest)),
                              error=str(worker_exc) if worker_exc else None)

    # ------------------------------------------------------------------ evidence helpers
    def _write_evidence(self, evidence_dir: Path, pre: dict, post: dict, source_root, sandbox: str) -> Path:
        diff = diff_manifests(pre, post)
        (evidence_dir / "diff.json").write_text(json.dumps(diff, indent=2), encoding="utf-8")
        patch = evidence_dir / "diff.patch"
        chunks: list[str] = []
        for rel in diff["added"] + diff["modified"]:
            new = Path(sandbox) / rel
            old = Path(source_root) / rel if source_root else None
            try:
                if not new.is_file() or new.stat().st_size > 1_000_000:
                    continue
                new_lines = new.read_text(encoding="utf-8").splitlines(keepends=True)
                old_lines = (old.read_text(encoding="utf-8").splitlines(keepends=True)
                             if old and old.is_file() and rel in diff["modified"] else [])
            except (UnicodeDecodeError, OSError):
                continue
            chunks.extend(difflib.unified_diff(old_lines, new_lines, f"a/{rel}", f"b/{rel}"))
        for rel in diff["deleted"]:
            chunks.append(f"--- a/{rel}\n+++ /dev/null\n")
        patch.write_text(redact("".join(chunks)), encoding="utf-8")
        return patch

    def _seal(self, job_id: str, num: int) -> None:
        try:
            self.worktrees.seal_attempt(job_id, num)
        except OSError:
            pass

    # ------------------------------------------------------------------ operations
    def reconcile_crashed_jobs(self, *, assume_all_stale: bool = False):
        return self.reconciler.reconcile(assume_all_stale=assume_all_stale)

    def cancel_job(self, job_id: str, actor: str = "operator") -> str:
        w = self._workers.get(job_id)
        srow = self.db.get_state_row(job_id)
        if w is not None and srow["active_attempt_id"]:
            try:
                w.cancel(srow["active_attempt_id"])
            except Exception:
                pass
        self.fsm.transition(job_id, fsm.CANCELLED, fence=True, actor=actor)
        return self.fsm.state(job_id)

    def finalize_handoff(self, job_id: str, next_job_id: str, *, context_density: str = "Unknown") -> dict:
        """ACCEPTED -> generate handoff + NEXT_CONTEXT -> machine-verify against DB -> HANDOFF_VERIFIED."""
        out_dir = self.runtime_root / "handoffs" / job_id
        info = handoff_gen.generate_handoff(self.db, job_id, next_job_id, out_dir, context_density=context_density,
                                            hmac_key=self.hmac_key)
        res = handoff_ver.verify_handoff(info["handoff_path"], db=self.db, hmac_key=self.hmac_key,
                                         require_signature=True)
        nres = handoff_ver.verify_next_context(info["next_context_path"], info["handoff_path"])
        errors = res.errors + nres.errors
        if errors:
            self.db.append_audit("HANDOFF_REJECTED", job_id=job_id, payload={"errors": errors})
            raise handoff_gen.HandoffError("; ".join(errors))
        if self.fsm.state(job_id) == fsm.ACCEPTED:
            self.fsm.transition(job_id, fsm.HANDOFF_VERIFIED, actor="handoff-verifier",
                                idempotency_key=f"handoff-verified:{job_id}:{info['receipt_sha256']}",
                                payload={"receipt_sha256": info["receipt_sha256"],
                                         "handoff_file_sha256": info["handoff_file_sha256"]})
        info["state"] = self.fsm.state(job_id)
        return info

    def close_job(self, job_id: str) -> str:
        self.fsm.transition(job_id, fsm.CLOSED, actor="supervisor")
        return self.fsm.state(job_id)

    def reconstruct_from_handoff(self, handoff_path: str | os.PathLike, *, next_context_path: str | None = None) -> dict:
        """Rebuild/confirm job state from a machine-verified handoff (e.g. after DB loss)."""
        existing = None
        res = handoff_ver.verify_handoff(handoff_path, db=None, hmac_key=self.hmac_key, require_signature=True)
        if not res.ok:
            self.db.append_audit("HANDOFF_IMPORT_REJECTED", payload={"errors": res.errors, "path": str(handoff_path)})
            raise handoff_gen.HandoffError("; ".join(res.errors))
        if next_context_path:
            nres = handoff_ver.verify_next_context(next_context_path, handoff_path)
            if not nres.ok:
                raise handoff_gen.HandoffError("; ".join(nres.errors))
        r = res.receipt
        job_id = r["job_id"]
        existing = self.db.get_state_row(job_id)
        if existing is not None:
            dres = handoff_ver.verify_handoff(handoff_path, db=self.db, hmac_key=self.hmac_key, require_signature=True)
            if not dres.ok:
                raise handoff_gen.HandoffError("; ".join(dres.errors))
            if existing["current_state"] == fsm.ACCEPTED:
                self.fsm.transition(job_id, fsm.HANDOFF_VERIFIED, actor="handoff-import",
                                    idempotency_key=f"handoff-verified:{job_id}:{res.receipt_sha256}")
            return {"job_id": job_id, "state": self.fsm.state(job_id), "reconstructed": False}
        pj = r["project"]
        self.db.register_project(pj["project_id"], pj["name"], pj["canonical_root"],
                                 r["protected_boundary"].get("roots", []), pj["serena_policy"])
        self.fsm.create_job(r["contract"])
        self.fsm.approve_contract(job_id, "reconstructed-from-handoff")
        with self.db.transaction() as c:
            c.execute("UPDATE job_states SET current_state=?, updated_at=? WHERE job_id=?",
                      (fsm.HANDOFF_VERIFIED, self.db.now_iso(), job_id))
            self.db.append_audit("RECONSTRUCTED_FROM_HANDOFF", job_id=job_id, from_state=fsm.DRAFT,
                                 to_state=fsm.HANDOFF_VERIFIED,
                                 payload={"receipt_sha256": res.receipt_sha256,
                                          "handoff_file_sha256": res.handoff_file_sha256,
                                          "evidence_manifest_sha256": r["evidence_manifest_sha256"]},
                                 idempotency_key=f"reconstruct:{job_id}:{res.receipt_sha256}")
        return {"job_id": job_id, "state": fsm.HANDOFF_VERIFIED, "reconstructed": True}

    def status(self) -> dict:
        rows = self.db.query(
            "SELECT k.job_id, k.batch_id, k.batch_ordinal, k.project_id, k.approval_state, s.current_state,"
            " s.rework_count, s.infra_retry_count, s.active_attempt_id, s.lease_generation FROM job_contracts k"
            " JOIN job_states s ON s.job_id=k.job_id ORDER BY k.batch_id, k.batch_ordinal")
        return {"db": self.db.path, "journal_mode": self.db.pragma("journal_mode"),
                "jobs": [dict(r) for r in rows], "audit_events": self.db.query_one(
                    "SELECT COUNT(*) AS n FROM audit_events")["n"]}
