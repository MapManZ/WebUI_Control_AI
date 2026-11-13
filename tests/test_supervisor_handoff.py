"""Supervisor lifecycle, G7 two-job batch gate, bounded repair loop, handoff and reconstruction tests."""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.e2e import Env, ScriptedWorker, good_result, make_contract, rmtree_retry, write_files

from src.handoff import HandoffError, generate_handoff, parse_handoff, verify_handoff, verify_next_context
from src.orchestra import BatchGateError, ContractRejected, SimulatedCrash
from src.state import fsm
from src.workers.mock_worker import MockWorker


class SupervisorRepairTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.sup = self.env.sup
        self.addCleanup(self.env.cleanup)

    def test_bounded_rework_loop_exhaustion_leads_to_failed(self):
        # max_rework_cycles=2
        self.env.job("R1", 1, max_rework_cycles=2)
        # Worker fails checks each time with code defect
        worker = MockWorker("fake_pass_with_bad_code")
        outs = self.sup.run_until_done("R1", worker, source_root=self.env.src, max_attempts=5)
        # Attempt 1: rework 1 (REWORK_REQUIRED)
        # Attempt 2: rework 2 (REWORK_REQUIRED)
        # Attempt 3: rework budget exhausted (FAILED)
        self.assertEqual(len(outs), 3)
        self.assertEqual(outs[0].final_state, fsm.REWORK_REQUIRED)
        self.assertEqual(outs[1].final_state, fsm.REWORK_REQUIRED)
        self.assertEqual(outs[2].final_state, fsm.FAILED)
        self.assertEqual(self.env.state("R1"), fsm.FAILED)
        self.assertEqual(self.sup.fsm.get("R1")["rework_count"], 2)

    def test_rework_success_on_retry(self):
        self.env.job("R2", 1, max_rework_cycles=3)
        # Attempt 1 fails, attempt 2 succeeds
        workers = [MockWorker("fake_pass_with_bad_code"), MockWorker("pass")]
        outs = self.sup.run_until_done("R2", workers, source_root=self.env.src)
        self.assertEqual(len(outs), 2)
        self.assertEqual(outs[0].final_state, fsm.REWORK_REQUIRED)
        self.assertEqual(outs[1].final_state, fsm.ACCEPTED)
        self.assertEqual(self.env.state("R2"), fsm.ACCEPTED)
        self.assertEqual(self.sup.fsm.get("R2")["rework_count"], 1)

    def test_scope_violation_blocks_immediately_without_rework(self):
        self.env.job("R3", 1, max_rework_cycles=3)
        worker = MockWorker("scope_violation")
        outs = self.sup.run_until_done("R3", worker, source_root=self.env.src)
        self.assertEqual(len(outs), 1)
        self.assertEqual(outs[0].final_state, fsm.BLOCKED)
        self.assertEqual(outs[0].failure_class, "SCOPE_VIOLATION")
        self.assertEqual(self.env.state("R3"), fsm.BLOCKED)
        self.assertEqual(self.sup.fsm.get("R3")["rework_count"], 0)

    def test_acceptance_checks_cannot_be_mutated_during_rework(self):
        self.env.job("R4", 1)
        out = self.sup.run_job_attempt("R4", MockWorker("fake_pass_with_bad_code"), source_root=self.env.src)
        self.assertEqual(out.final_state, fsm.REWORK_REQUIRED)
        # Attempt to requeue with mutated acceptance checks
        bad_contract = make_contract("R4", 1, acceptance_checks=['python -c "print(1)"'])
        with self.assertRaises(Exception):
            self.sup.repair.requeue_for_rework("R4", bad_contract)

    def test_infra_retries_budget_exhaustion(self):
        self.env.job("R5", 1, max_infra_retries=2)
        worker = MockWorker("timeout")
        outs = self.sup.run_until_done("R5", worker, source_root=self.env.src, max_attempts=5)
        # Attempt 1 -> retry 1
        # Attempt 2 -> retry 2
        # Attempt 3 -> budget exhausted -> FAILED
        self.assertEqual(len(outs), 3)
        self.assertEqual(outs[-1].final_state, fsm.FAILED)
        self.assertEqual(outs[-1].failure_class, "INFRA_TRANSIENT")


class BatchGateG7Tests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.sup = self.env.sup
        self.addCleanup(self.env.cleanup)

    def test_two_job_batch_limit_at_submit(self):
        c1 = make_contract("J1", 1, batch="B1")
        c2 = make_contract("J2", 2, batch="B1")
        c3 = make_contract("J3", 3, batch="B1")
        self.sup.submit_contract(c1)
        self.sup.submit_contract(c2)
        with self.assertRaises(BatchGateError):
            self.sup.submit_contract(c3)

    def test_duplicate_batch_ordinal_rejected(self):
        c1 = make_contract("J1", 1, batch="B1")
        c2 = make_contract("J2", 1, batch="B1")
        self.sup.submit_contract(c1)
        with self.assertRaises(BatchGateError):
            self.sup.submit_contract(c2)

    def test_job2_cannot_start_before_job1_handoff_verified(self):
        self.env.job("J1", 1, batch="B1")
        self.env.job("J2", 2, batch="B1", dependencies=["J1"])
        # Attempting to start J2 when J1 is QUEUED
        with self.assertRaises(BatchGateError):
            self.sup.run_job_attempt("J2", MockWorker(), source_root=self.env.src)
        # Run J1 to ACCEPTED
        o1 = self.sup.run_job_attempt("J1", MockWorker(), source_root=self.env.src)
        self.assertEqual(o1.final_state, fsm.ACCEPTED)
        # J1 is ACCEPTED, but NOT YET HANDOFF_VERIFIED
        with self.assertRaises(BatchGateError):
            self.sup.run_job_attempt("J2", MockWorker(), source_root=self.env.src)
        # Finalize handoff for J1 -> now HANDOFF_VERIFIED
        self.sup.finalize_handoff("J1", "J2")
        self.assertEqual(self.env.state("J1"), fsm.HANDOFF_VERIFIED)
        # Now J2 can start and run cleanly!
        o2 = self.sup.run_job_attempt("J2", MockWorker(), source_root=self.env.src)
        self.assertEqual(o2.final_state, fsm.ACCEPTED)

    def test_job2_can_start_if_job1_is_closed(self):
        self.env.job("J1", 1, batch="B1")
        self.env.job("J2", 2, batch="B1", dependencies=["J1"])
        self.sup.run_job_attempt("J1", MockWorker(), source_root=self.env.src)
        self.sup.finalize_handoff("J1", "J2")
        self.sup.close_job("J1")
        self.assertEqual(self.env.state("J1"), fsm.CLOSED)
        # J2 can proceed because J1 is in DONE_STATES
        o2 = self.sup.run_job_attempt("J2", MockWorker(), source_root=self.env.src)
        self.assertEqual(o2.final_state, fsm.ACCEPTED)


class HandoffLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.sup = self.env.sup
        self.addCleanup(self.env.cleanup)

    def test_generate_and_machine_verify_handoff(self):
        self.env.job("H1", 1, batch="BH")
        o = self.sup.run_job_attempt("H1", MockWorker(), source_root=self.env.src)
        self.assertEqual(o.final_state, fsm.ACCEPTED)
        info = self.sup.finalize_handoff("H1", "H2", context_density="Moderate")
        self.assertEqual(info["state"], fsm.HANDOFF_VERIFIED)
        self.assertTrue(os.path.isfile(info["handoff_path"]))
        self.assertTrue(os.path.isfile(info["next_context_path"]))

        # Independent verification against DB with HMAC signature
        res = verify_handoff(info["handoff_path"], db=self.sup.db, hmac_key=self.sup.hmac_key, require_signature=True)
        self.assertTrue(res.ok, res.errors)
        self.assertEqual(res.receipt["job_id"], "H1")
        self.assertEqual(res.receipt["next_job_id"], "H2")
        self.assertEqual(res.receipt["context_density"], "Moderate")

        # NEXT_CONTEXT verification
        nres = verify_next_context(info["next_context_path"], info["handoff_path"])
        self.assertTrue(nres.ok, nres.errors)

    def test_tampered_handoff_rejected(self):
        self.env.job("H1", 1, batch="BH")
        self.sup.run_job_attempt("H1", MockWorker(), source_root=self.env.src)
        info = self.sup.finalize_handoff("H1", "H2")

        # Tamper with markdown text (e.g. change changed files)
        hp = Path(info["handoff_path"])
        content = hp.read_text(encoding="utf-8")
        tampered = content.replace("out/result.txt", "evil/backdoor.py")
        hp.write_text(tampered, encoding="utf-8")

        res = verify_handoff(hp, db=self.sup.db, hmac_key=self.sup.hmac_key)
        self.assertFalse(res.ok)
        self.assertTrue(any("digest" in e.lower() or "receipt" in e.lower() or "tamper" in e.lower() for e in res.errors))

    def test_reconstruct_from_handoff_after_db_loss(self):
        self.env.job("H1", 1, batch="BH")
        self.sup.run_job_attempt("H1", MockWorker(), source_root=self.env.src)
        info = self.sup.finalize_handoff("H1", "H2")

        # Reconstruct into a new supervisor instance with clean/empty DB
        tmp_recon = tempfile.mkdtemp(prefix="orch_recon_")
        self.addCleanup(rmtree_retry, tmp_recon)
        from src.orchestra.supervisor import Supervisor
        recon_sup = Supervisor(tmp_recon, hmac_key=self.sup.hmac_key)
        self.addCleanup(recon_sup.close)

        res = recon_sup.reconstruct_from_handoff(info["handoff_path"], next_context_path=info["next_context_path"])
        self.assertEqual(res["job_id"], "H1")
        self.assertEqual(res["state"], fsm.HANDOFF_VERIFIED)
        self.assertTrue(res["reconstructed"])
        # Verify DB in recon instance has project and contract
        self.assertEqual(recon_sup.fsm.state("H1"), fsm.HANDOFF_VERIFIED)
        self.assertIsNotNone(recon_sup.db.get_contract_row("H1"))

    def test_cancel_job(self):
        self.env.job("C1", 1)
        self.assertEqual(self.env.state("C1"), fsm.QUEUED)
        self.sup.cancel_job("C1", actor="operator-test")
        self.assertEqual(self.env.state("C1"), fsm.CANCELLED)


if __name__ == "__main__":
    unittest.main()
