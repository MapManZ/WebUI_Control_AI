"""Preflight environment, runtime, CLI harness, and target diagnostics."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _run_cmd(cmd: List[str] | str, timeout: int = 10) -> Tuple[bool, str]:
    """Execute command safely and capture output without shell=True."""
    try:
        if isinstance(cmd, str):
            argv = shlex.split(cmd, posix=False)
        else:
            argv = list(cmd)
        if argv and argv[0].lower().endswith((".cmd", ".bat")):
            argv = ["cmd.exe", "/d", "/s", "/c", *argv]
        res = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            shell=False,
            timeout=timeout,
        )
        out = (res.stdout or res.stderr or "").strip()
        return res.returncode == 0, out
    except Exception as e:
        return False, str(e)


def check_runtime(cmd_name: str, version_flag: str = "--version") -> Dict[str, Any]:
    """Check availability of a system CLI runtime."""
    which_path = shutil.which(cmd_name)
    if not which_path:
        return {
            "ok": False,
            "command": cmd_name,
            "path": None,
            "version": None,
            "error": "Executable not found in PATH",
        }

    ok, out = _run_cmd([cmd_name, version_flag])
    first_line = out.splitlines()[0] if out else ""
    return {
        "ok": ok,
        "command": cmd_name,
        "path": which_path,
        "version": first_line if ok else None,
        "error": None if ok else out,
    }


def run_doctor(repo_root: Optional[Path | str] = None) -> Dict[str, Any]:
    """Execute comprehensive preflight diagnostics across runtimes, harnesses, and boundaries."""
    if repo_root is None:
        root_path = Path(__file__).resolve().parent.parent.parent
    else:
        root_path = Path(repo_root).resolve()

    # 1. Probing Runtimes
    runtimes = {
        "python": check_runtime("python"),
        "node": check_runtime("node"),
        "bun": check_runtime("bun"),
        "git": check_runtime("git"),
        "moli": check_runtime("moli"),
        "uv": check_runtime("uv"),
    }

    # Verify python is at least 3.14
    if runtimes["python"]["ok"]:
        py_ver = sys.version_info
        if py_ver < (3, 14):
            runtimes["python"]["ok"] = False
            runtimes["python"]["error"] = f"Python >= 3.14 required; found {py_ver.major}.{py_ver.minor}.{py_ver.micro}"

    # 2. Probing CLI Harnesses
    harnesses_config_path = root_path / "config" / "harnesses.json"
    cli_harnesses: Dict[str, Any] = {}
    harness_configs = {}
    if harnesses_config_path.exists():
        try:
            with open(harnesses_config_path, "r", encoding="utf-8") as f:
                harness_configs = json.load(f).get("harnesses", {})
        except Exception as e:
            harness_configs = {}

    # Define harness definitions with portable fallbacks
    dsh_candidates = [
        os.path.join(os.environ.get("DSH_HOME", ""), "DeepSeek Harness.exe") if os.environ.get("DSH_HOME") else "",
        r"%LOCALAPPDATA%\Programs\DeepSeek Harness\DeepSeek Harness.exe",
        r"%PROGRAMFILES%\DeepSeek Harness\DeepSeek Harness.exe",
        r"O:\DSHarness\DeepSeek Harness\DeepSeek Harness.exe",
    ]
    dsh_default_exe = next(
        (os.path.expandvars(p) for p in dsh_candidates if p and Path(os.path.expandvars(p)).exists()),
        r"%LOCALAPPDATA%\Programs\DeepSeek Harness\DeepSeek Harness.exe",
    )

    harness_specs = {
        "antigravity": {
            "exe": r"%LOCALAPPDATA%\agy\bin\agy.exe",
            "wrapper": str(root_path / "bin" / "agy-cli.cmd"),
            "version_args": ["--version"],
            "optional": False,
        },
        "opencode": {
            "exe": r"%LOCALAPPDATA%\Programs\ai.opencode.desktop\resources\opencode-cli.exe",
            "wrapper": str(root_path / "bin" / "opencode-cli.cmd"),
            "version_args": ["--version"],
            "optional": False,
        },
        "hermes": {
            "exe": r"%LOCALAPPDATA%\hermes\bin\hermes.exe",
            "wrapper": str(root_path / "bin" / "hermes-cli.cmd"),
            "version_args": ["--version"],
            "optional": False,
        },
        "dsh": {
            "exe": dsh_default_exe,
            "wrapper": str(root_path / "bin" / "dsh-cli.cmd"),
            "version_args": ["--version"],
            "optional": False,
        },
        "codex": {
            "exe": "codex",
            "wrapper": str(root_path / "bin" / "codex-cli.cmd"),
            "version_args": ["--version"],
            "optional": True,
        },
    }

    # Merge config if present
    for key, spec in harness_configs.items():
        if key in harness_specs:
            if "executable" in spec:
                expanded_cfg_exe = os.path.expandvars(str(spec["executable"]))
                if Path(expanded_cfg_exe).exists() or shutil.which(expanded_cfg_exe) or key != "dsh":
                    harness_specs[key]["exe"] = expanded_cfg_exe
            if "wrapper" in spec:
                harness_specs[key]["wrapper"] = str(root_path / spec["wrapper"])
            if "optional" in spec:
                harness_specs[key]["optional"] = spec["optional"]

    for name, spec in harness_specs.items():
        expanded_exe = os.path.expandvars(str(spec["exe"]))
        exe_path = Path(expanded_exe)
        wrapper_path = Path(spec["wrapper"])
        optional = spec.get("optional", False)

        wrapper_exists = wrapper_path.exists()
        exe_exists = exe_path.exists() or bool(shutil.which(expanded_exe)) or wrapper_exists

        version_str = None
        probe_ok = False
        error_msg = None

        if wrapper_exists:
            # Test wrapper first
            cmd = ["cmd.exe", "/d", "/s", "/c", str(wrapper_path), "--version"]
            ok, out = _run_cmd(cmd, timeout=10)
            if ok and out:
                probe_ok = True
                version_str = out.splitlines()[0]
            else:
                error_msg = out or "Wrapper execution failed"
        elif exe_exists:
            cmd = [str(exe_path), "--version"]
            ok, out = _run_cmd(cmd, timeout=10)
            if ok and out:
                probe_ok = True
                version_str = out.splitlines()[0]
            else:
                error_msg = out or "Executable execution failed"
        else:
            if not optional:
                error_msg = f"Neither executable nor wrapper found for {name}"

        cli_harnesses[name] = {
            "ok": probe_ok if not optional else (probe_ok or not exe_exists),
            "available": probe_ok,
            "executable": str(exe_path),
            "executable_exists": exe_exists,
            "wrapper": str(wrapper_path),
            "wrapper_exists": wrapper_exists,
            "version": version_str,
            "optional": optional,
            "error": error_msg if not optional else None,
        }

    # 3. Checking WebUI targets config
    browser_cfg_path = root_path / "config" / "browser_targets.example.json"
    webui_targets: Dict[str, Any] = {}
    expected_providers = ["chatgpt", "claude", "gemini", "glm", "deepseek"]

    if browser_cfg_path.exists():
        try:
            with open(browser_cfg_path, "r", encoding="utf-8") as f:
                b_cfg = json.load(f).get("providers", {})
            for p in expected_providers:
                if p in b_cfg:
                    item = b_cfg[p]
                    has_url = bool(item.get("url"))
                    has_selectors = bool(item.get("selectors", {}).get("input"))
                    webui_targets[p] = {
                        "ok": has_url and has_selectors,
                        "url": item.get("url"),
                        "delivery_modes": item.get("delivery_modes", []),
                    }
                else:
                    webui_targets[p] = {
                        "ok": False,
                        "url": None,
                        "error": f"Provider '{p}' missing from config",
                    }
        except Exception as e:
            for p in expected_providers:
                webui_targets[p] = {"ok": False, "error": str(e)}
    else:
        for p in expected_providers:
            webui_targets[p] = {
                "ok": False,
                "error": f"Config not found at {browser_cfg_path}",
            }

    # 4. Checking Protected REA Project Root
    rea_root_path = Path(r"O:\AI_Workspace\REA Project\OpenCodeREA\SummoningRemake")
    rea_exists = rea_root_path.exists()
    protected_rea_root = {
        "path": str(rea_root_path),
        "exists": rea_exists,
        "status": "PROTECTED_READ_ONLY" if rea_exists else "MISSING",
        "serena_mcp_port": 9121,
    }

    # Overall OK calculation
    runtimes_ok = all(r["ok"] for r in runtimes.values())
    harnesses_ok = all(
        h["ok"] for k, h in cli_harnesses.items() if not h.get("optional", False)
    )
    webuis_ok = all(w["ok"] for w in webui_targets.values())
    boundary_ok = rea_exists

    overall_ok = runtimes_ok and harnesses_ok and webuis_ok and boundary_ok

    return {
        "ok": overall_ok,
        "runtimes": runtimes,
        "cli_harnesses": cli_harnesses,
        "webui_targets": webui_targets,
        "protected_rea_root": protected_rea_root,
    }


def print_doctor_report(report: Dict[str, Any]) -> None:
    """Print clean ASCII diagnostic report to console."""
    print("=" * 70)
    print(" WEBUI CONTROL AI - PREFLIGHT DOCTOR REPORT")
    print("=" * 70)

    print("\n[1] System Runtimes:")
    for name, r in report["runtimes"].items():
        status = "[PASS]" if r["ok"] else "[FAIL]"
        ver = f"({r['version']})" if r.get("version") else ""
        err = f" - Error: {r['error']}" if r.get("error") else ""
        print(f"  {status} {name:<10}: {ver}{err}")

    print("\n[2] CLI Coding Harnesses:")
    for name, h in report["cli_harnesses"].items():
        if h.get("optional"):
            status = "[OPTIONAL]" if not h["available"] else "[PASS]"
        else:
            status = "[PASS]" if h["ok"] else "[FAIL]"
        ver = f"({h['version']})" if h.get("version") else "(not available)"
        print(f"  {status} {name:<12}: {ver}")

    print("\n[3] WebUI Provider Targets (5 Configured):")
    for name, w in report["webui_targets"].items():
        status = "[PASS]" if w["ok"] else "[FAIL]"
        url = w.get("url") or w.get("error", "")
        print(f"  {status} {name:<10}: {url}")

    print("\n[4] Protected Boundary Integrity:")
    rea = report["protected_rea_root"]
    rea_status = "[PASS]" if rea["exists"] else "[FAIL]"
    print(f"  {rea_status} REA Root: {rea['path']} [{rea['status']}]")
    print(f"  [INFO] Serena MCP Port: {rea['serena_mcp_port']} (Preserved)")

    print("\n" + "=" * 70)
    if report["ok"]:
        print(" OVERALL STATUS: ALL PREFLIGHT CHECKS PASSED [OK]")
    else:
        print(" OVERALL STATUS: ONE OR MORE CHECKS FAILED [ACTION REQUIRED]")
    print("=" * 70)


def main() -> int:
    """CLI entrypoint for doctor."""
    report = run_doctor()
    print_doctor_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
