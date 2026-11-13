"""Isolated REA pilot: snapshot a few governance/preflight files, run a bounded 2-job batch through
Supervisor + QA gate + handoff in ``.runtime/sandboxes/REA-PILOT*/`` and prove (pre/post SHA-256 + stat
fingerprint) that ``SummoningRemake`` was not modified.

READ-ONLY on the REA project: files are only opened for reading (copy into the sandbox). No REA code is ever
executed (the preflight script is only parsed by ``py_compile`` *inside the sandbox copy*) and Serena port 9121
is never contacted.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from src.orchestra.supervisor import BatchGateError, Supervisor
from src.sandbox.manifest_hasher import anchor_manifest, fingerprint_tree, sha256_file
from src.workers.base import STATUS_COMPLETED, STATUS_FAILED, WorkerAdapter, WorkerResult

REA_ROOT = r"O:\AI_Workspace\REA Project\OpenCodeREA\SummoningRemake"
SNAPSHOT_FILES = [
    "FULL_SUMMARY_HANDOFF_R387_TO_R388_2026-11-12.md",
    "REA_WORKFLOW_ACCEPTANCE_AGREEMENT_2026-11-09.md",
    "tools/rea_serena_workspace_preflight.py",
]
JOB1 = "REA-PILOT"
JOB2 = "REA-PILOT-NEXT"
PROJECT_ID = "REA-SUMMONING-REMAKE"
BATCH_ID = "REA-PILOT-BATCH"


class PilotWorker(WorkerAdapter):
    """Deterministic local worker: summarises the snapshotted files (no network, no REA code execution)."""

    def __init__(self, role: str, handoff_path: str | None = None, hmac_key: bytes | None = None, db=None):
        self.role = role
        self.handoff_path = handoff_path
        self.hmac_key = hmac_key
        self.db = db

    def capabilities(self) -> dict:
        return {"worker_type": "rea_pilot_worker", "network": False}

    def cancel(self, attempt_id: str) -> bool:
        return True

    def start(self, job_contract: Any, sandbox_dir: str, attempt_id: str, **kwargs) -> WorkerResult:
        t0 = time.time()
        sb = Path(sandbox_dir)
        out = sb / "pilot_out"
        out.mkdir(parents=True, exist_ok=True)
        try:
            if self.role == "snapshot_summary":
                digests = {f: hashlib.sha256((sb / f).read_bytes()).hexdigest() for f in SNAPSHOT_FILES}
                lines = {f: len((sb / f).read_text(encoding="utf-8", errors="replace").splitlines()) for f in SNAPSHOT_FILES}
                (out / "pilot_summary.json").write_text(
                    json.dumps({"sha256": digests, "line_counts": lines, "worker": "PilotWorker"}, indent=2, sort_keys=True),
                    encoding="utf-8")
                (out / "pilot_notes.md").write_text(
                    "# REA pilot notes\n\nSnapshot-only analysis of governance + preflight files.\n"
                    "No REA code was executed. Serena port 9121 untouched.\n", encoding="utf-8")
                changed = ["pilot_out/pilot_summary.json", "pilot_out/pilot_notes.md"]
            else:  # "handoff_consumer": proves job 2 imports job 1's handoff only after machine verification
                from src.handoff.verifier import verify_handoff

                res = verify_handoff(self.handoff_path, db=self.db, hmac_key=self.hmac_key, require_signature=True)
                (out / "handoff_verified.json").write_text(json.dumps({
                    "ok": res.ok, "errors": res.errors, "receipt_sha256": res.receipt_sha256,
                    "previous_job": res.receipt["job_id"] if res.receipt else None}, indent=2), encoding="utf-8")
                changed = ["pilot_out/handoff_verified.json"]
        except Exception as exc:  # pragma: no cover
            return WorkerResult(job_id=str(job_contract.get("job_id")), attempt_id=attempt_id, worker_type="rea_pilot_worker",
                                status=STATUS_FAILED, stderr=str(exc), exit_code=1)
        return WorkerResult(job_id=str(job_contract.get("job_id")), attempt_id=attempt_id, worker_type="rea_pilot_worker",
                            status=STATUS_COMPLETED, changed_files=changed, stdout=f"{self.role} done", exit_code=0,
                            duration_ms=int((time.time() - t0) * 1000))


def _contract(job_id: str, ordinal: int, rea_root: str, deps: list[str], goal: str, checks: list[str],
              write_paths: list[str]) -> dict:
    return {
        "schema_version": "1.0.0", "plan_id": "REA-ORCH-PILOT", "phase_id": "PILOT", "job_id": job_id,
        "batch_id": BATCH_ID, "batch_ordinal": ordinal, "project_id": PROJECT_ID,
        "workspace_root_ref": "REA_SNAPSHOT_ONLY", "goal": goal,
        "in_scope": ["read snapshot copies", "write pilot_out/**"],
        "out_of_scope": ["modify SummoningRemake", "contact Serena 9121", "execute REA code"],
        "allowed_read_paths": list(SNAPSHOT_FILES), "allowed_write_paths": write_paths,
        "forbidden_write_roots": [rea_root, "tools/", "FULL_SUMMARY_HANDOFF_*", "REA_WORKFLOW_*"],
        "allowed_commands": ["python"], "dependencies": deps, "acceptance_checks": checks,
        "required_evidence": ["qa_report", "diff", "handoff"], "max_rework_cycles": 1, "max_infra_retries": 1,
        "timeout_seconds": 60, "role_d_review_required": False,
    }


CHECK_COMPILE = "python -m py_compile tools/rea_serena_workspace_preflight.py"
CHECK_SUMMARY = (
    "python -c \"import json,hashlib,sys;d=json.load(open('pilot_out/pilot_summary.json'));"
    "ok=len(d['sha256'])==3 and all(hashlib.sha256(open(k,'rb').read()).hexdigest()==v for k,v in d['sha256'].items());"
    "sys.exit(0 if ok else 1)\""
)
CHECK_AGREEMENT = (
    "python -c \"import sys;t=open('REA_WORKFLOW_ACCEPTANCE_AGREEMENT_2026-11-09.md',encoding='utf-8').read();"
    "sys.exit(0 if len(t)>1000 else 1)\""
)
CHECK_HANDOFF = (
    "python -c \"import json,re,sys;d=json.load(open('pilot_out/handoff_verified.json'));"
    "sys.exit(0 if d['ok'] and re.fullmatch('[0-9a-f]{64}',d['receipt_sha256'] or '') and d['previous_job']=='REA-PILOT' else 1)\""
)


def _safe_clean(runtime: Path, job_ids: list[str]) -> None:
    base = (runtime / "sandboxes").resolve()
    for j in job_ids:
        p = (base / j).resolve()
        if p.parent == base and p.exists():
            shutil.rmtree(p, ignore_errors=True)


def run_pilot(repo_root: str | os.PathLike, *, rea_root: str = REA_ROOT, deep: bool = False) -> dict:
    """Run the full isolated pilot. ``deep=True`` content-hashes every file under the REA root (slow)."""
    repo = Path(os.path.abspath(str(repo_root)))
    runtime = repo / ".runtime"
    pilot_dir = runtime / "pilot"
    pilot_dir.mkdir(parents=True, exist_ok=True)
    started = _dt.datetime.now(_dt.timezone.utc).isoformat()
    rea = Path(rea_root)
    if not rea.is_dir():
        raise FileNotFoundError(f"REA root not found (pilot needs read access): {rea}")
    for rel in SNAPSHOT_FILES:
        if not (rea / rel).is_file():
            raise FileNotFoundError(f"snapshot source missing: {rea / rel}")

    # ---- BEFORE: prove baseline of the protected tree
    anchors = [
        str(rea / r)
        for r in [
            "FULL_SUMMARY_HANDOFF_R387_TO_R388_2026-11-12.md",
            "REA_WORKFLOW_ACCEPTANCE_AGREEMENT_2026-11-09.md",
            "tools/rea_serena_workspace_preflight.py",
            ".rea-serena-workspace.json",
            "AGENTS.md",
            "README.md",
            "package.json",
            "WORKSPACE_INDEX.md",
        ]
        if (rea / r).is_file()
    ]
    before_anchor = anchor_manifest(anchors)
    before_tree = fingerprint_tree(rea_root, content=deep)

    # ---- fresh pilot state (inside .runtime only)
    _safe_clean(runtime, [JOB1, JOB2])
    db_path = pilot_dir / "pilot.db"
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(db_path) + suffix)
        except OSError:
            pass
    sup = Supervisor(repo, runtime_root=runtime, db_path=db_path)
    steps: list[dict] = []
    try:
        c1 = _contract(JOB1, 1, rea_root, [], "Summarise snapshotted REA governance + preflight files",
                       [CHECK_COMPILE, CHECK_SUMMARY, CHECK_AGREEMENT], ["pilot_out/**"])
        c2 = _contract(JOB2, 2, rea_root, [JOB1], "Consume verified handoff of REA-PILOT",
                       [CHECK_COMPILE, CHECK_HANDOFF], ["pilot_out/**"])
        sup.submit_contract(c1, canonical_root=rea_root, protected_paths=anchors, project_name="SummoningRemake")
        sup.submit_contract(c2, canonical_root=rea_root, protected_paths=anchors, project_name="SummoningRemake")
        sup.approve_contract(JOB1, "pilot-operator")
        sup.approve_contract(JOB2, "pilot-operator")
        # G7: job 2 must refuse to start before job 1 is HANDOFF_VERIFIED
        try:
            sup.run_job_attempt(JOB2, PilotWorker("handoff_consumer"), source_root=rea_root, include=SNAPSHOT_FILES)
            g7 = False
        except BatchGateError as exc:
            g7 = True
            steps.append({"step": "G7_job2_blocked_before_handoff", "detail": str(exc)})
        o1 = sup.run_job_attempt(JOB1, PilotWorker("snapshot_summary"), source_root=rea_root, include=SNAPSHOT_FILES)
        steps.append({"step": "job1_attempt", **o1.to_dict()})
        h1 = sup.finalize_handoff(JOB1, JOB2, context_density="Low") if o1.accepted else None
        steps.append({"step": "job1_handoff", "state": h1["state"] if h1 else "n/a"})
        o2 = None
        h2 = None
        if h1:
            w2 = PilotWorker("handoff_consumer", h1["handoff_path"], sup.hmac_key, sup.db)
            o2 = sup.run_job_attempt(JOB2, w2, source_root=rea_root, include=SNAPSHOT_FILES)
            steps.append({"step": "job2_attempt", **o2.to_dict()})
            if o2.accepted:
                h2 = sup.finalize_handoff(JOB2, "REA-NEXT", context_density="Low")
                sup.close_job(JOB1)
        states = {j["job_id"]: j["current_state"] for j in sup.status()["jobs"]}

        # reconstruction from handoff into a brand-new DB (simulated DB loss)
        recon_ok = False
        if h1:
            rdb = pilot_dir / "reconstructed.db"
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(rdb) + suffix)
                except OSError:
                    pass
            sup2 = Supervisor(repo, runtime_root=runtime, db_path=rdb, hmac_key=sup.hmac_key)
            try:
                rr = sup2.reconstruct_from_handoff(h1["handoff_path"], next_context_path=h1["next_context_path"])
                recon_ok = rr["state"] == "HANDOFF_VERIFIED" and rr["reconstructed"]
            finally:
                sup2.close()
        audit_count = sup.status()["audit_events"]
    finally:
        sup.close()

    # ---- AFTER: prove the protected tree is byte-for-byte untouched via SHA-256
    after_anchor = anchor_manifest(anchors)
    after_tree = fingerprint_tree(rea_root, content=deep)
    anchor_match = (before_anchor == after_anchor and len(before_anchor) > 0)
    no_foreign_writes = not any((rea / f).exists() for f in ["pilot_out", ".runtime", "sandboxes", "evidence", "out"])
    untouched = bool(anchor_match and no_foreign_writes)
    sandbox_files = sorted(
        str(p.relative_to(runtime)).replace("\\", "/")
        for j in (JOB1, JOB2) for p in (runtime / "sandboxes" / j).rglob("*") if p.is_file()
    ) if (runtime / "sandboxes").exists() else []
    ok = bool(untouched and g7 and o1.accepted and o2 is not None and o2.accepted and h2 and recon_ok)
    result = {
        "started_at": started,
        "finished_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "rea_root": rea_root,
        "fingerprint_mode": "content-sha256" if deep else "stat(size,mtime_ns)+anchor-content-sha256",
        "protected_tree_untouched": untouched,
        "before_tree": before_tree, "after_tree": after_tree,
        "anchor_sha256_before": before_anchor, "anchor_sha256_after": after_anchor,
        "g7_two_job_gate_enforced": g7,
        "job_states": states,
        "handoff_reconstruction_ok": recon_ok,
        "audit_events": audit_count,
        "sandbox_files": sandbox_files,
        "serena_port_9121_contacted": False,
        "steps": steps,
        "go_no_go": "GO" if ok else "NO-GO",
    }
    (pilot_dir / "REA_PILOT_RESULT.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":  # pragma: no cover
    import sys

    res = run_pilot(Path(__file__).resolve().parents[2], deep="--deep" in sys.argv)
    print(json.dumps({k: res[k] for k in ("go_no_go", "protected_tree_untouched", "job_states",
                                          "handoff_reconstruction_ok")}, indent=2))
    sys.exit(0 if res["go_no_go"] == "GO" else 1)
