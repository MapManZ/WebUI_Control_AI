"""Semi-automated setup wizard for WebUI Control AI (Thai + English beginner-friendly)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.cli.doctor import run_doctor
from src.state.db import StateDB

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_RUNTIME_DIRS = (
    "state",
    "sandboxes",
    "webui_packets",
    "evidence",
    "mcp_configs",
    "webui_hub",
    "handoffs",
    "keys",
    "pilot",
)
CLI_WRAPPERS = (
    ("dsh", "dsh-cli.cmd"),
    ("opencode", "opencode-cli.cmd"),
    ("hermes", "hermes-cli.cmd"),
    ("antigravity", "agy-cli.cmd"),
)


def _configure_stdio_utf8() -> None:
    """Ensure stdout/stderr can safely render Thai + ASCII summary output on Windows."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _safe_print(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def _confirm_step(step_num: int, title_th: str, title_en: str, auto_yes: bool) -> bool:
    """Prompt beginner interactively with [Enter = ดำเนินการต่อ / Y], defaulting to Y on EOF."""
    if auto_yes:
        _safe_print(f"\n[Step {step_num}/5] {title_en} ({title_th}) -> [AUTO-YES]")
        return True

    if sys.stdin is None or not sys.stdin.isatty():
        _safe_print(f"\n[Step {step_num}/5] {title_en} ({title_th}) -> [NON-INTERACTIVE: Y]")
        return True

    _safe_print(f"\n[Step {step_num}/5] {title_en} ({title_th})")
    try:
        answer = input("  กด [Enter = ดำเนินการต่อ / Y] หรือพิมพ์ N เพื่อข้าม: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        _safe_print("  (stdin closed -> defaulting to Y)")
        return True

    return answer not in ("n", "no")


def _resolve_mcp_script(root: Path) -> str:
    candidate = (root / "src" / "mcp" / "orchestra_mcp.py").resolve()
    if candidate.is_file():
        return str(candidate)
    return str((DEFAULT_REPO_ROOT / "src" / "mcp" / "orchestra_mcp.py").resolve())


def _resolve_wrapper(root: Path, wrapper_name: str) -> Path:
    candidate = root / "bin" / wrapper_name
    if candidate.is_file():
        return candidate
    return DEFAULT_REPO_ROOT / "bin" / wrapper_name


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _build_mcp_configs(root: Path, mcp_script_path: str) -> Dict[str, Dict[str, Any]]:
    python_exe = sys.executable or "python"
    repo_str = str(root.resolve())
    standard_entry = {
        "command": python_exe,
        "args": [mcp_script_path],
        "env": {
            "PYTHONPATH": repo_str,
            "PYTHONUTF8": "1",
        },
    }
    return {
        "antigravity_mcp.json": {
            "mcpServers": {
                "orchestra": standard_entry,
            }
        },
        "claude_desktop_mcp.json": {
            "mcpServers": {
                "orchestra": standard_entry,
            }
        },
        "opencode_mcp.json": {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "orchestra": {
                    "type": "local",
                    "command": [python_exe, mcp_script_path],
                    "environment": {
                        "PYTHONPATH": repo_str,
                        "PYTHONUTF8": "1",
                    },
                    "enabled": True,
                }
            },
        },
        "hermes_mcp.json": {
            "mcp_servers": {
                "orchestra": standard_entry,
            }
        },
    }


def _run_wrapper_version(wrapper_path: Path) -> Dict[str, Any]:
    if not wrapper_path.is_file():
        return {
            "ok": False,
            "wrapper": str(wrapper_path),
            "version": None,
            "error": f"Wrapper not found: {wrapper_path}",
        }
    try:
        res = subprocess.run(
            f'cmd /c "{wrapper_path}" --version',
            capture_output=True,
            text=True,
            shell=True,
            timeout=15,
        )
        output = (res.stdout or res.stderr or "").strip()
        first_line = output.splitlines()[0] if output else ""
        return {
            "ok": res.returncode == 0,
            "wrapper": str(wrapper_path),
            "version": first_line if res.returncode == 0 else None,
            "error": None if res.returncode == 0 else (output or f"Exit code {res.returncode}"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "wrapper": str(wrapper_path),
            "version": None,
            "error": str(exc),
        }


def _print_summary_table(steps: List[Dict[str, Any]], overall_ok: bool, root: Path) -> None:
    _safe_print("\n" + "=" * 78)
    _safe_print(" WEBUI CONTROL AI - SETUP SUMMARY / สรุปผลการติดตั้งระบบ")
    _safe_print("=" * 78)
    _safe_print(f" Workspace: {root}")
    _safe_print("-" * 78)
    _safe_print(f" {'Step':<6} {'Status':<10} {'Title (TH / EN)':<38} {'Detail'}")
    _safe_print("-" * 78)
    for step in steps:
        tag = "[OK]" if step["ok"] else "[WARN]"
        if step.get("skipped"):
            tag = "[SKIP]"
        title = f"{step['name_en']} / {step['name_th']}"
        if len(title) > 36:
            title = title[:33] + "..."
        _safe_print(f" {step['step']:<6} {tag:<10} {title:<38} {step['summary']}")
    _safe_print("-" * 78)
    if overall_ok:
        _safe_print(" RESULT: [OK] ระบบพร้อมใช้งานแล้ว! / All setup steps completed successfully.")
        _safe_print(" Next / ขั้นตอนถัดไป:")
        _safe_print("   1) ดับเบิลคลิก START_DASHBOARD.cmd เพื่อเปิดหน้าเว็บ Dashboard (http://127.0.0.1:8765)")
        _safe_print("   2) หรือรัน .\\bin\\orchestra.cmd doctor เพื่อตรวจเช็กสถานะอีกครั้ง")
    else:
        _safe_print(" RESULT: [ACTION REQUIRED] พบรายการที่ต้องตรวจสอบ / Some checks need attention.")
    _safe_print("=" * 78)


def run_semi_auto_setup(repo_root: Optional[Path | str] = None, auto_yes: bool = False) -> Dict[str, Any]:
    """Run the 5-step semi-automated setup wizard for WebUI Control AI."""
    _configure_stdio_utf8()
    root = Path(repo_root).resolve() if repo_root else DEFAULT_REPO_ROOT
    runtime_dir = root / ".runtime"

    _safe_print("=" * 78)
    _safe_print(" WEBUI CONTROL AI - SEMI-AUTOMATED SETUP WIZARD (ติดตั้งกึ่งอัตโนมัติ)")
    _safe_print(" Models: Gemini 3.8 Flash | Gemini 4 Hydrogen | Claude Sonnet 5.5 | Argon (Review)")
    _safe_print("=" * 78)

    steps: List[Dict[str, Any]] = []
    doctor_report: Dict[str, Any] = {}
    db_path = runtime_dir / "state" / "orchestra.db"
    browser_targets_path = root / "config" / "browser_targets.json"
    mcp_configs_dir = runtime_dir / "mcp_configs"
    generated_mcp_files: Dict[str, str] = {}
    self_test_report: Dict[str, Any] = {}

    # Step 1: Preflight Doctor
    if _confirm_step(1, "ตรวจเช็กโปรแกรมและระบบเบื้องต้น", "Preflight Doctor", auto_yes):
        doctor_report = run_doctor(root)
        doc_ok = bool(doctor_report.get("ok"))
        cli_ok_count = sum(
            1 for k in ("antigravity", "opencode", "hermes", "dsh")
            if doctor_report.get("cli_harnesses", {}).get(k, {}).get("ok")
        )
        webui_ok_count = sum(
            1 for v in doctor_report.get("webui_targets", {}).values() if v.get("ok")
        )
        steps.append({
            "step": "1/5",
            "name_en": "Preflight Doctor",
            "name_th": "เช็กความพร้อม",
            "ok": doc_ok,
            "skipped": False,
            "summary": f"CLIs {cli_ok_count}/4, WebUIs {webui_ok_count}/5, doctor_ok={doc_ok}",
        })
    else:
        steps.append({
            "step": "1/5",
            "name_en": "Preflight Doctor",
            "name_th": "เช็กความพร้อม",
            "ok": True,
            "skipped": True,
            "summary": "Skipped by user",
        })

    # Step 2: Initialize SQLite WAL DB & runtime directories
    if _confirm_step(2, "สร้างฐานข้อมูล SQLite WAL และโฟลเดอร์ทำงาน", "Init SQLite WAL DB & Runtime Dirs", auto_yes):
        for sub in REQUIRED_RUNTIME_DIRS:
            (runtime_dir / sub).mkdir(parents=True, exist_ok=True)
        db = StateDB(db_path)
        try:
            journal_mode = str(db.pragma("journal_mode") or "").lower()
            tables = db.table_names() if hasattr(db, "table_names") else [
                r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")
            ]
        finally:
            db.close()
        step2_ok = db_path.is_file() and journal_mode == "wal" and len(tables) >= 6
        steps.append({
            "step": "2/5",
            "name_en": "SQLite WAL + Dirs",
            "name_th": "ฐานข้อมูล",
            "ok": step2_ok,
            "skipped": False,
            "summary": f"db={db_path.name}, mode={journal_mode}, tables={len(tables)}",
        })
    else:
        steps.append({
            "step": "2/5",
            "name_en": "SQLite WAL + Dirs",
            "name_th": "ฐานข้อมูล",
            "ok": True,
            "skipped": True,
            "summary": "Skipped by user",
        })

    # Step 3: Copy browser_targets.example.json -> browser_targets.json if missing
    if _confirm_step(3, "สร้างไฟล์ตั้งค่า 5 WebUI (คงค่าเดิมไว้ถ้ามีอยู่แล้ว)", "Configure 5 WebUI Targets", auto_yes):
        example_path = root / "config" / "browser_targets.example.json"
        if not example_path.is_file():
            example_path = DEFAULT_REPO_ROOT / "config" / "browser_targets.example.json"
        if browser_targets_path.is_file():
            action = "preserved existing config/browser_targets.json"
            step3_ok = True
        elif example_path.is_file():
            browser_targets_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(example_path, browser_targets_path)
            action = "created config/browser_targets.json from example"
            step3_ok = browser_targets_path.is_file()
        else:
            action = f"missing template {example_path}"
            step3_ok = False
        steps.append({
            "step": "3/5",
            "name_en": "WebUI Config",
            "name_th": "ตั้งค่า WebUI",
            "ok": step3_ok,
            "skipped": False,
            "summary": action,
        })
    else:
        steps.append({
            "step": "3/5",
            "name_en": "WebUI Config",
            "name_th": "ตั้งค่า WebUI",
            "ok": True,
            "skipped": True,
            "summary": "Skipped by user",
        })

    # Step 4: Generate MCP configs under .runtime/mcp_configs/
    if _confirm_step(4, "สร้างไฟล์เชื่อมต่อ MCP สำหรับ Antigravity/OpenCode/Hermes/Claude", "Generate MCP Configs", auto_yes):
        mcp_script = _resolve_mcp_script(root)
        configs = _build_mcp_configs(root, mcp_script)
        mcp_configs_dir.mkdir(parents=True, exist_ok=True)
        for filename, payload in configs.items():
            out_file = mcp_configs_dir / filename
            _write_json(out_file, payload)
            generated_mcp_files[filename] = str(out_file)
        step4_ok = len(generated_mcp_files) == 4 and all(Path(p).is_file() for p in generated_mcp_files.values())
        steps.append({
            "step": "4/5",
            "name_en": "MCP Config Files",
            "name_th": "ไฟล์ MCP",
            "ok": step4_ok,
            "skipped": False,
            "summary": f"Generated {len(generated_mcp_files)} configs -> {mcp_script}",
        })
    else:
        steps.append({
            "step": "4/5",
            "name_en": "MCP Config Files",
            "name_th": "ไฟล์ MCP",
            "ok": True,
            "skipped": True,
            "summary": "Skipped by user",
        })

    # Step 5: Self-test 4 CLI wrappers + DB read/write
    if _confirm_step(5, "ทดสอบคำสั่ง 4 CLI และอ่าน-เขียนฐานข้อมูลจริง", "Self-Test CLIs & DB", auto_yes):
        cli_results: Dict[str, Any] = {}
        for name, wrapper in CLI_WRAPPERS:
            cli_results[name] = _run_wrapper_version(_resolve_wrapper(root, wrapper))

        db_rw_ok = False
        db_error: Optional[str] = None
        try:
            db = StateDB(db_path)
            try:
                db.append_audit(
                    event_type="SETUP_SELF_TEST",
                    payload={"repo_root": str(root), "status": "ok"},
                )
                row = db.query_one(
                    "SELECT COUNT(*) AS cnt FROM audit_events WHERE event_type = ?",
                    ("SETUP_SELF_TEST",),
                )
                db_rw_ok = bool(row and int(row["cnt"]) >= 1)
            finally:
                db.close()
        except Exception as exc:
            db_error = str(exc)

        clis_passed = sum(1 for v in cli_results.values() if v.get("ok"))
        step5_ok = (clis_passed == len(CLI_WRAPPERS)) and db_rw_ok
        self_test_report = {
            "ok": step5_ok,
            "clis": cli_results,
            "db_read_write_ok": db_rw_ok,
            "db_error": db_error,
        }
        steps.append({
            "step": "5/5",
            "name_en": "Self-Test",
            "name_th": "ทดสอบระบบ",
            "ok": step5_ok,
            "skipped": False,
            "summary": f"CLI wrappers {clis_passed}/4 passed, DB R/W={'OK' if db_rw_ok else 'FAIL'}",
        })
    else:
        steps.append({
            "step": "5/5",
            "name_en": "Self-Test",
            "name_th": "ทดสอบระบบ",
            "ok": True,
            "skipped": True,
            "summary": "Skipped by user",
        })

    overall_ok = all(s["ok"] for s in steps)
    _print_summary_table(steps, overall_ok, root)

    return {
        "ok": overall_ok,
        "repo_root": str(root),
        "auto_yes": auto_yes,
        "steps": steps,
        "db_path": str(db_path),
        "browser_targets_path": str(browser_targets_path),
        "mcp_configs_dir": str(mcp_configs_dir),
        "mcp_configs": generated_mcp_files,
        "self_test": self_test_report,
        "doctor": doctor_report,
    }
