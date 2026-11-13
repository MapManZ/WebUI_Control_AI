"""CLI harness worker adapter supporting local CLIs (Antigravity, OpenCode, Hermes, DSH, Codex)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.workers.base import (
    WorkerAdapter,
    WorkerResult,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    STATUS_CANCELLED,
    STATUS_BLOCKED_TOOL_UNAVAILABLE,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "harnesses.json"

CLIS = ("antigravity", "opencode", "hermes", "dsh")

ARGON_BLOCK_MESSAGE = (
    "Policy block: Gemini 4 Argon is reserved for Review and Merge only; "
    "use Gemini 3.8 Flash, Gemini 4 Hydrogen, or Claude Sonnet 5.5 for worker tasks"
)

DEFAULT_SUPPORTED_MODELS: Dict[str, Dict[str, Any]] = {
    "gemini-3.8-flash": {
        "display_name": "Gemini 3.8 Flash",
        "tier": "default_worker",
        "role": "worker",
        "allow_worker_execution": True,
        "default": True,
        "description": "Default Worker model for general coding & fast execution",
    },
    "gemini-4-hydrogen": {
        "display_name": "Gemini 4 Hydrogen",
        "tier": "high_capability_worker",
        "role": "worker",
        "allow_worker_execution": True,
        "default": False,
        "description": "High-capability Worker model for reasoning, implementation & multi-step tasks",
    },
    "claude-sonnet-5.5": {
        "display_name": "Claude Sonnet 5.5",
        "tier": "hard_task_worker",
        "role": "worker",
        "allow_worker_execution": True,
        "default": False,
        "description": "Hard-task Worker model reserved for complex architecture/security tasks",
    },
    "gemini-4-argon": {
        "display_name": "Gemini 4 Argon",
        "tier": "coordinator_reviewer_merge",
        "role": "reviewer_merge_only",
        "allow_worker_execution": False,
        "default": False,
        "description": "Coordinator/Reviewer/Merge-Only model; never used as a regular coding worker",
    },
}

DEFAULT_MODEL_POLICY: Dict[str, Any] = {
    "default_worker_model": "gemini-3.8-flash",
    "allowed_worker_models": [
        "gemini-3.8-flash",
        "gemini-4-hydrogen",
        "claude-sonnet-5.5",
    ],
    "reviewer_merge_only_models": ["gemini-4-argon"],
    "rules": {
        "gemini-4-argon": {
            "role": "reviewer_merge_only",
            "allow_worker_execution": False,
            "allowed_roles": ["reviewer", "merge"],
            "block_message": ARGON_BLOCK_MESSAGE,
        }
    },
}

MODEL_ALIASES: Dict[str, str] = {
    "gemini 3.8 flash": "gemini-3.8-flash",
    "gemini-3.8-flash": "gemini-3.8-flash",
    "gemini 4 hydrogen": "gemini-4-hydrogen",
    "gemini-4-hydrogen": "gemini-4-hydrogen",
    "claude sonnet 5.5": "claude-sonnet-5.5",
    "claude-sonnet-5.5": "claude-sonnet-5.5",
    "gemini 4 argon": "gemini-4-argon",
    "gemini-4-argon": "gemini-4-argon",
    "gemini-4-argon (review-only)": "gemini-4-argon",
}

DEFAULT_HARNESSES: Dict[str, Dict[str, Any]] = {
    "antigravity": {
        "name": "Google Antigravity CLI",
        "executable": r"%LOCALAPPDATA%\agy\bin\agy.exe",
        "wrapper": r"bin\agy-cli.cmd",
        "version_args": ["--version"],
        "protocols": ["mcp", "cli"],
        "optional": False,
    },
    "opencode": {
        "name": "OpenCode Desktop CLI",
        "executable": r"%LOCALAPPDATA%\Programs\ai.opencode.desktop\resources\opencode-cli.exe",
        "wrapper": r"bin\opencode-cli.cmd",
        "version_args": ["--version"],
        "run_args_template": ["--prompt", "{prompt}", "--standalone", "{sandbox}"],
        "protocols": ["cli", "acp", "mcp"],
        "optional": False,
    },
    "hermes": {
        "name": "Hermes Agent CLI",
        "executable": r"%LOCALAPPDATA%\hermes\bin\hermes.exe",
        "wrapper": r"bin\hermes-cli.cmd",
        "version_args": ["--version"],
        "run_args_template": ["-z", "{prompt}", "--in", "{sandbox}", "--no-restore-cwd", "--safe-mode"],
        "profiles": ["default", "fast", "research", "reviewer"],
        "protocols": ["cli", "acp", "mcp"],
        "optional": False,
    },
    "dsh": {
        "name": "DeepSeek Harness CLI",
        "executable": r"%LOCALAPPDATA%\Programs\DeepSeek Harness\DeepSeek Harness.exe",
        "wrapper": r"bin\dsh-cli.cmd",
        "version_args": ["--version"],
        "run_args_template": ["--profile", "headless", "--json", "{prompt}"],
        "protocols": ["cli", "acp", "mcp"],
        "electron_node": True,
        "optional": False,
    },
    "codex": {
        "name": "Codex Agent CLI (Extensible)",
        "executable": "codex",
        "wrapper": r"bin\codex-cli.cmd",
        "version_args": ["--version"],
        "run_args_template": ["exec", "--prompt", "{prompt}"],
        "protocols": ["cli"],
        "optional": True,
    },
}


def load_harness_config(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    target = path or CONFIG_PATH
    configs = dict(DEFAULT_HARNESSES)
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "harnesses" in data:
                for k, v in data["harnesses"].items():
                    if isinstance(v, dict):
                        configs[k] = {**configs.get(k, {}), **v}
        except Exception:
            pass
    return configs


def load_supported_models(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    target = path or CONFIG_PATH
    models = {k: dict(v) for k, v in DEFAULT_SUPPORTED_MODELS.items()}
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("supported_models"), dict):
                for k, v in data["supported_models"].items():
                    if isinstance(v, dict):
                        models[k] = {**models.get(k, {}), **v}
        except Exception:
            pass
    return models


def load_model_policy(path: Optional[Path] = None) -> Dict[str, Any]:
    target = path or CONFIG_PATH
    policy = json.loads(json.dumps(DEFAULT_MODEL_POLICY))
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("model_policy"), dict):
                raw_policy = data["model_policy"]
                for key in ("default_worker_model", "allowed_worker_models", "reviewer_merge_only_models"):
                    if key in raw_policy:
                        policy[key] = raw_policy[key]
                if isinstance(raw_policy.get("rules"), dict):
                    merged_rules = dict(policy.get("rules", {}))
                    for k, v in raw_policy["rules"].items():
                        if isinstance(v, dict):
                            merged_rules[k] = {**merged_rules.get(k, {}), **v}
                    policy["rules"] = merged_rules
        except Exception:
            pass
    return policy


def normalize_model_name(model: Optional[str], default_model: str = "gemini-3.8-flash") -> str:
    if not model or not str(model).strip():
        return default_model
    raw = str(model).strip()
    lowered = raw.lower()
    if lowered in MODEL_ALIASES:
        return MODEL_ALIASES[lowered]
    return raw


def validate_model_policy(
    model: Optional[str],
    role: str = "worker",
    config_path: Optional[Path] = None,
) -> tuple[bool, str, Optional[str]]:
    """Validate whether a model is permitted for the given execution role."""
    policy = load_model_policy(config_path)
    models = load_supported_models(config_path)
    default_model = str(policy.get("default_worker_model", "gemini-3.8-flash"))
    resolved_model = normalize_model_name(model, default_model=default_model)
    norm_role = (role or "worker").strip().lower()

    rules = policy.get("rules", {})
    reviewer_only = set(policy.get("reviewer_merge_only_models", []))
    model_meta = models.get(resolved_model, {})

    if resolved_model in reviewer_only or model_meta.get("allow_worker_execution") is False or resolved_model in rules:
        rule = rules.get(resolved_model, {})
        allowed_roles = {str(r).strip().lower() for r in rule.get("allowed_roles", ["reviewer", "merge"])}
        allow_worker = rule.get("allow_worker_execution", model_meta.get("allow_worker_execution", False))
        if not allow_worker and norm_role not in allowed_roles:
            msg = str(rule.get("block_message") or ARGON_BLOCK_MESSAGE)
            return False, resolved_model, msg

    return True, resolved_model, None


def kill_process_tree(pid: int) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except Exception:
            pass


def probe_harness(harness_name: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    all_configs = load_harness_config()
    cfg = config or all_configs.get(harness_name)
    if not cfg:
        return {
            "name": harness_name,
            "available": False,
            "version": "",
            "executable": "",
            "error": f"Unknown harness: {harness_name}",
            "optional": True,
        }

    raw_exe = os.path.expandvars(str(cfg.get("executable", harness_name)))
    wrapper_rel = cfg.get("wrapper")
    wrapper_path = (REPO_ROOT / wrapper_rel) if wrapper_rel else None
    optional = bool(cfg.get("optional", False))

    resolved_exe = None
    if wrapper_path and wrapper_path.is_file():
        resolved_exe = str(wrapper_path)
    elif os.path.isabs(raw_exe) and os.path.exists(raw_exe):
        resolved_exe = raw_exe
    else:
        resolved_exe = shutil.which(raw_exe)

    if not resolved_exe:
        return {
            "name": harness_name,
            "available": False,
            "version": "",
            "executable": raw_exe,
            "error": f"Binary not found: {raw_exe}",
            "optional": optional,
        }

    # Special handling for DSH
    if harness_name == "dsh":
        # Check wrapper or execute with ELECTRON_RUN_AS_NODE=1
        if wrapper_path and wrapper_path.is_file():
            cmd = [str(wrapper_path), "--profile", "headless", "--help"]
            env = os.environ.copy()
        else:
            cmd = [
                resolved_exe,
                "-e",
                "const path=require('path'),url=require('url');"
                "process.argv=['node','dsh','--profile','headless','--help']; process.pkg={}; "
                "const binPath=path.join(path.dirname(process.execPath),'resources','app.asar','dsh','node_modules','@deepseek-ai','dsh','lib','bin.js');"
                "import(url.pathToFileURL(binPath).href).then(m => m.runCli())",
            ]
            env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=10)
            if res.returncode == 0:
                first_line = (res.stdout or res.stderr).strip().splitlines()[0] if (res.stdout or res.stderr) else "dsh headless ready"
                return {
                    "name": harness_name,
                    "available": True,
                    "version": first_line,
                    "executable": resolved_exe,
                    "error": None,
                    "optional": optional,
                }
            return {
                "name": harness_name,
                "available": False,
                "version": "",
                "executable": resolved_exe,
                "error": f"Exit code {res.returncode}: {res.stderr[:200]}",
                "optional": optional,
            }
        except Exception as e:
            return {
                "name": harness_name,
                "available": False,
                "version": "",
                "executable": resolved_exe,
                "error": str(e),
                "optional": optional,
            }

    # Standard harness version probe
    version_args = cfg.get("version_args", ["--version"])
    try:
        res = subprocess.run(
            [resolved_exe, *version_args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0:
            version_str = (res.stdout or res.stderr).strip().splitlines()[0] if (res.stdout or res.stderr) else "detected"
            return {
                "name": harness_name,
                "available": True,
                "version": version_str,
                "executable": resolved_exe,
                "error": None,
                "optional": optional,
            }
        return {
            "name": harness_name,
            "available": False,
            "version": "",
            "executable": resolved_exe,
            "error": f"Probe exit code {res.returncode}: {res.stderr[:200]}",
            "optional": optional,
        }
    except Exception as e:
        return {
            "name": harness_name,
            "available": False,
            "version": "",
            "executable": resolved_exe,
            "error": str(e),
            "optional": optional,
        }


class CLIHarnessWorker(WorkerAdapter):
    """WorkerAdapter implementation for local CLI agent harnesses."""

    def __init__(self, harness_name: str = "opencode", config_path: Optional[Path] = None):
        self.harness_name = harness_name
        self.config_path = config_path or CONFIG_PATH
        self.harnesses = load_harness_config(self.config_path)
        self.supported_models = load_supported_models(self.config_path)
        self.model_policy = load_model_policy(self.config_path)
        self._active_procs: Dict[str, subprocess.Popen] = {}
        self._cancelled_attempts: set[str] = set()

    def capabilities(self) -> Dict[str, Any]:
        return {
            "worker_type": "cli_harness",
            "harness_name": self.harness_name,
            "supported_harnesses": list(self.harnesses.keys()),
            "supported_models": list(self.supported_models.keys()),
            "model_policy": self.model_policy,
            "probe": probe_harness(self.harness_name, self.harnesses.get(self.harness_name)),
        }

    def cancel(self, attempt_id: str) -> bool:
        self._cancelled_attempts.add(attempt_id)
        proc = self._active_procs.get(attempt_id)
        if proc:
            kill_process_tree(proc.pid)
            return True
        return False

    def _snapshot_dir(self, directory: Path) -> set[Path]:
        if not directory.exists():
            return set()
        return set(directory.rglob("*"))

    def start(self, job_contract: Any, sandbox_dir: str, attempt_id: str, **kwargs: Any) -> WorkerResult:
        start_time = time.time()
        sandbox = Path(sandbox_dir).resolve()
        sandbox.mkdir(parents=True, exist_ok=True)

        if hasattr(job_contract, "to_dict"):
            c_dict = job_contract.to_dict()
        elif isinstance(job_contract, dict):
            c_dict = job_contract
        else:
            c_dict = vars(job_contract) if hasattr(job_contract, "__dict__") else {}

        harness_name = kwargs.get("harness_name") or c_dict.get("harness_name") or self.harness_name
        job_id = str(c_dict.get("job_id", kwargs.get("job_id", "cli-job-001")))
        goal = str(c_dict.get("goal", kwargs.get("prompt", "Execute task")))
        timeout_seconds = int(c_dict.get("timeout_seconds", kwargs.get("timeout_seconds", 180)))
        requested_model = kwargs.get("model") or c_dict.get("model")
        requested_role = str(kwargs.get("role") or c_dict.get("role") or "worker")

        allowed, resolved_model, policy_error = validate_model_policy(
            requested_model,
            role=requested_role,
            config_path=self.config_path,
        )
        if not allowed:
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type=f"cli_{harness_name}",
                status=STATUS_BLOCKED_TOOL_UNAVAILABLE,
                exit_code=1,
                duration_ms=duration_ms,
                stdout="",
                stderr=policy_error or ARGON_BLOCK_MESSAGE,
                metadata={
                    "policy_blocked": True,
                    "policy_error": policy_error or ARGON_BLOCK_MESSAGE,
                    "model": resolved_model,
                    "role": requested_role,
                },
            )

        harness_cfg = self.harnesses.get(harness_name)
        probe = probe_harness(harness_name, harness_cfg)
        if not probe.get("available"):
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type=f"cli_{harness_name}",
                status=STATUS_BLOCKED_TOOL_UNAVAILABLE,
                exit_code=127,
                duration_ms=duration_ms,
                stdout="",
                stderr=f"Tool unavailable: {probe.get('error', 'Binary not found')}",
                metadata={"probe": probe, "model": resolved_model, "role": requested_role},
            )

        resolved_exe = probe["executable"]
        env = os.environ.copy()
        env["ORCHESTRA_MODEL"] = resolved_model
        env["ORCHESTRA_ROLE"] = requested_role

        # Build command invocation
        if harness_name == "dsh":
            wrapper_rel = harness_cfg.get("wrapper") if harness_cfg else None
            wrapper_path = (REPO_ROOT / wrapper_rel) if wrapper_rel else None
            if wrapper_path and wrapper_path.is_file():
                cmd = [str(wrapper_path), "--profile", "headless", "--json", goal]
            else:
                script = (
                    f"const path=require('path'),url=require('url');"
                    f"process.argv=['node','dsh','--profile','headless','--json',JSON.parse({json.dumps(goal)})]; "
                    "process.pkg={}; "
                    "const binPath=path.join(path.dirname(process.execPath),'resources','app.asar','dsh','node_modules','@deepseek-ai','dsh','lib','bin.js');"
                    "import(url.pathToFileURL(binPath).href).then(m => m.runCli())"
                )
                cmd = [resolved_exe, "-e", script]
                env["ELECTRON_RUN_AS_NODE"] = "1"
        elif harness_name == "hermes":
            profile = kwargs.get("profile") or c_dict.get("harness_profile", "default")
            cmd = [resolved_exe, "-z", goal, "--in", str(sandbox), "--no-restore-cwd", "--safe-mode", "--profile", profile]
        elif harness_name == "opencode":
            cmd = [resolved_exe, "--prompt", goal, "--standalone", str(sandbox)]
        elif harness_name == "antigravity":
            cmd = [resolved_exe, "eval", goal]
        else:
            template = (harness_cfg or {}).get("run_args_template", ["--prompt", "{prompt}"])
            expanded = []
            for arg in template:
                expanded.append(arg.format(prompt=goal, sandbox=str(sandbox)))
            cmd = [resolved_exe, *expanded]

        pre_files = self._snapshot_dir(sandbox)

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(sandbox),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            self._active_procs[attempt_id] = proc
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type=f"cli_{harness_name}",
                status=STATUS_FAILED,
                exit_code=1,
                duration_ms=duration_ms,
                stdout="",
                stderr=f"Failed to start CLI process: {e}",
            )

        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            kill_process_tree(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=3)
            except Exception:
                stdout, stderr = "", "Process killed after timeout."
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type=f"cli_{harness_name}",
                status=STATUS_TIMEOUT,
                exit_code=124,
                duration_ms=duration_ms,
                stdout=stdout or "",
                stderr=stderr or "CLI harness timed out.",
            )
        finally:
            self._active_procs.pop(attempt_id, None)

        duration_ms = int((time.time() - start_time) * 1000)

        if attempt_id in self._cancelled_attempts:
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type=f"cli_{harness_name}",
                status=STATUS_CANCELLED,
                exit_code=130,
                duration_ms=duration_ms,
                stdout=stdout or "",
                stderr=stderr or "Cancelled by operator.",
            )

        post_files = self._snapshot_dir(sandbox)
        changed = []
        for p in post_files:
            if p not in pre_files and p.is_file():
                try:
                    rel = p.relative_to(sandbox).as_posix()
                    changed.append(rel)
                except Exception:
                    pass

        status = STATUS_COMPLETED if exit_code == 0 else STATUS_FAILED
        return WorkerResult(
            job_id=job_id,
            attempt_id=attempt_id,
            worker_type=f"cli_{harness_name}",
            status=status,
            changed_files=changed,
            stdout=stdout or "",
            stderr=stderr or "",
            exit_code=exit_code,
            duration_ms=duration_ms,
            metadata={"command": cmd, "probe": probe, "model": resolved_model, "role": requested_role},
        )
