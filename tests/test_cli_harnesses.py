"""Comprehensive unit and integration tests for Local CLI Harnesses and MockWorker."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from src.workers.base import (
    STATUS_BLOCKED_TOOL_UNAVAILABLE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    WorkerResult,
)
from src.workers.cli_harness_worker import (
    CLIHarnessWorker,
    DEFAULT_HARNESSES,
    load_harness_config,
    probe_harness,
)
from src.workers.mock_worker import MockWorker


class TestMockWorkerModes(unittest.TestCase):
    """Test all deterministic modes of MockWorker used in testing and fault injection."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_mock_pass_mode(self) -> None:
        worker = MockWorker(mode="pass")
        contract = {
            "job_id": "test-pass",
            "allowed_write_paths": ["result.txt"],
        }
        res = worker.start(contract, str(self.sandbox), "att-1")
        self.assertEqual(res.status, STATUS_COMPLETED)
        self.assertEqual(res.exit_code, 0)
        self.assertIn("result.txt", res.changed_files)
        self.assertTrue((self.sandbox / "result.txt").is_file())

    def test_mock_fail_mode(self) -> None:
        worker = MockWorker(mode="fail")
        contract = {
            "job_id": "test-fail",
            "allowed_write_paths": ["error.log"],
        }
        res = worker.start(contract, str(self.sandbox), "att-2")
        self.assertEqual(res.status, STATUS_FAILED)
        self.assertEqual(res.exit_code, 1)

    def test_mock_fake_pass_with_bad_code(self) -> None:
        worker = MockWorker(mode="fake_pass_with_bad_code")
        contract = {
            "job_id": "test-fake-pass",
            "allowed_write_paths": ["solution.py"],
        }
        res = worker.start(contract, str(self.sandbox), "att-3")
        self.assertEqual(res.status, STATUS_COMPLETED)
        self.assertEqual(res.exit_code, 0)
        content = (self.sandbox / "solution.py").read_text(encoding="utf-8")
        self.assertIn("Broken implementation", content)
        self.assertTrue(res.metadata.get("self_certified"))

    def test_mock_scope_violation(self) -> None:
        worker = MockWorker(mode="scope_violation")
        contract = {
            "job_id": "test-scope-violation",
            "allowed_write_paths": ["allowed.txt"],
        }
        res = worker.start(contract, str(self.sandbox), "att-4")
        self.assertIn("unauthorized_scope_probe.txt", res.changed_files)
        self.assertNotIn("allowed.txt", res.changed_files)

    def test_mock_leak_secret(self) -> None:
        worker = MockWorker(mode="leak_secret")
        contract = {"job_id": "test-leak"}
        res = worker.start(contract, str(self.sandbox), "att-5")
        self.assertIn("sk-secret-1234567890abcdef", res.stdout)

    def test_mock_timeout(self) -> None:
        worker = MockWorker(mode="timeout")
        contract = {"job_id": "test-timeout"}
        res = worker.start(contract, str(self.sandbox), "att-6")
        self.assertEqual(res.status, STATUS_TIMEOUT)
        self.assertEqual(res.exit_code, 124)

    def test_mock_cancellation(self) -> None:
        worker = MockWorker()
        worker.cancel("att-cancel-1")
        contract = {"job_id": "test-cancel"}
        res = worker.start(contract, str(self.sandbox), "att-cancel-1")
        self.assertEqual(res.status, STATUS_CANCELLED)


class TestCLIHarnessProbes(unittest.TestCase):
    """Test live and simulated probes for all 4 local CLIs plus optional codex."""

    def test_harness_config_loading(self) -> None:
        configs = load_harness_config()
        self.assertIn("antigravity", configs)
        self.assertIn("opencode", configs)
        self.assertIn("hermes", configs)
        self.assertIn("dsh", configs)
        self.assertIn("codex", configs)

    def test_live_antigravity_probe(self) -> None:
        probe = probe_harness("antigravity")
        wrapper = Path(__file__).resolve().parents[1] / "bin" / "agy-cli.cmd"
        if wrapper.is_file() and probe["available"]:
            self.assertTrue(probe["available"], f"Antigravity probe failed: {probe}")
            self.assertTrue(len(probe["version"]) > 0)

    def test_live_opencode_probe(self) -> None:
        probe = probe_harness("opencode")
        wrapper = Path(__file__).resolve().parents[1] / "bin" / "opencode-cli.cmd"
        if wrapper.is_file() and probe["available"]:
            self.assertTrue(probe["available"], f"OpenCode probe failed: {probe}")
            self.assertIn("opencode", probe["version"].lower())

    def test_live_hermes_probe(self) -> None:
        probe = probe_harness("hermes")
        wrapper = Path(__file__).resolve().parents[1] / "bin" / "hermes-cli.cmd"
        if wrapper.is_file() and probe["available"]:
            self.assertTrue(probe["available"], f"Hermes probe failed: {probe}")
            self.assertIn("hermes", probe["version"].lower())

    def test_live_dsh_probe(self) -> None:
        probe = probe_harness("dsh")
        wrapper = Path(__file__).resolve().parents[1] / "bin" / "dsh-cli.cmd"
        if wrapper.is_file() and probe["available"]:
            self.assertTrue(probe["available"], f"DSH probe failed: {probe}")

    def test_optional_codex_probe(self) -> None:
        probe = probe_harness("codex")
        # Codex is optional: if not in PATH, available must be False and optional must be True
        if not shutil.which("codex"):
            self.assertFalse(probe["available"])
            self.assertTrue(probe["optional"])

    def test_unavailable_tool_returns_blocked(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        try:
            worker = CLIHarnessWorker(harness_name="codex")
            contract = {"job_id": "test-missing-codex", "goal": "Write code"}
            # Unless codex happens to be installed in PATH
            if not shutil.which("codex"):
                res = worker.start(contract, temp_dir.name, "att-codex")
                self.assertEqual(res.status, STATUS_BLOCKED_TOOL_UNAVAILABLE)
                self.assertEqual(res.exit_code, 127)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
