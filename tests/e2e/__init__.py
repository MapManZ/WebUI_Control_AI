"""Shared helpers for unit + e2e tests of the core engine (state, sandbox, QA, supervisor, handoff)."""
from __future__ import annotations

import datetime as _dt
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestra.supervisor import Supervisor  # noqa: E402
from src.workers.base import STATUS_COMPLETED, WorkerAdapter, WorkerResult  # noqa: E402

DEFAULT_CHECK = "python -c \"import sys; sys.exit(0 if open('out/result.txt').read().startswith('Mock') else 1)\""


class Clock:
    """Injectable UTC clock so lease expiry can be tested deterministically."""

    def __init__(self):
        self.t = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

    def __call__(self) -> _dt.datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += _dt.timedelta(seconds=seconds)


def make_contract(job_id: str, ordinal: int = 1, batch: str = "B1", project: str = "proj", **over) -> dict:
    c = {
        "schema_version": "1.0.0", "plan_id": "TEST", "phase_id": "T", "job_id": job_id, "batch_id": batch,
        "batch_ordinal": ordinal, "project_id": project, "workspace_root_ref": ".", "goal": f"goal of {job_id}",
        "in_scope": ["out/**"], "out_of_scope": ["everything else"], "allowed_read_paths": ["**"],
        "allowed_write_paths": ["out/result.txt"], "forbidden_write_roots": ["secrets/"],
        "allowed_commands": ["python"], "dependencies": [], "acceptance_checks": [DEFAULT_CHECK],
        "required_evidence": [], "max_rework_cycles": 3, "max_infra_retries": 2, "timeout_seconds": 60,
        "role_d_review_required": False,
    }
    c.update(over)
    return c


def rmtree_retry(path: str, attempts: int = 8) -> None:
    def onerr(func, p, exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    for _ in range(attempts):
        shutil.rmtree(path, onerror=onerr)
        if not os.path.exists(path):
            return
        time.sleep(0.2)


class Env:
    """Temp repo root + source tree + Supervisor. Always call cleanup()."""

    def __init__(self, now_fn=None, **sup_kw):
        self.tmp = tempfile.mkdtemp(prefix="orch_test_")
        self.src = os.path.join(self.tmp, "source")
        os.makedirs(self.src)
        Path(self.src, "keep.txt").write_text("keep\n", encoding="utf-8")
        kw = dict(sup_kw)
        if now_fn is not None:
            kw["now_fn"] = now_fn
        self.sup = Supervisor(self.tmp, **kw)

    @property
    def db(self):
        return self.sup.db

    def job(self, job_id: str, ordinal: int = 1, batch: str = "B1", approve: bool = True, protected=None,
            project: str = "proj", **over) -> dict:
        c = make_contract(job_id, ordinal, batch, project, **over)
        self.sup.submit_contract(c, canonical_root=self.src, protected_paths=protected or [])
        if approve:
            self.sup.approve_contract(job_id, "tester")
        return c

    def state(self, job_id: str) -> str:
        return self.sup.fsm.state(job_id)

    def cleanup(self) -> None:
        try:
            self.sup.close()
        finally:
            rmtree_retry(self.tmp)


class ScriptedWorker(WorkerAdapter):
    """Runs ``fn(sandbox_dir)`` then reports a (possibly dishonest) status."""

    def __init__(self, fn=None, status: str = STATUS_COMPLETED, stdout: str = "", exit_code: int = 0,
                 changed_files: list[str] | None = None):
        self.fn, self.status, self.stdout, self.exit_code = fn, status, stdout, exit_code
        self.changed_files = list(changed_files or [])

    def capabilities(self) -> dict:
        return {"worker_type": "scripted"}

    def cancel(self, attempt_id: str) -> bool:
        return True

    def start(self, job_contract, sandbox_dir, attempt_id, **kwargs) -> WorkerResult:
        if self.fn:
            self.fn(sandbox_dir)
        return WorkerResult(job_id=str(job_contract.get("job_id")), attempt_id=attempt_id, worker_type="scripted",
                            status=self.status, stdout=self.stdout, exit_code=self.exit_code,
                            changed_files=self.changed_files)


def write_files(sandbox: str, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = Path(sandbox, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def good_result(sandbox: str) -> None:
    write_files(sandbox, {"out/result.txt": "Mock worker completed.\n"})


def make_junction(link: str, target: str) -> bool:
    """Create an NTFS junction (no admin needed). Returns False if unsupported."""
    import subprocess

    if os.name != "nt":
        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except OSError:
            return False
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True, text=True)
    return r.returncode == 0
