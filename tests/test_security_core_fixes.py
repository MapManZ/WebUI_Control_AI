from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.contracts.validator import is_safe_relative_path
from src.handoff.generator import generate_handoff
from src.mcp.orchestra_mcp import REPO_ROOT, OrchestraMCPServer
from src.orchestra.supervisor import BatchGateError, ContractRejected, Supervisor
from src.qa.runner import command_allowed
from src.sandbox.path_guard import ForbiddenPathError, PathPolicy, PathViolation
from src.sandbox.workdir_guard import get_protected_roots, validate_sandbox_dir
from src.workers.cli_harness_worker import (
    CLIHarnessWorker,
    _resolve_direct_executable,
    _validate_safe_goal,
)
from src.workers.mock_worker import MockWorker


class TestSecurityCoreFixes(unittest.TestCase):
    """Regression suite for Phase 1-4 core/sandbox/contracts/QA/CLI/MCP security fixes."""

    def test_3_1_and_3_2_mcp_job_id_and_sandbox_validation_and_timeout_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as td_hub:
            srv = OrchestraMCPServer(hub_store_dir=Path(td_hub) / "hub")

            for bad_id in ("../evil", "a/b", "a\\b", "bad id", "x" * 65, ""):
                resp = srv.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "orchestra_run_job",
                            "arguments": {
                                "job_id": bad_id,
                                "worker_type": "mock",
                                "goal": "Generate sample",
                            },
                        },
                    }
                )
                self.assertTrue(resp["result"]["isError"], f"Expected error for job_id={bad_id!r}")

                resp_pkt = srv.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 11,
                        "method": "tools/call",
                        "params": {
                            "name": "orchestra_create_webui_packet",
                            "arguments": {
                                "job_id": bad_id,
                                "case_id": "CASE-1",
                                "provider": "chatgpt",
                                "scenario": "Test scenario",
                            },
                        },
                    }
                )
                self.assertTrue(resp_pkt["result"]["isError"], f"Expected packet error for job_id={bad_id!r}")

                resp_sub = srv.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 12,
                        "method": "tools/call",
                        "params": {
                            "name": "orchestra_submit_webui_response",
                            "arguments": {
                                "job_id": bad_id,
                                "response": {"status": "COMPLETED"},
                            },
                        },
                    }
                )
                self.assertTrue(resp_sub["result"]["isError"], f"Expected submit error for job_id={bad_id!r}")

            for bad_sb in (
                str(REPO_ROOT),
                str(REPO_ROOT.parent),
                str(Path(tempfile.gettempdir()) / ".." / "escape"),
                str(REPO_ROOT / "src"),
            ):
                resp = srv.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "orchestra_run_job",
                            "arguments": {
                                "job_id": "safe-id-1",
                                "worker_type": "mock",
                                "goal": "Generate sample",
                                "sandbox_dir": bad_sb,
                            },
                        },
                    }
                )
                self.assertTrue(resp["result"]["isError"], f"Expected error for sandbox_dir={bad_sb!r}")

            # 3.15 timeout clamping (0 -> 1, 99999 -> 3600) + Supervisor governance
            sb = Path(td_hub) / "sb_ok"
            sb.mkdir()
            resp_hi = srv.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "orchestra_run_job",
                        "arguments": {
                            "job_id": "governed-job-1",
                            "worker_type": "mock",
                            "mock_behavior": "pass",
                            "goal": "Write solution",
                            "sandbox_dir": str(sb),
                            "timeout_seconds": 99999,
                        },
                    },
                }
            )
            self.assertFalse(resp_hi["result"]["isError"], resp_hi)
            body = json.loads(resp_hi["result"]["content"][0]["text"])
            self.assertEqual(body["governed_by"], "Supervisor")
            self.assertTrue(body["accepted"])
            self.assertEqual(body["final_state"], "ACCEPTED")

            resp_lo = srv.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "orchestra_run_job",
                        "arguments": {
                            "job_id": "governed-job-2",
                            "worker_type": "mock",
                            "mock_behavior": "pass",
                            "goal": "Write solution",
                            "sandbox_dir": str(sb),
                            "timeout_seconds": 0,
                        },
                    },
                }
            )
            self.assertFalse(resp_lo["result"]["isError"], resp_lo)

    def test_3_2_cli_worker_rejects_repo_root_and_traversal_sandbox(self) -> None:
        worker = CLIHarnessWorker("opencode")
        with self.assertRaises(PathViolation):
            worker.start({"job_id": "j-root", "goal": "test"}, str(REPO_ROOT), "att-1")
        with self.assertRaises(PathViolation):
            worker.start({"job_id": "j-src", "goal": "test"}, str(REPO_ROOT / "src"), "att-2")
        with self.assertRaises(PathViolation):
            validate_sandbox_dir(str(Path(tempfile.gettempdir()) / ".." / "bad"), require_sandbox_subroot=True)

    def test_3_4_direct_executable_and_batbadbut_goal_guard(self) -> None:
        # Resolve direct executable prefers non-.cmd/.bat when available on system
        exe_oc = _resolve_direct_executable("opencode")
        if exe_oc is not None:
            self.assertFalse(exe_oc.lower().endswith((".cmd", ".bat")))

        _validate_safe_goal("Implement feature & verify | ok", uses_cmd_wrapper=False)
        for bad in ("a & calc", "a|b", "a>b", "a<b", "%PATH%", "a^b", "a!b", "a(b)", "a`b", "a\rb", "a\nb", 'a"b', "a\x00b"):
            with self.assertRaises(ValueError):
                _validate_safe_goal(bad, uses_cmd_wrapper=True)
        with self.assertRaises(ValueError):
            _validate_safe_goal("bad\x00goal", uses_cmd_wrapper=False)

    def test_3_7_and_3_8_protected_roots_path_policy_and_supervisor_fingerprint_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            prot = Path(td) / "extra_protected"
            prot.mkdir()
            old_val = os.environ.get("ORCHESTRA_PROTECTED_ROOTS")
            os.environ["ORCHESTRA_PROTECTED_ROOTS"] = str(prot)
            try:
                roots = get_protected_roots()
                self.assertTrue(any(str(prot.resolve()).lower() == r.lower() for r in roots))
                with self.assertRaises(ForbiddenPathError):
                    PathPolicy(["**"], [], sandbox_root=prot)
            finally:
                if old_val is None:
                    os.environ.pop("ORCHESTRA_PROTECTED_ROOTS", None)
                else:
                    os.environ["ORCHESTRA_PROTECTED_ROOTS"] = old_val

            sup = Supervisor(Path(td), runtime_root=Path(td) / ".rt")
            try:
                self.assertTrue(sup.protected_content)
            finally:
                sup.close()

    def test_3_9_and_3_15_cli_worker_role_guard_model_env_and_timeout_clamp(self) -> None:
        class _FakeProc:
            pid = 4242
            returncode = 0

            def communicate(self, timeout=None):
                return ("ok", "")

        seen_env: dict[str, str] = {}
        seen_timeouts: list[int] = []

        def fake_popen(cmd, **kw):
            seen_env.update(kw.get("env", {}))
            proc = _FakeProc()

            orig_comm = proc.communicate

            def _comm(timeout=None):
                if timeout is not None:
                    seen_timeouts.append(timeout)
                return orig_comm(timeout=timeout)

            proc.communicate = _comm
            return proc

        worker = CLIHarnessWorker("opencode")
        with tempfile.TemporaryDirectory() as td:
            with (
                patch(
                    "src.workers.cli_harness_worker.probe_harness",
                    return_value={"available": True, "executable": "opencode.exe", "is_cmd_wrapper": False},
                ),
                patch("src.workers.cli_harness_worker.subprocess.Popen", side_effect=fake_popen),
            ):
                # Untrusted role="reviewer" forced to "worker" -> Argon blocked
                r_untrusted = worker.start(
                    {"job_id": "j-untrusted", "goal": "Review diff", "role": "reviewer"},
                    td,
                    "att-u",
                    model="gemini-4-argon",
                )
                self.assertEqual(r_untrusted.status, "BLOCKED_TOOL_UNAVAILABLE")

                # Trusted adapter unlocks reviewer + exports ORCHESTRA_ENFORCED_MODEL + clamps timeout
                r_trusted = worker.start(
                    {"job_id": "j-trusted", "goal": "Review diff", "role": "reviewer", "timeout_seconds": 99999},
                    td,
                    "att-t",
                    model="gemini-4-argon",
                    _trusted_reviewer_adapter=True,
                )
                self.assertEqual(r_trusted.status, "COMPLETED")
                self.assertEqual(r_trusted.metadata.get("role"), "reviewer")
                self.assertEqual(seen_env.get("ORCHESTRA_ENFORCED_MODEL"), "gemini-4-argon")
                self.assertEqual(seen_timeouts[-1], 3600)

    def test_3_10_supervisor_dag_cycle_and_reconstruct_batch_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            sup = Supervisor(ws, runtime_root=ws / ".rt")
            try:
                base = {
                    "schema_version": "1.0.0",
                    "plan_id": "P",
                    "phase_id": "PH",
                    "batch_id": "B-CYC",
                    "project_id": "PR",
                    "workspace_root_ref": ".",
                    "goal": "g",
                    "allowed_write_paths": ["solution.txt", "a.txt"],
                    "forbidden_write_roots": [],
                    "acceptance_checks": ['python -c "pass"'],
                    "timeout_seconds": 30,
                }
                with self.assertRaises(ContractRejected):
                    sup.submit_contract({**base, "job_id": "J-SELF", "batch_ordinal": 1, "dependencies": ["J-SELF"]})

                # Submit valid job in B-GATE, tamper extra active job in same batch to exceed G7, then reconstruct
                c1 = {**base, "job_id": "JG-1", "batch_id": "B-GATE", "batch_ordinal": 1, "dependencies": []}
                sup.submit_contract(c1)
                sup.approve_contract("JG-1", "qa")
                out = sup.run_job_attempt("JG-1", MockWorker("pass"))
                self.assertTrue(out.accepted)
                hinfo = generate_handoff(sup.db, "JG-1", "JG-NEXT", ws / ".rt" / "handoffs", hmac_key=sup.hmac_key)

                # Fresh supervisor reconstructs within gate OK, then fails gate if batch has >2 jobs
                sup2 = Supervisor(ws, runtime_root=ws / ".rt2", hmac_key=sup.hmac_key)
                try:
                    rec = sup2.reconstruct_from_handoff(
                        hinfo["handoff_path"], next_context_path=hinfo["next_context_path"]
                    )
                    self.assertEqual(rec["job_id"], "JG-1")

                    # Simulate DB tampering adding a 3rd job to batch B-GATE to exceed G7 (> 2)
                    sup2.submit_contract({**base, "job_id": "JG-2", "batch_id": "B-GATE", "batch_ordinal": 2, "dependencies": []})
                    with sup2.db.transaction():
                        c3 = {**base, "job_id": "JG-3", "batch_id": "B-GATE", "batch_ordinal": 2, "dependencies": []}
                        sup2.db.execute(
                            "INSERT INTO job_contracts(job_id, batch_id, batch_ordinal, project_id, depends_on_json, contract_json, contract_sha256, approval_state) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                            ("JG-3", "B-GATE", 2, "PR", "[]", json.dumps(c3), "deadbeef", "PENDING"),
                        )
                    with self.assertRaises(BatchGateError):
                        sup2.reconstruct_from_handoff(
                            hinfo["handoff_path"], next_context_path=hinfo["next_context_path"]
                        )
                finally:
                    sup2.close()
            finally:
                sup.close()

    def test_3_11_cli_manifest_diff_and_supervisor_read_and_pycache_scope(self) -> None:
        # CLIHarnessWorker detects modified & deleted files & __pycache__ writes via build_manifest
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            existing = sb / "allowed.txt"
            existing.write_text("v1", encoding="utf-8")

            def _fake_popen(cmd, **kw):
                existing.write_text("v2", encoding="utf-8")
                pc = sb / "__pycache__"
                pc.mkdir(exist_ok=True)
                (pc / "sneaky.pyc").write_bytes(b"123")

                class _P:
                    pid = 555
                    returncode = 0

                    def communicate(self, timeout=None):
                        return ("ok", "")

                return _P()

            worker = CLIHarnessWorker("opencode")
            with (
                patch(
                    "src.workers.cli_harness_worker.probe_harness",
                    return_value={"available": True, "executable": "opencode.exe", "is_cmd_wrapper": False},
                ),
                patch("src.workers.cli_harness_worker.subprocess.Popen", side_effect=_fake_popen),
            ):
                res = worker.start(
                    {"job_id": "j-diff", "goal": "Modify allowed", "allowed_write_paths": ["allowed.txt"]},
                    str(sb),
                    "att-diff",
                )
            self.assertEqual(res.status, "FAILED")
            self.assertIn("allowed.txt", res.changed_files)
            self.assertIn("__pycache__/sneaky.pyc", res.changed_files)

        # Supervisor stages only allowed_read_paths when include is None
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            (ws / "allowed").mkdir(parents=True)
            (ws / "secret").mkdir(parents=True)
            (ws / "allowed" / "ok.txt").write_text("ok", encoding="utf-8")
            (ws / "secret" / "no.txt").write_text("no", encoding="utf-8")
            sup = Supervisor(ws, runtime_root=Path(td) / ".rt")
            try:
                sup.submit_contract(
                    {
                        "schema_version": "1.0.0",
                        "plan_id": "P",
                        "phase_id": "PH",
                        "job_id": "J-READ",
                        "batch_id": "B-READ",
                        "batch_ordinal": 1,
                        "project_id": "PR",
                        "workspace_root_ref": ".",
                        "goal": "g",
                        "allowed_read_paths": ["allowed/**"],
                        "allowed_write_paths": ["solution.txt", "out.txt"],
                        "forbidden_write_roots": [],
                        "acceptance_checks": ['python -c "pass"'],
                        "timeout_seconds": 30,
                    }
                )
                sup.approve_contract("J-READ", "qa")
                out = sup.run_job_attempt("J-READ", MockWorker("pass"), source_root=str(ws))
                att = sup.db.get_attempt(out.attempt_id)
                att_sb = Path(att["sandbox_path"])
                self.assertTrue((att_sb / "allowed" / "ok.txt").is_file())
                self.assertFalse((att_sb / "secret" / "no.txt").exists())
            finally:
                sup.close()

    def test_3_13_relative_path_and_empty_allowed_commands(self) -> None:
        self.assertTrue(is_safe_relative_path("src/**/*.py")[0])
        self.assertFalse(is_safe_relative_path("../escape")[0])
        self.assertFalse(is_safe_relative_path("C:\\Windows\\win.ini")[0])
        self.assertFalse(command_allowed("python -V", []))
        self.assertFalse(command_allowed(r"C:\evil\python.exe -c pass", ["python"]))
        self.assertFalse(command_allowed("evil/python -c pass", ["python"]))
        self.assertTrue(command_allowed(f'"{sys.executable}" -c "print(1)"', ["python"]))

    def test_3_4_cmd_wrapper_rejects_metachars_in_sandbox_dir(self) -> None:
        worker = CLIHarnessWorker("opencode")
        with tempfile.TemporaryDirectory() as td:
            evil_sb = Path(td) / "poc_a&echo_PWNED"
            evil_sb.mkdir()
            with (
                patch(
                    "src.workers.cli_harness_worker.probe_harness",
                    return_value={"available": True, "executable": "opencode-cli.cmd", "is_cmd_wrapper": True},
                ),
                patch("src.workers.cli_harness_worker.subprocess.Popen") as popen_mock,
            ):
                res = worker.start({"job_id": "j-bat-sb", "goal": "safe goal"}, str(evil_sb), "att-bat")
            self.assertEqual(res.status, "FAILED")
            self.assertIn("Policy block", res.stderr)
            popen_mock.assert_not_called()

    def test_3_7_and_3_8_supervisor_merges_protected_roots_and_maps_workspace_forbidden_roots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            (ws / "src").mkdir(parents=True)
            (ws / "src" / "app.py").write_text("v1\n", encoding="utf-8")
            prot = Path(td) / "ext_prot"
            prot.mkdir()
            prot_file = prot / "secret.txt"
            prot_file.write_text("keep", encoding="utf-8")

            old_val = os.environ.get("ORCHESTRA_PROTECTED_ROOTS")
            os.environ["ORCHESTRA_PROTECTED_ROOTS"] = str(prot)
            try:
                # 3.8: Supervisor merges ORCHESTRA_PROTECTED_ROOTS even when protected_paths is omitted
                class _ProtTamperWorker:
                    def start(self, contract, sandbox_dir, attempt_id, **kwargs):
                        prot_file.write_text("pwned", encoding="utf-8")
                        Path(sandbox_dir, "solution.txt").write_text("ok", encoding="utf-8")
                        from src.workers.base import WorkerResult
                        return WorkerResult(
                            job_id=contract["job_id"],
                            attempt_id=attempt_id,
                            worker_type="mock",
                            status="COMPLETED",
                            exit_code=0,
                            changed_files=["solution.txt"],
                        )

                sup = Supervisor(ws, runtime_root=Path(td) / ".rt")
                try:
                    sup.submit_contract(
                        {
                            "schema_version": "1.0.0",
                            "plan_id": "P",
                            "phase_id": "PH",
                            "job_id": "J-PROT-MERGE",
                            "batch_id": "B-PM",
                            "batch_ordinal": 1,
                            "project_id": "PR",
                            "workspace_root_ref": ".",
                            "goal": "g",
                            "allowed_write_paths": ["solution.txt"],
                            "forbidden_write_roots": [],
                            "acceptance_checks": ['python -c "pass"'],
                            "timeout_seconds": 30,
                        }
                    )
                    proj = sup.db.get_project("PR")
                    stored_prot = json.loads(proj["protected_paths_json"])
                    self.assertTrue(any(str(prot.resolve()).lower() == p.lower() for p in stored_prot))
                    sup.approve_contract("J-PROT-MERGE", "qa")
                    out_p = sup.run_job_attempt("J-PROT-MERGE", _ProtTamperWorker(), source_root=str(ws))
                    self.assertFalse(out_p.accepted)

                    # 3.7: Absolute forbidden_write_roots inside canonical ws blocks sandbox src/app.py edits
                    class _SrcEditWorker:
                        def start(self, contract, sandbox_dir, attempt_id, **kwargs):
                            Path(sandbox_dir, "src", "app.py").write_text("tampered\n", encoding="utf-8")
                            from src.workers.base import WorkerResult
                            return WorkerResult(
                                job_id=contract["job_id"],
                                attempt_id=attempt_id,
                                worker_type="mock",
                                status="COMPLETED",
                                exit_code=0,
                                changed_files=["src/app.py"],
                            )

                    sup.submit_contract(
                        {
                            "schema_version": "1.0.0",
                            "plan_id": "P",
                            "phase_id": "PH",
                            "job_id": "J-FORB-WS",
                            "batch_id": "B-FW",
                            "batch_ordinal": 1,
                            "project_id": "PR",
                            "workspace_root_ref": ".",
                            "goal": "g",
                            "allowed_write_paths": ["**"],
                            "forbidden_write_roots": [str(ws / "src")],
                            "acceptance_checks": ['python -c "pass"'],
                            "timeout_seconds": 30,
                        }
                    )
                    sup.approve_contract("J-FORB-WS", "qa")
                    out_fw = sup.run_job_attempt("J-FORB-WS", _SrcEditWorker(), source_root=str(ws))
                    self.assertFalse(out_fw.accepted)
                    self.assertTrue(any("FORBIDDEN_ROOT" in r for r in out_fw.reasons))
                finally:
                    sup.close()
            finally:
                if old_val is None:
                    os.environ.pop("ORCHESTRA_PROTECTED_ROOTS", None)
                else:
                    os.environ["ORCHESTRA_PROTECTED_ROOTS"] = old_val

    def test_3_1_and_3_15_mcp_list_jobs_limit_and_untrusted_qa_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = Path(td) / "hub"
            srv = OrchestraMCPServer(hub_store_dir=hub)
            # 3.15: orchestra_list_jobs rejects invalid non-int limit and clamps negative/huge limits safely
            r_bad = srv.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "orchestra_list_jobs", "arguments": {"limit": "not-an-int"}},
                }
            )
            self.assertTrue(r_bad["result"]["isError"])
            r_neg = srv.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "orchestra_list_jobs", "arguments": {"limit": -5}},
                }
            )
            self.assertFalse(r_neg["result"]["isError"])

            # 3.1: untrusted MCP caller cannot smuggle arbitrary powershell allowed_commands/acceptance_checks
            sb = Path(td) / "sb_cmd"
            sb.mkdir()
            r_run = srv.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "orchestra_run_job",
                        "arguments": {
                            "job_id": "mcp-pin-qa",
                            "worker_type": "mock",
                            "mock_behavior": "pass",
                            "goal": "ok",
                            "sandbox_dir": str(sb),
                            "allowed_commands": ["powershell"],
                            "acceptance_checks": ["powershell -c exit 1"],
                        },
                    },
                }
            )
            self.assertFalse(r_run["result"]["isError"])
            body = json.loads(r_run["result"]["content"][0]["text"])
            self.assertTrue(body["accepted"])
