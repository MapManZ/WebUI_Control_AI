"""CLI harness worker adapter supporting local CLIs (Antigravity, OpenCode, Hermes, DSH, Codex)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.sandbox.manifest_hasher import build_manifest, changed_paths, diff_manifests
from src.sandbox.path_guard import PathPolicy
from src.sandbox.workdir_guard import validate_sandbox_dir
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


_CMD_METACHARS = ("&", "|", ">", "<", "%", "^", "!", "(", ")", "`", "\r", "\n", '"')
_SAFE_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _is_cmd_wrapper(path: str | os.PathLike) -> bool:
    return str(path).strip().lower().endswith((".cmd", ".bat"))


def _resolve_direct_executable(harness_name: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Locate the direct binary (.exe) for a harness without going through a .cmd/.bat wrapper."""
    all_configs = load_harness_config()
    cfg = cfg or all_configs.get(harness_name) or {}
    candidates: List[str] = []

    raw_exe = os.path.expandvars(str(cfg.get("executable", "")).strip())
    if raw_exe:
        candidates.append(raw_exe)

    if harness_name == "dsh":
        dsh_home = os.environ.get("DSH_HOME", "").strip()
        if dsh_home:
            candidates.append(os.path.join(dsh_home, "DeepSeek Harness.exe"))
        for root_env in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(root_env, "").strip()
            if base:
                candidates.append(os.path.join(base, "Programs", "DeepSeek Harness", "DeepSeek Harness.exe"))
                candidates.append(os.path.join(base, "DeepSeek Harness", "DeepSeek Harness.exe"))
        candidates.append(r"O:\DSHarness\DeepSeek Harness\DeepSeek Harness.exe")
        which_names = ["dsh.exe", "DeepSeek Harness.exe", "dsh"]
    elif harness_name == "opencode":
        if os.environ.get("OPENCODE_CLI_PATH", "").strip():
            candidates.append(os.environ["OPENCODE_CLI_PATH"].strip())
        for root_env in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(root_env, "").strip()
            if base:
                candidates.append(os.path.join(base, "Programs", "ai.opencode.desktop", "resources", "opencode-cli.exe"))
                candidates.append(os.path.join(base, "OpenCode", "opencode-cli.exe"))
        which_names = ["opencode-cli.exe", "opencode.exe", "opencode-cli", "opencode"]
    elif harness_name == "hermes":
        if os.environ.get("HERMES_CLI_PATH", "").strip():
            candidates.append(os.environ["HERMES_CLI_PATH"].strip())
        for root_env in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(root_env, "").strip()
            if base:
                candidates.append(os.path.join(base, "hermes", "bin", "hermes.exe"))
        which_names = ["hermes.exe", "hermes"]
    elif harness_name == "antigravity":
        if os.environ.get("AGY_CLI_PATH", "").strip():
            candidates.append(os.environ["AGY_CLI_PATH"].strip())
        for root_env in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(root_env, "").strip()
            if base:
                candidates.append(os.path.join(base, "agy", "bin", "agy.exe"))
        which_names = ["agy.exe", "agy"]
    else:
        which_names = [f"{raw_exe}.exe", raw_exe] if raw_exe else [f"{harness_name}.exe", harness_name]

    for cand in candidates:
        expanded = os.path.expandvars(cand)
        if os.path.isabs(expanded) and os.path.isfile(expanded) and not _is_cmd_wrapper(expanded):
            return expanded

    for name in which_names:
        if not name:
            continue
        found = shutil.which(name)
        if found and not _is_cmd_wrapper(found):
            return found
    return None


def _validate_safe_arg(value: str, *, label: str = "arg", uses_cmd_wrapper: bool = False) -> str:
    """Validate a subprocess argv value, blocking NUL bytes and BatBadBut cmd.exe metacharacters."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if "\x00" in value:
        raise ValueError(f"NUL byte rejected in {label}")
    if uses_cmd_wrapper:
        for ch in _CMD_METACHARS:
            if ch in value:
                raise ValueError(
                    f"Unsafe cmd.exe metacharacter {ch!r} rejected in {label} when using .cmd/.bat wrapper"
                )
    return value


def _validate_safe_goal(goal: str, uses_cmd_wrapper: bool = False) -> str:
    """Validate goal text before passing to a subprocess, blocking BatBadBut cmd.exe metacharacters."""
    return _validate_safe_arg(goal, label="goal", uses_cmd_wrapper=uses_cmd_wrapper)


def _validate_safe_argv(argv: List[str], uses_cmd_wrapper: bool = False) -> None:
    """Validate every argv element before Popen, preventing cmd.exe injection via sandbox_dir or args."""
    for idx, item in enumerate(argv):
        _validate_safe_arg(str(item), label=f"argv[{idx}]", uses_cmd_wrapper=uses_cmd_wrapper)


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

    resolved_exe = _resolve_direct_executable(harness_name, cfg)
    if not resolved_exe and wrapper_path and wrapper_path.is_file():
        resolved_exe = str(wrapper_path)

    if not resolved_exe:
        return {
            "name": harness_name,
            "available": False,
            "version": "",
            "executable": raw_exe,
            "error": f"Binary not found: {raw_exe}",
            "optional": optional,
        }

    is_wrapper = _is_cmd_wrapper(resolved_exe)

    # Special handling for DSH
    if harness_name == "dsh":
        # Prefer direct Electron binary with ELECTRON_RUN_AS_NODE=1 over .cmd wrapper
        if not is_wrapper:
            cmd = [
                resolved_exe,
                "-e",
                "const path=require('path'),url=require('url');"
                "process.argv=['node','dsh','--profile','headless','--help']; process.pkg={}; "
                "const binPath=path.join(path.dirname(process.execPath),'resources','app.asar','dsh','node_modules','@deepseek-ai','dsh','lib','bin.js');"
                "import(url.pathToFileURL(binPath).href).then(m => m.runCli())",
            ]
            env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
        else:
            cmd = [resolved_exe, "--profile", "headless", "--help"]
            env = os.environ.copy()
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=10, shell=False)
            if res.returncode == 0:
                first_line = (res.stdout or res.stderr).strip().splitlines()[0] if (res.stdout or res.stderr) else "dsh headless ready"
                return {
                    "name": harness_name,
                    "available": True,
                    "version": first_line,
                    "executable": resolved_exe,
                    "is_cmd_wrapper": is_wrapper,
                    "error": None,
                    "optional": optional,
                }
            return {
                "name": harness_name,
                "available": False,
                "version": "",
                "executable": resolved_exe,
                "is_cmd_wrapper": is_wrapper,
                "error": f"Exit code {res.returncode}: {res.stderr[:200]}",
                "optional": optional,
            }
        except Exception as e:
            return {
                "name": harness_name,
                "available": False,
                "version": "",
                "executable": resolved_exe,
                "is_cmd_wrapper": is_wrapper,
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
            shell=False,
        )
        if res.returncode == 0:
            version_str = (res.stdout or res.stderr).strip().splitlines()[0] if (res.stdout or res.stderr) else "detected"
            return {
                "name": harness_name,
                "available": True,
                "version": version_str,
                "executable": resolved_exe,
                "is_cmd_wrapper": is_wrapper,
                "error": None,
                "optional": optional,
            }
        return {
            "name": harness_name,
            "available": False,
            "version": "",
            "executable": resolved_exe,
            "is_cmd_wrapper": is_wrapper,
            "error": f"Probe exit code {res.returncode}: {res.stderr[:200]}",
            "optional": optional,
        }
    except Exception as e:
        return {
            "name": harness_name,
            "available": False,
            "version": "",
            "executable": resolved_exe,
            "is_cmd_wrapper": is_wrapper,
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

    def _snapshot_dir(self, directory: Path) -> dict[str, str]:
        if not directory.exists():
            return {}
        return build_manifest(directory, exclude_dirs=(), exclude_suffixes=())

    def start(self, job_contract: Any, sandbox_dir: str, attempt_id: str, **kwargs: Any) -> WorkerResult:
        start_time = time.time()
        sandbox = validate_sandbox_dir(
            sandbox_dir,
            workspace_root=REPO_ROOT,
            require_sandbox_subroot=True,
            create=True,
        )

        if hasattr(job_contract, "to_dict"):
            c_dict = job_contract.to_dict()
        elif isinstance(job_contract, dict):
            c_dict = job_contract
        else:
            c_dict = vars(job_contract) if hasattr(job_contract, "__dict__") else {}

        harness_name = kwargs.get("harness_name") or c_dict.get("harness_name") or self.harness_name
        job_id = str(c_dict.get("job_id", kwargs.get("job_id", "cli-job-001")))
        goal = str(c_dict.get("goal", kwargs.get("prompt", "Execute task")))
        raw_timeout_val = c_dict.get("timeout_seconds")
        if raw_timeout_val is None:
            raw_timeout_val = kwargs.get("timeout_seconds")
        raw_timeout = int(raw_timeout_val if raw_timeout_val is not None else 180)
        timeout_seconds = max(1, min(3600, raw_timeout))
        requested_model = kwargs.get("model") or c_dict.get("model")

        # Never trust caller-supplied role="reviewer" unless internal trusted adapter flag is set
        if kwargs.get("_trusted_reviewer_adapter") is True:
            requested_role = str(kwargs.get("role") or c_dict.get("role") or "reviewer").strip().lower() or "reviewer"
        else:
            requested_role = "worker"

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

        resolved_exe = str(probe["executable"])
        uses_cmd_wrapper = bool(probe.get("is_cmd_wrapper")) or _is_cmd_wrapper(resolved_exe)
        if harness_name == "dsh":
            direct_dsh = _resolve_direct_executable("dsh", harness_cfg)
            if direct_dsh:
                resolved_exe = direct_dsh
                uses_cmd_wrapper = False

        try:
            goal = _validate_safe_goal(goal, uses_cmd_wrapper=uses_cmd_wrapper)
            _validate_safe_arg(str(sandbox), label="sandbox_dir", uses_cmd_wrapper=uses_cmd_wrapper)
        except ValueError as exc:
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type=f"cli_{harness_name}",
                status=STATUS_FAILED,
                exit_code=1,
                duration_ms=duration_ms,
                stdout="",
                stderr=f"Policy block: {exc}",
                metadata={"policy_blocked": True, "model": resolved_model, "role": requested_role},
            )

        env = os.environ.copy()
        env["ORCHESTRA_MODEL"] = resolved_model
        env["ORCHESTRA_ENFORCED_MODEL"] = resolved_model
        env["ORCHESTRA_ROLE"] = requested_role

        # Build command invocation (preferring direct .exe with shell=False over .cmd wrappers)
        if harness_name == "dsh":
            if not uses_cmd_wrapper:
                script = (
                    "const path=require('path'),url=require('url');"
                    f"process.argv=['node','dsh','--profile','headless','--json',JSON.parse({json.dumps(goal)})]; "
                    "process.pkg={}; "
                    "const binPath=path.join(path.dirname(process.execPath),'resources','app.asar','dsh','node_modules','@deepseek-ai','dsh','lib','bin.js');"
                    "import(url.pathToFileURL(binPath).href).then(m => m.runCli())"
                )
                cmd = [resolved_exe, "-e", script]
                env["ELECTRON_RUN_AS_NODE"] = "1"
            else:
                cmd = [resolved_exe, "--profile", "headless", "--json", goal]
        elif harness_name == "hermes":
            profile = str(kwargs.get("profile") or c_dict.get("harness_profile", "default")).strip()
            if not _SAFE_PROFILE_RE.match(profile):
                profile = "default"
            cmd = [resolved_exe, "-z", goal, "--in", str(sandbox), "--no-restore-cwd", "--safe-mode", "--profile", profile]
        elif harness_name == "opencode":
            cmd = [resolved_exe, "--prompt", goal, "--standalone", str(sandbox)]
        elif harness_name == "antigravity":
            cmd = [resolved_exe, "eval", goal]
        else:
            template = (harness_cfg or {}).get("run_args_template", ["--prompt", "{prompt}"])
            expanded = []
            for arg in template:
                expanded.append(arg.format(prompt=goal, sandbox=str(sandbox), model=resolved_model))
            cmd = [resolved_exe, *expanded]

        model_flag = (harness_cfg or {}).get("model_flag")
        if model_flag and isinstance(model_flag, str):
            cmd.extend([model_flag, resolved_model])

        try:
            _validate_safe_argv(cmd, uses_cmd_wrapper=uses_cmd_wrapper)
        except ValueError as exc:
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type=f"cli_{harness_name}",
                status=STATUS_FAILED,
                exit_code=1,
                duration_ms=duration_ms,
                stdout="",
                stderr=f"Policy block: {exc}",
                metadata={"policy_blocked": True, "model": resolved_model, "role": requested_role},
            )

        pre_manifest = build_manifest(sandbox, exclude_dirs=(), exclude_suffixes=())

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(sandbox),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                shell=False,
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

        post_manifest = build_manifest(sandbox, exclude_dirs=(), exclude_suffixes=())
        changed = changed_paths(diff_manifests(pre_manifest, post_manifest))

        awp = c_dict.get("allowed_write_paths")
        if awp and changed:
            policy = PathPolicy(
                list(awp),
                list(c_dict.get("forbidden_write_roots") or []),
                sandbox_root=sandbox,
            )
            pol = policy.check_changed(changed, sandbox)
            if not pol.ok:
                viol_summary = ", ".join(sorted({f"{v['path']}[{v['code']}]" for v in pol.violations})[:10])
                return WorkerResult(
                    job_id=job_id,
                    attempt_id=attempt_id,
                    worker_type=f"cli_{harness_name}",
                    status=STATUS_FAILED,
                    changed_files=changed,
                    stdout=stdout or "",
                    stderr=((stderr or "") + f"\nScope violation: {viol_summary}").strip(),
                    exit_code=1,
                    duration_ms=duration_ms,
                    metadata={
                        "command": cmd,
                        "probe": probe,
                        "model": resolved_model,
                        "role": requested_role,
                        "scope_violation": True,
                        "policy_violations": pol.violations,
                    },
                )

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
