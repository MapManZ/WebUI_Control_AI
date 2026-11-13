"""Disposable immutable attempt sandboxes under ``.runtime/sandboxes/<job_id>/attempt-<N>/``."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .manifest_hasher import build_manifest, manifest_digest
from .path_guard import PathViolation, glob_to_regex, is_reparse_point, is_within, validate_relative_path

_JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024


class SandboxError(Exception):
    pass


@dataclass
class AttemptSandbox:
    job_id: str
    attempt_num: int
    path: str
    mode: str
    files_copied: int


class WorktreeManager:
    def __init__(self, runtime_root: str | os.PathLike):
        self.runtime_root = Path(os.path.abspath(str(runtime_root)))
        self.base = self.runtime_root / "sandboxes"

    def _job_dir(self, job_id: str) -> Path:
        if not _JOB_RE.match(job_id or "") or ".." in job_id:
            raise SandboxError(f"unsafe job id {job_id!r}")
        d = self.base / job_id
        if not is_within(d, self.base):
            raise SandboxError("job dir escapes sandbox base")
        return d

    def attempt_path(self, job_id: str, attempt_num: int) -> Path:
        return self._job_dir(job_id) / f"attempt-{int(attempt_num)}"

    def list_attempts(self, job_id: str) -> list[int]:
        d = self._job_dir(job_id)
        if not d.is_dir():
            return []
        nums = []
        for p in d.iterdir():
            m = re.fullmatch(r"attempt-(\d+)", p.name)
            if m and p.is_dir():
                nums.append(int(m.group(1)))
        return sorted(nums)

    def next_attempt_num(self, job_id: str) -> int:
        a = self.list_attempts(job_id)
        return (a[-1] + 1) if a else 1

    # --------------------------------------------------------------- creation
    def create_attempt(
        self,
        job_id: str,
        attempt_num: int | None = None,
        *,
        source_root: str | os.PathLike | None = None,
        include: list[str] | None = None,
        mode: str = "snapshot",
        max_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> AttemptSandbox:
        """Create a fresh sandbox. Existing attempt dirs are NEVER overwritten (immutable history)."""
        num = attempt_num or self.next_attempt_num(job_id)
        target = self.attempt_path(job_id, num)
        if target.exists():
            raise SandboxError(f"{target} already exists; attempts are immutable")
        later = [n for n in self.list_attempts(job_id) if n > num]
        if later:
            raise SandboxError(f"attempt-{num} would be created behind attempt-{later[-1]}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_root is None:
            target.mkdir()
            return AttemptSandbox(job_id, num, str(target), "empty", 0)
        src = Path(os.path.abspath(str(source_root)))
        if not src.is_dir():
            raise SandboxError(f"source_root missing: {src}")
        if mode == "git" and (src / ".git").exists() and not include:
            res = subprocess.run(
                ["git", "-C", str(src), "worktree", "add", "--detach", str(target)],
                capture_output=True, text=True, timeout=120,
            )
            if res.returncode == 0:
                return AttemptSandbox(job_id, num, str(target), "git-worktree", len(build_manifest(target)))
            shutil.rmtree(target, ignore_errors=True)
        return self._snapshot(job_id, num, src, target, include, max_bytes)

    def _snapshot(self, job_id, num, src: Path, target: Path, include, max_bytes) -> AttemptSandbox:
        patterns = list(include) if include else ["**"]
        regexes = []
        for pat in patterns:
            validate_relative_path(pat, allow_glob=True)
            regexes.append(glob_to_regex(pat))
        target.mkdir()
        total = 0
        count = 0
        try:
            for dp, dns, fns in os.walk(src, followlinks=False):
                dns[:] = [d for d in dns if d not in (".git", "__pycache__", ".runtime", "node_modules")
                          and not is_reparse_point(os.path.join(dp, d))]
                for f in fns:
                    full = os.path.join(dp, f)
                    if is_reparse_point(full):
                        continue  # never copy links into a sandbox
                    rel = os.path.relpath(full, src).replace("\\", "/")
                    if not any(r.match(rel) for r in regexes):
                        continue
                    size = os.path.getsize(full)
                    total += size
                    if total > max_bytes:
                        raise SandboxError("snapshot exceeds size limit")
                    dest = target / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(full, dest)
                    count += 1
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return AttemptSandbox(job_id, num, str(target), "snapshot", count)

    # --------------------------------------------------------------- sealing
    def _seal_file(self, job_id: str, num: int) -> Path:
        return self._job_dir(job_id) / f"attempt-{num}.seal.json"

    def seal_attempt(self, job_id: str, num: int) -> str:
        """Record the manifest digest of a finished attempt so later tampering is detectable."""
        digest = manifest_digest(build_manifest(self.attempt_path(job_id, num)))
        self._seal_file(job_id, num).write_text(json.dumps({"job_id": job_id, "attempt": num, "digest": digest}),
                                                encoding="utf-8")
        return digest

    def verify_sealed(self, job_id: str, num: int) -> bool:
        sf = self._seal_file(job_id, num)
        if not sf.is_file():
            return False
        want = json.loads(sf.read_text(encoding="utf-8"))["digest"]
        return manifest_digest(build_manifest(self.attempt_path(job_id, num))) == want

    def cleanup_attempt(self, job_id: str, num: int) -> None:
        p = self.attempt_path(job_id, num)
        if p.exists():
            _rmtree(p)


def _rmtree(path: Path) -> None:
    def onerr(func, p, exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerr)
