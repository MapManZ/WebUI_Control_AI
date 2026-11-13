"""Independent QA runner: executes contract ``acceptance_checks`` in the sandbox, never trusting the worker."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field

from src.sandbox.win_job_object import run_bounded

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"(?i)\b(?:[A-Z0-9_]*API_KEY)\s*[=:]\s*[^\s\"']{4,}"),
    re.compile(r"(?i)\b(?:[A-Z0-9_]*SECRET)\s*[=:]\s*[^\s\"']{4,}"),
    re.compile(r"(?i)\b(?:[A-Z0-9_]*(?:TOKEN|PASSWORD))\s*[=:]\s*[^\s\"']{6,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]
REDACTION = "[REDACTED]"

SAFE_ENV_KEYS = ("SystemRoot", "SYSTEMROOT", "PATH", "PATHEXT", "COMSPEC", "WINDIR", "TEMP", "TMP", "OS",
                 "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS")


def contains_secret(text: str) -> bool:
    return any(p.search(text or "") for p in SECRET_PATTERNS)


def redact(text: str) -> str:
    out = text or ""
    for p in SECRET_PATTERNS:
        out = p.sub(REDACTION, out)
    return out


def sanitized_env(sandbox_dir: str, extra: dict | None = None) -> dict:
    env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": sandbox_dir,
        "NO_COLOR": "1",
        "HOME": sandbox_dir,
        "USERPROFILE": sandbox_dir,
    })
    if extra:
        env.update(extra)
    return env


def normalize_check(chk, idx: int = 0) -> dict:
    """Accept str or dict acceptance checks -> canonical dict with a stable check_id."""
    if isinstance(chk, str):
        d = {"command": chk}
    elif isinstance(chk, dict):
        d = dict(chk)
    else:
        raise ValueError(f"unsupported acceptance check type {type(chk).__name__}")
    command = d.get("command") or d.get("cmd") or d.get("run") or ""
    ctype = (d.get("type") or d.get("kind") or ("browser" if d.get("browser") else "command")).lower()
    if ctype not in ("command", "browser"):
        ctype = "command"
    required = bool(d.get("required", True))
    ident = hashlib.sha256(f"{ctype}\n{command}\n{d.get('url', '')}".encode("utf-8")).hexdigest()[:16]
    return {
        "check_id": d.get("id") and f"{d['id']}:{ident}" or ident,
        "type": ctype,
        "command": command,
        "url": d.get("url"),
        "required": required,
        "timeout_seconds": d.get("timeout_seconds"),
        "index": idx,
    }


def contract_check_ids(contract: dict) -> list[str]:
    return [normalize_check(c, i)["check_id"] for i, c in enumerate(contract.get("acceptance_checks") or [])]


def find_browser() -> str | None:
    for name in ("msedge", "msedge.exe"):
        p = shutil.which(name)
        if p:
            return p
    for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if os.path.isfile(p):
            return p
    return None


@dataclass
class CheckResult:
    check_id: str
    type: str
    command: str
    required: bool
    status: str  # PASSED | FAILED | NOT_VERIFIED | POLICY_BLOCKED
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    secret_leak_detected: bool = False
    note: str = ""


@dataclass
class QAReport:
    report_id: str
    job_id: str
    attempt_id: str
    sandbox_dir: str
    checks: list[CheckResult] = field(default_factory=list)
    all_exit_zero: bool = False
    security_violation: bool = False
    not_verified: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    report_sha256: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def body_digest(self) -> str:
        d = self.to_dict()
        d.pop("report_sha256", None)
        return hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def seal(self) -> "QAReport":
        self.report_sha256 = self.body_digest()
        return self

    def verify_seal(self) -> bool:
        return bool(self.report_sha256) and self.report_sha256 == self.body_digest()

    @classmethod
    def from_dict(cls, d: dict) -> "QAReport":
        d = dict(d)
        d["checks"] = [CheckResult(**c) for c in d.get("checks", [])]
        return cls(**d)


def _first_token_raw(cmd: str) -> str:
    try:
        return shlex.split(cmd, posix=False)[0].strip('"')
    except (ValueError, IndexError):
        return ""


def _first_token_name(cmd: str) -> str:
    tok = _first_token_raw(cmd)
    if not tok:
        return ""
    name = os.path.basename(tok).lower()
    for ext in (".exe", ".cmd", ".bat"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return {"python3": "python", "py": "python"}.get(name, name)


def command_allowed(command: str, allowed_commands: list) -> bool:
    """Contract must explicitly list allowed_commands; empty allowlist rejects all commands."""
    if not allowed_commands:
        return False
    cmd_norm = " ".join(command.split()).lower()
    raw_first = _first_token_raw(command)
    has_path_sep = ("/" in raw_first) or ("\\" in raw_first)
    first = _first_token_name(command)
    if not first:
        return False

    sys_py_real = os.path.normcase(os.path.realpath(os.path.abspath(sys.executable)))
    raw_first_real = os.path.normcase(os.path.realpath(os.path.abspath(raw_first))) if has_path_sep else ""

    for a in allowed_commands:
        a_str = a if isinstance(a, str) else (a.get("command") or "")
        a_norm = " ".join(a_str.split()).lower()
        if not a_norm:
            continue
        a_raw_first = _first_token_raw(a_str)
        a_has_sep = ("/" in a_raw_first) or ("\\" in a_raw_first)
        if has_path_sep:
            if a_has_sep:
                a_real = os.path.normcase(os.path.realpath(os.path.abspath(a_raw_first)))
                if raw_first_real != a_real:
                    continue
                if cmd_norm == a_norm or cmd_norm.startswith(a_norm + " ") or " " not in a_norm:
                    return True
            else:
                if " " not in a_norm and _first_token_name(a_str) == "python" and raw_first_real == sys_py_real:
                    return True
            continue
        if cmd_norm == a_norm or cmd_norm.startswith(a_norm + " "):
            return True
        if " " not in a_norm and first == _first_token_name(a_str):
            return True
    return False


class QARunner:
    def __init__(self, *, now_fn=None, browser_path: str | None | bool = False, python_exe: str | None = None):
        """``browser_path=False`` -> auto-detect; ``None`` -> force unavailable; str -> use that executable."""
        import datetime as dt

        self.now_fn = now_fn or (lambda: dt.datetime.now(dt.timezone.utc))
        self.browser_path = find_browser() if browser_path is False else browser_path
        self.python_exe = python_exe or sys.executable

    def _argv(self, command: str) -> list[str]:
        argv = shlex.split(command, posix=(os.name != "nt"))
        argv = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] == '"' else a for a in argv]
        if argv and _first_token_name(command) == "python":
            argv[0] = self.python_exe
        return argv

    def run_checks(
        self,
        contract: dict,
        sandbox_dir: str,
        job_id: str,
        attempt_id: str,
        *,
        disallow_secret_leak: bool = True,
        default_timeout: float | None = None,
        extra_env: dict | None = None,
    ) -> QAReport:
        ts = lambda: self.now_fn().strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # noqa: E731
        report = QAReport(report_id=uuid.uuid4().hex, job_id=job_id, attempt_id=attempt_id,
                          sandbox_dir=os.path.abspath(sandbox_dir), started_at=ts())
        timeout = float(default_timeout or contract.get("timeout_seconds") or 180)
        allowed = contract.get("allowed_commands") or []
        checks = contract.get("acceptance_checks") or []
        if not checks:
            report.not_verified.append("contract has no acceptance_checks")
        for i, raw in enumerate(checks):
            chk = normalize_check(raw, i)
            report.checks.append(self._run_one(chk, sandbox_dir, allowed, timeout, disallow_secret_leak, extra_env,
                                               report))
        report.all_exit_zero = bool(report.checks) and all(
            (c.status == "PASSED" and c.exit_code == 0) or (not c.required and c.status == "NOT_VERIFIED")
            for c in report.checks
        )
        report.finished_at = ts()
        return report.seal()

    def _run_one(self, chk, sandbox_dir, allowed, default_timeout, disallow_leak, extra_env, report) -> CheckResult:
        res = CheckResult(chk["check_id"], chk["type"], redact(chk["command"] or (chk.get("url") or "")), chk["required"],
                          "FAILED", None)
        if chk["type"] == "browser":
            return self._run_browser(chk, res, sandbox_dir, default_timeout, report)
        if not chk["command"]:
            res.note = "empty command"
            return res
        if not command_allowed(chk["command"], allowed):
            res.status = "POLICY_BLOCKED"
            res.note = "command not in contract allowed_commands"
            report.security_violation = True
            return res
        argv = self._argv(chk["command"])
        pr = run_bounded(argv, cwd=sandbox_dir, env=sanitized_env(os.path.abspath(sandbox_dir), extra_env),
                         timeout=float(chk["timeout_seconds"] or default_timeout))
        res.exit_code = pr.exit_code
        res.duration_ms = pr.duration_ms
        res.timed_out = pr.timed_out
        leaked = contains_secret(pr.stdout) or contains_secret(pr.stderr)
        res.secret_leak_detected = leaked
        res.stdout = redact(pr.stdout)[-20000:]
        res.stderr = redact(pr.stderr)[-20000:]
        if leaked and disallow_leak:
            report.security_violation = True
            res.note = "raw secret detected in check output (redacted)"
        if pr.spawn_error:
            res.note = f"spawn error: {pr.spawn_error}"
        res.status = "PASSED" if (pr.exit_code == 0 and not pr.timed_out and not (leaked and disallow_leak)) else "FAILED"
        return res

    def _run_browser(self, chk, res: CheckResult, sandbox_dir, timeout, report) -> CheckResult:
        res.command = chk.get("url") or chk.get("command") or "browser-check"
        if not self.browser_path:
            res.status = "NOT_VERIFIED"
            res.note = "Edge/browser unavailable; required browser check cannot be verified"
            if chk["required"]:
                report.not_verified.append(f"browser check {res.check_id} unavailable")
            return res
        url = chk.get("url") or chk.get("command")
        if not url:
            res.note = "browser check has no url"
            return res
        profile = os.path.join(os.path.abspath(sandbox_dir), "..", f".browser-profile-{uuid.uuid4().hex[:8]}")
        argv = [self.browser_path, "--headless=new", "--disable-gpu", f"--user-data-dir={os.path.abspath(profile)}",
                "--dump-dom", url]
        pr = run_bounded(argv, cwd=sandbox_dir, env=sanitized_env(os.path.abspath(sandbox_dir)),
                         timeout=float(chk["timeout_seconds"] or timeout))
        shutil.rmtree(profile, ignore_errors=True)
        res.exit_code = pr.exit_code
        res.duration_ms = pr.duration_ms
        res.timed_out = pr.timed_out
        res.stdout = redact(pr.stdout)[:4000]
        res.stderr = redact(pr.stderr)[-2000:]
        res.status = "PASSED" if (pr.exit_code == 0 and pr.stdout.strip() and not pr.timed_out) else "FAILED"
        return res
