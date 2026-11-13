"""Unit tests for the CLI doctor and preflight diagnostics."""

import os
import unittest
from pathlib import Path

from src.cli.doctor import run_doctor, check_runtime, _run_cmd


class TestDoctor(unittest.TestCase):
    """Test suite for system diagnostics, runtimes, harnesses, and boundaries."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parent.parent

    def test_run_cmd_success(self):
        ok, out = _run_cmd("python --version")
        self.assertTrue(ok)
        self.assertIn("Python", out)

    def test_check_runtime_python(self):
        info = check_runtime("python")
        self.assertTrue(info["ok"])
        self.assertIsNotNone(info["path"])
        self.assertIn("Python 3.14", info["version"])

    def test_check_runtime_missing(self):
        info = check_runtime("non_existent_executable_12345")
        self.assertFalse(info["ok"])
        self.assertIsNone(info["path"])
        self.assertIn("Executable not found", info["error"])

    def test_run_doctor_full_report(self):
        report = run_doctor(self.repo_root)
        self.assertIsInstance(report, dict)
        self.assertTrue(report["ok"])

        # Check runtimes section
        self.assertIn("runtimes", report)
        runtimes = report["runtimes"]
        for key in ["python", "node", "bun", "git", "moli", "uv"]:
            self.assertIn(key, runtimes)
            self.assertTrue(runtimes[key]["ok"], f"Runtime {key} check failed: {runtimes[key]}")

        # Check CLI harnesses section
        self.assertIn("cli_harnesses", report)
        harnesses = report["cli_harnesses"]
        for key in ["antigravity", "opencode", "hermes", "dsh"]:
            self.assertIn(key, harnesses)
            self.assertTrue(harnesses[key]["ok"], f"Harness {key} check failed: {harnesses[key]}")
            self.assertTrue(harnesses[key]["available"])

        # Check WebUI targets section (5 expected)
        self.assertIn("webui_targets", report)
        targets = report["webui_targets"]
        for provider in ["chatgpt", "claude", "gemini", "glm", "deepseek"]:
            self.assertIn(provider, targets)
            self.assertTrue(targets[provider]["ok"], f"Provider {provider} check failed")
            self.assertTrue(targets[provider]["url"].startswith("http"))

        # Check protected boundary
        self.assertIn("protected_rea_root", report)
        rea = report["protected_rea_root"]
        self.assertTrue(rea["exists"])
        self.assertEqual(rea["status"], "PROTECTED_READ_ONLY")
        self.assertEqual(rea["serena_mcp_port"], 9121)


if __name__ == "__main__":
    unittest.main()
