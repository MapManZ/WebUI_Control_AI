"""Unit and integration tests for Semi-Automated Installer, 1-Click Launchers, and Hydrogen Model Integration."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from src.cli.installer import run_semi_auto_setup
from src.webui.dashboard_server import DashboardServer
from src.workers.base import STATUS_BLOCKED_TOOL_UNAVAILABLE, STATUS_COMPLETED
from src.workers.cli_harness_worker import (
    ARGON_BLOCK_MESSAGE,
    CLIHarnessWorker,
    load_model_policy,
    load_supported_models,
    validate_model_policy,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestModelTierAndArgonPolicy(unittest.TestCase):
    """Verify supported_models, model_policy in config/harnesses.json, and CLIHarnessWorker enforcement."""

    def test_harnesses_json_model_policy_and_supported_models(self) -> None:
        cfg_path = REPO_ROOT / "config" / "harnesses.json"
        data = json.loads(cfg_path.read_text(encoding="utf-8"))

        self.assertIn("supported_models", data)
        self.assertIn("model_policy", data)

        models = data["supported_models"]
        for model_id in ("gemini-3.8-flash", "gemini-4-hydrogen", "claude-sonnet-5.5", "gemini-4-argon"):
            self.assertIn(model_id, models)

        self.assertTrue(models["gemini-3.8-flash"]["allow_worker_execution"])
        self.assertTrue(models["gemini-4-hydrogen"]["allow_worker_execution"])
        self.assertTrue(models["claude-sonnet-5.5"]["allow_worker_execution"])
        self.assertFalse(models["gemini-4-argon"]["allow_worker_execution"])
        self.assertEqual(models["gemini-4-argon"]["role"], "reviewer_merge_only")

        policy = load_model_policy(cfg_path)
        self.assertEqual(policy["default_worker_model"], "gemini-3.8-flash")
        self.assertIn("gemini-4-hydrogen", policy["allowed_worker_models"])
        self.assertIn("gemini-4-argon", policy["reviewer_merge_only_models"])
        self.assertIn("gemini-4-hydrogen", load_supported_models(cfg_path))

    def test_worker_models_allowed_and_argon_blocked_for_worker(self) -> None:
        for allowed_model in ("gemini-3.8-flash", "gemini-4-hydrogen", "claude-sonnet-5.5"):
            ok, resolved, err = validate_model_policy(allowed_model, role="worker")
            self.assertTrue(ok, f"Expected {allowed_model} to be allowed for worker, got {err}")
            self.assertEqual(resolved, allowed_model)
            self.assertIsNone(err)

        ok, resolved, err = validate_model_policy("gemini-4-argon", role="worker")
        self.assertFalse(ok)
        self.assertEqual(resolved, "gemini-4-argon")
        self.assertEqual(err, ARGON_BLOCK_MESSAGE)

    def test_argon_allowed_only_for_reviewer_or_merge(self) -> None:
        for role in ("reviewer", "merge"):
            ok, resolved, err = validate_model_policy("gemini-4-argon", role=role)
            self.assertTrue(ok, f"Expected Argon to be allowed for role={role}, got {err}")
            self.assertEqual(resolved, "gemini-4-argon")
            self.assertIsNone(err)

    def test_cli_harness_worker_blocks_argon_for_coding_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = CLIHarnessWorker(harness_name="opencode")
            res = worker.start(
                {"job_id": "job-argon-block", "goal": "Implement feature"},
                tmp,
                "att-argon-worker",
                model="gemini-4-argon",
            )
            self.assertEqual(res.status, STATUS_BLOCKED_TOOL_UNAVAILABLE)
            self.assertNotEqual(res.exit_code, 0)
            self.assertIn("Policy block: Gemini 4 Argon is reserved for Review and Merge only", res.stderr)
            self.assertTrue(res.metadata.get("policy_blocked"))

    def test_cli_harness_worker_allows_hydrogen_and_argon_review(self) -> None:
        class _FakeProc:
            pid = 99999
            returncode = 0

            def communicate(self, timeout=None):
                return ("ok", "")

        with tempfile.TemporaryDirectory() as tmp:
            worker = CLIHarnessWorker(harness_name="opencode")
            with (
                patch("src.workers.cli_harness_worker.probe_harness", return_value={"available": True, "executable": "opencode"}),
                patch("src.workers.cli_harness_worker.subprocess.Popen", return_value=_FakeProc()),
            ):
                res_h = worker.start(
                    {"job_id": "job-hydrogen-ok", "goal": "Implement module"},
                    tmp,
                    "att-h",
                    model="gemini-4-hydrogen",
                )
                self.assertEqual(res_h.status, STATUS_COMPLETED)
                self.assertEqual(res_h.metadata.get("model"), "gemini-4-hydrogen")

                # Untrusted contract role="reviewer" cannot bypass worker role enforcement (Finding 3.9)
                res_untrusted = worker.start(
                    {"job_id": "job-argon-untrusted", "goal": "Review diff", "role": "reviewer"},
                    tmp,
                    "att-r-untrusted",
                    model="gemini-4-argon",
                )
                self.assertEqual(res_untrusted.status, "BLOCKED_TOOL_UNAVAILABLE")

                res_r = worker.start(
                    {"job_id": "job-argon-review", "goal": "Review diff", "role": "reviewer"},
                    tmp,
                    "att-r",
                    model="gemini-4-argon",
                    _trusted_reviewer_adapter=True,
                )
                self.assertEqual(res_r.status, STATUS_COMPLETED)
                self.assertEqual(res_r.metadata.get("model"), "gemini-4-argon")
                self.assertEqual(res_r.metadata.get("role"), "reviewer")


class TestDashboardModelSupport(unittest.TestCase):
    """Verify /api/health exposes Hydrogen and UI includes model selector + badges."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.server = DashboardServer(port=0, storage_dir=Path(self.temp_dir.name))
        self.server.start_background()
        self.base_url = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.temp_dir.cleanup()

    def test_api_health_includes_hydrogen_and_argon_review_only(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/api/health", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertIn("models", data)
        self.assertIn("gemini-3.8-flash", data["models"])
        self.assertIn("gemini-4-hydrogen", data["models"])
        self.assertIn("claude-sonnet-5.5", data["models"])
        self.assertIn("gemini-4-argon (review-only)", data["models"])

    def test_dashboard_html_has_model_selector_and_badges(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/dashboard.html", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn('id="model-tier"', html)
        self.assertIn("Gemini 3.8 Flash [Default Worker]", html)
        self.assertIn("Gemini 4 Hydrogen [Worker]", html)
        self.assertIn("Claude Sonnet 5.5 [Hard Tasks Only]", html)
        self.assertIn("Gemini 4 Argon [Review &amp; Merge Only]", html)


class TestSemiAutomatedInstallerAndLaunchers(unittest.TestCase):
    """Verify run_semi_auto_setup, INSTALL_SETUP.cmd --yes, and START_DASHBOARD.cmd."""

    def test_run_semi_auto_setup_isolated_preserves_existing_browser_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            (tmp_root / "config").mkdir(parents=True)
            shutil.copytree(REPO_ROOT / "bin", tmp_root / "bin")
            shutil.copy2(
                REPO_ROOT / "config" / "browser_targets.example.json",
                tmp_root / "config" / "browser_targets.example.json",
            )
            shutil.copy2(
                REPO_ROOT / "config" / "harnesses.json",
                tmp_root / "config" / "harnesses.json",
            )

            res = run_semi_auto_setup(repo_root=tmp_root, auto_yes=True)
            self.assertTrue(res["ok"], f"Setup failed: {res}")
            self.assertTrue((tmp_root / ".runtime" / "state" / "orchestra.db").is_file())
            self.assertTrue((tmp_root / "config" / "browser_targets.json").is_file())

            mcp_dir = tmp_root / ".runtime" / "mcp_configs"
            for name in ("antigravity_mcp.json", "opencode_mcp.json", "hermes_mcp.json", "claude_desktop_mcp.json"):
                cfg_file = mcp_dir / name
                self.assertTrue(cfg_file.is_file(), f"Missing MCP config: {cfg_file}")
                content = cfg_file.read_text(encoding="utf-8")
                self.assertIn("orchestra_mcp.py", content)

            # Verify existing user edits in config/browser_targets.json are preserved
            custom_targets = tmp_root / "config" / "browser_targets.json"
            custom_targets.write_text('{"custom_user_edit": true}', encoding="utf-8")
            res2 = run_semi_auto_setup(repo_root=tmp_root, auto_yes=True)
            self.assertTrue(res2["ok"])
            self.assertEqual(custom_targets.read_text(encoding="utf-8"), '{"custom_user_edit": true}')

    def test_install_setup_cmd_yes_exits_zero_and_creates_artifacts(self) -> None:
        proc = subprocess.run(
            ["cmd", "/c", "INSTALL_SETUP.cmd", "--yes"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
        self.assertEqual(proc.returncode, 0, f"INSTALL_SETUP.cmd failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("[OK]", proc.stdout)

        self.assertTrue((REPO_ROOT / "config" / "browser_targets.json").is_file())
        self.assertTrue((REPO_ROOT / ".runtime" / "state" / "orchestra.db").is_file())

        expected_mcp = [
            "antigravity_mcp.json",
            "opencode_mcp.json",
            "hermes_mcp.json",
            "claude_desktop_mcp.json",
        ]
        expected_mcp_path = str((REPO_ROOT / "src" / "mcp" / "orchestra_mcp.py").resolve())
        for fname in expected_mcp:
            fpath = REPO_ROOT / ".runtime" / "mcp_configs" / fname
            self.assertTrue(fpath.is_file(), f"Missing {fpath}")
            parsed = json.loads(fpath.read_text(encoding="utf-8"))
            self.assertIn(expected_mcp_path, json.dumps(parsed).replace("\\\\", "\\"))

    def test_start_dashboard_cmd_smoke_no_browser(self) -> None:
        proc = subprocess.run(
            ["cmd", "/c", "START_DASHBOARD.cmd", "--no-browser", "--smoke"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, f"START_DASHBOARD.cmd failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("gemini-4-hydrogen", proc.stdout)


if __name__ == "__main__":
    unittest.main()
