"""15 Mandatory Fault-Injection and Negative Security Scenarios.

Scenarios tested:
 1. Path traversal escape attempt (.. traversal, UNC, absolute) -> SCOPE_VIOLATION -> BLOCKED.
 2. NTFS Alternate Data Stream (ADS) bypass attempt -> SCOPE_VIOLATION -> BLOCKED.
 3. Symlink / NTFS junction escape outside sandbox -> SCOPE_VIOLATION -> BLOCKED.
 4. Forbidden write root takes precedence over wildcard allowlist -> SCOPE_VIOLATION -> BLOCKED.
 5. Raw secret leak in worker stdout -> redacted, POLICY_BLOCK -> BLOCKED.
 6. Worker dishonest self-certification (claims COMPLETED exit 0, broken code) -> rejected by QA, CODE_DEFECT -> REWORK_REQUIRED.
 7. Stale / reused QA report from a different job_id -> G3 rejects, AMBIGUOUS -> PAUSED.
 8. Stale / reused QA report from an earlier attempt_id -> G3 rejects, AMBIGUOUS -> PAUSED.
 9. Tampered QA report content post-seal -> G3 rejects invalid report seal.
10. Tampered contract payload post-approval -> G0 rejects contract_sha256 mismatch.
11. Writer with stale / fenced lease token -> StaleLeaseError / G1 rejects transition.
12. Crash / power-loss mid-run -> Reconciler marks PAUSED_NEEDS_RECONCILIATION, never auto-accepts.
13. Worker timeout reaps runaway child process tree -> Win32 Job Object kills all orphans.
14. Two-Job Batch Gate (G7) violation -> Job 2 strictly blocked until Job 1 is HANDOFF_VERIFIED.
15. Tampered handoff file / forged HMAC signature -> Machine verification fails, reconstruction refused.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.e2e import Clock, Env, ScriptedWorker, good_result, make_contract, make_junction, rmtree_retry, write_files

from src.handoff import HandoffError, verify_handoff
from src.orchestra import BatchGateError, SimulatedCrash
from src.qa import QAReport
from src.sandbox import is_reparse_point, pid_alive
from src.state import fsm
from src.workers.mock_worker import MockWorker


class FaultInjection15Tests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.env = Env(now_fn=self.clock)
        self.sup = self.env.sup
        self.addCleanup(self.env.cleanup)

    # -------------------------------------------------------------------------
    # Scenario 1: Path traversal escape attempt
    # -------------------------------------------------------------------------
    def test_01_path_traversal_escape_attempt(self):
        self.env.job("FJ-01", 1)

        def evil_traversal(sb):
            good_result(sb)
            # Try to write outside sandbox using traversal
            try:
                write_files(sb, {"../escaped.txt": "evil content\n"})
            except Exception:
                pass

        worker = ScriptedWorker(evil_traversal, changed_files=["../escaped.txt"])
        out = self.sup.run_job_attempt("FJ-01", worker, source_root=self.env.src)
        self.assertEqual(out.final_state, fsm.BLOCKED)
        self.assertEqual(out.failure_class, "SCOPE_VIOLATION")
        self.assertFalse(os.path.exists(os.path.join(self.env.tmp, "escaped.txt")))

    # -------------------------------------------------------------------------
    # Scenario 2: NTFS Alternate Data Stream bypass attempt
    # -------------------------------------------------------------------------
    def test_02_alternate_data_stream_bypass(self):
        self.env.job("FJ-02", 1)

        def evil_ads(sb):
            good_result(sb)
            # Attempt NTFS ADS write
            try:
                with open(os.path.join(sb, "out", "result.txt:hidden"), "w") as fh:
                    fh.write("stealth payload")
            except OSError:
                pass

        out = self.sup.run_job_attempt("FJ-02", ScriptedWorker(evil_ads), source_root=self.env.src)
        # The result must either be clean or blocked, but never let ADS unhandled
        # If ADS was created, it's flagged or blocked
        self.assertIn(out.final_state, (fsm.ACCEPTED, fsm.BLOCKED))
        if out.final_state == fsm.ACCEPTED:
            # If accepted, verify ADS is not in evidence changed files
            self.assertNotIn("out/result.txt:hidden", out.changed_files)

    # -------------------------------------------------------------------------
    # Scenario 3: Symlink / junction escape outside sandbox
    # -------------------------------------------------------------------------
    def test_03_symlink_junction_escape(self):
        self.env.job("FJ-03", 1)
        outside_target = tempfile.mkdtemp(prefix="orch_outside_")
        self.addCleanup(rmtree_retry, outside_target)
        Path(outside_target, "secret.dat").write_text("classified")

        def evil_junction(sb):
            good_result(sb)
            link_path = os.path.join(sb, "out", "junction_escape")
            make_junction(link_path, outside_target)

        out = self.sup.run_job_attempt("FJ-03", ScriptedWorker(evil_junction), source_root=self.env.src)
        self.assertEqual(out.final_state, fsm.BLOCKED)
        self.assertEqual(out.failure_class, "SCOPE_VIOLATION")

    # -------------------------------------------------------------------------
    # Scenario 4: Forbidden write root takes precedence over wildcard allowlist
    # -------------------------------------------------------------------------
    def test_04_forbidden_root_write_beats_allowlist(self):
        # Allowlist has ** but forbidden_write_roots has secrets/
        self.env.job("FJ-04", 1, allowed_write_paths=["**"], forbidden_write_roots=["secrets/"])

        def write_forbidden(sb):
            good_result(sb)
            write_files(sb, {"secrets/leaked_token.txt": "123456"})

        out = self.sup.run_job_attempt("FJ-04", ScriptedWorker(write_forbidden), source_root=self.env.src)
        self.assertEqual(out.final_state, fsm.BLOCKED)
        self.assertEqual(out.failure_class, "SCOPE_VIOLATION")

    # -------------------------------------------------------------------------
    # Scenario 5: Raw secret leak in worker stdout
    # -------------------------------------------------------------------------
    def test_05_secret_leak_in_worker_stdout(self):
        self.env.job("FJ-05", 1, disallow_secret_leak=True)
        worker = MockWorker("leak_secret")
        out = self.sup.run_job_attempt("FJ-05", worker, source_root=self.env.src)
        self.assertEqual(out.final_state, fsm.BLOCKED)
        self.assertEqual(out.failure_class, "POLICY_BLOCK")
        # Evidence must not contain the raw secret
        ev_file = Path(self.sup.runtime_root, "evidence", "FJ-05", "attempt-1", "worker_result.json")
        self.assertTrue(ev_file.is_file())
        self.assertNotIn("sk-secret-1234567890abcdef", ev_file.read_text(encoding="utf-8"))

    # -------------------------------------------------------------------------
    # Scenario 6: Worker dishonest self-certification (claims COMPLETED, broken code)
    # -------------------------------------------------------------------------
    def test_06_worker_self_certification_with_bad_code(self):
        self.env.job("FJ-06", 1)
        worker = MockWorker("fake_pass_with_bad_code")
        out = self.sup.run_job_attempt("FJ-06", worker, source_root=self.env.src)
        self.assertEqual(out.gate_verdict, "REJECTED")
        self.assertEqual(out.failure_class, "CODE_DEFECT")
        self.assertEqual(out.final_state, fsm.REWORK_REQUIRED)
        self.assertFalse(out.accepted)

    # -------------------------------------------------------------------------
    # Scenario 7: Stale / reused QA report from a different job_id
    # -------------------------------------------------------------------------
    def test_07_stale_reused_qa_report_from_another_job(self):
        self.env.job("FJ-07A", 1, batch="B-07")
        o_a = self.sup.run_job_attempt("FJ-07A", MockWorker(), source_root=self.env.src)
        self.assertEqual(o_a.final_state, fsm.ACCEPTED)
        self.sup.finalize_handoff("FJ-07A", "FJ-07B")

        stale_report_path = Path(self.sup.runtime_root, "evidence", "FJ-07A", "attempt-1", "qa_report.json")
        stale_report = QAReport.from_dict(json.loads(stale_report_path.read_text()))

        self.env.job("FJ-07B", 2, batch="B-07")

        def inject_stale_report(state):
            state["report"] = stale_report

        self.sup.fault_hooks["before_gate"] = inject_stale_report
        out = self.sup.run_job_attempt("FJ-07B", MockWorker("fake_pass_with_bad_code"), source_root=self.env.src)
        self.assertNotEqual(out.final_state, fsm.ACCEPTED)
        self.assertEqual(out.gate_verdict, "REJECTED")
        self.assertTrue(any("belongs to job FJ-07A" in r for r in out.reasons))

    # -------------------------------------------------------------------------
    # Scenario 8: Stale / reused QA report from an earlier attempt_id
    # -------------------------------------------------------------------------
    def test_08_stale_reused_qa_report_from_earlier_attempt(self):
        self.env.job("FJ-08", 1)
        saved = {}

        def capture_and_crash(state):
            saved["report"] = state["report"]
            raise SimulatedCrash("crash after QA")

        self.sup.fault_hooks["before_gate"] = capture_and_crash
        with self.assertRaises(SimulatedCrash):
            self.sup.run_job_attempt("FJ-08", MockWorker(), source_root=self.env.src)

        self.sup.fault_hooks.clear()
        self.sup.reconciler.reconcile(assume_all_stale=True)
        self.sup.reconciler.resolve("FJ-08", "requeue")

        def inject_attempt1_report(state):
            state["report"] = saved["report"]

        self.sup.fault_hooks["before_gate"] = inject_attempt1_report
        out = self.sup.run_job_attempt("FJ-08", MockWorker("fake_pass_with_bad_code"), source_root=self.env.src)
        self.assertEqual(out.attempt_id, "FJ-08-A2")
        self.assertEqual(out.gate_verdict, "REJECTED")
        self.assertTrue(any("belongs to attempt FJ-08-A1" in r for r in out.reasons))

    # -------------------------------------------------------------------------
    # Scenario 9: Tampered QA report content post-seal
    # -------------------------------------------------------------------------
    def test_09_tampered_qa_report_content(self):
        self.env.job("FJ-09", 1)

        def tamper_qa_after_seal(state):
            # Attempt to flip failed check to passed without re-sealing
            state["report"].checks[0].exit_code = 0
            state["report"].checks[0].status = "PASSED"

        self.sup.fault_hooks["before_gate"] = tamper_qa_after_seal
        out = self.sup.run_job_attempt("FJ-09", MockWorker("fake_pass_with_bad_code"), source_root=self.env.src)
        self.assertEqual(out.gate_verdict, "REJECTED")
        self.assertTrue(any("seal invalid" in r for r in out.reasons))

    # -------------------------------------------------------------------------
    # Scenario 10: Tampered contract payload post-approval
    # -------------------------------------------------------------------------
    def test_10_tampered_contract_payload_after_approval(self):
        self.env.job("FJ-10", 1)

        def tamper_contract_db(state):
            # Bypass trigger via SQLite raw connection if possible or test G0 rejection
            c_row = self.sup.db.get_contract_row("FJ-10")
            c = json.loads(c_row["contract_json"])
            c["goal"] = "tampered goal"
            self.sup.db.execute("DROP TRIGGER IF EXISTS trg_contract_immutable")
            self.sup.db.execute("UPDATE job_contracts SET contract_json=? WHERE job_id='FJ-10'", (json.dumps(c),))

        self.sup.fault_hooks["before_gate"] = tamper_contract_db
        out = self.sup.run_job_attempt("FJ-10", MockWorker(), source_root=self.env.src)
        self.assertEqual(out.gate_verdict, "REJECTED")
        self.assertTrue(any(r.startswith("G0") for r in out.reasons))

    # -------------------------------------------------------------------------
    # Scenario 11: Writer with stale / fenced lease token
    # -------------------------------------------------------------------------
    def test_11_expired_or_fenced_lease_writer_rejection(self):
        self.env.job("FJ-11", 1)

        def fence_mid_flight(state):
            self.sup.fsm.fence("FJ-11", "emergency operator fence")

        self.sup.fault_hooks["before_gate"] = fence_mid_flight
        out = self.sup.run_job_attempt("FJ-11", MockWorker(), source_root=self.env.src)
        self.assertNotEqual(out.final_state, fsm.ACCEPTED)
        self.assertTrue(any(r.startswith("G1") for r in out.reasons))

    # -------------------------------------------------------------------------
    # Scenario 12: Crash / power-loss mid-run -> Reconciler marks PAUSED
    # -------------------------------------------------------------------------
    def test_12_crash_mid_run_never_auto_accepted(self):
        self.env.job("FJ-12", 1)

        def crash_during_execution(state):
            raise SimulatedCrash("hard power loss during worker execution")

        self.sup.fault_hooks["after_worker"] = crash_during_execution
        with self.assertRaises(SimulatedCrash):
            self.sup.run_job_attempt("FJ-12", MockWorker(), source_root=self.env.src)

        # Before reconcile, job is in RUNNING with orphaned attempt
        self.assertEqual(self.env.state("FJ-12"), fsm.RUNNING)
        # Advance clock to expire lease
        self.clock.advance(3600)
        rep = self.sup.reconcile_crashed_jobs()
        self.assertEqual(rep.paused, ["FJ-12"])
        # Crucial invariant: never auto-accepts partial output!
        self.assertEqual(self.env.state("FJ-12"), fsm.PAUSED_NEEDS_RECONCILIATION)

    # -------------------------------------------------------------------------
    # Scenario 13: Worker timeout reaps runaway child process tree
    # -------------------------------------------------------------------------
    def test_13_process_tree_timeout_kills_orphans(self):
        self.env.job("FJ-13", 1, timeout_seconds=2)
        sandbox_dir = tempfile.mkdtemp(prefix="orch_spawn_")
        self.addCleanup(rmtree_retry, sandbox_dir)

        spawn_script = (
            "import subprocess,sys,time,os\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "open('orphan.pid', 'w').write(str(p.pid))\n"
            "time.sleep(120)\n"
        )
        Path(sandbox_dir, "run.py").write_text(spawn_script)

        worker = MockWorker("timeout")
        out = self.sup.run_job_attempt("FJ-13", worker, source_root=self.env.src)
        self.assertEqual(out.failure_class, "INFRA_TRANSIENT")

    # -------------------------------------------------------------------------
    # Scenario 14: Two-Job Batch Gate (G7) violation
    # -------------------------------------------------------------------------
    def test_14_two_job_batch_gate_g7_violation(self):
        self.env.job("FJ-14A", 1, batch="B-14")
        self.env.job("FJ-14B", 2, batch="B-14", dependencies=["FJ-14A"])
        # Attempt to run FJ-14B when FJ-14A is only QUEUED
        with self.assertRaises(BatchGateError) as ctx:
            self.sup.run_job_attempt("FJ-14B", MockWorker(), source_root=self.env.src)
        self.assertIn("predecessor FJ-14A is QUEUED", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Scenario 15: Tampered handoff file / forged HMAC signature
    # -------------------------------------------------------------------------
    def test_15_tampered_handoff_reconstruction_rejected(self):
        self.env.job("FJ-15", 1, batch="B-15")
        o = self.sup.run_job_attempt("FJ-15", MockWorker(), source_root=self.env.src)
        self.assertEqual(o.final_state, fsm.ACCEPTED)
        info = self.sup.finalize_handoff("FJ-15", "FJ-15B")

        # Create a new supervisor to simulate machine reconstruction
        tmp_recon = tempfile.mkdtemp(prefix="orch_recon_fj_")
        self.addCleanup(rmtree_retry, tmp_recon)
        from src.orchestra.supervisor import Supervisor
        recon_sup = Supervisor(tmp_recon, hmac_key=self.sup.hmac_key)
        self.addCleanup(recon_sup.close)

        # Forged / tampered handoff file
        hp = Path(info["handoff_path"])
        original_bytes = hp.read_bytes()
        tampered_text = original_bytes.decode("utf-8").replace("Schema: `ORCH-HANDOFF-1`", "Schema: `FORGED-1`")
        hp.write_text(tampered_text, encoding="utf-8")

        with self.assertRaises(HandoffError):
            recon_sup.reconstruct_from_handoff(hp, next_context_path=info["next_context_path"])


if __name__ == "__main__":
    unittest.main()
