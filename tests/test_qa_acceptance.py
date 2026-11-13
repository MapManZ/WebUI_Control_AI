"""Independent QA runner + deterministic acceptance gate (G0-G5) tests."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.e2e import Env, ScriptedWorker, good_result, make_contract, rmtree_retry, write_files

from src.orchestra import SimulatedCrash
from src.qa import (
    QAReport, QARunner, RoleDAdapter, build_review_request, command_allowed, contains_secret, normalize_check, redact,
    sanitized_env, seal_review, verify_review,
)
from src.workers.mock_worker import MockWorker

PY = "python -c "


def _runner_dir():
    d = tempfile.mkdtemp(prefix="orch_qa_")
    return d


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.d = _runner_dir()
        self.addCleanup(rmtree_retry, self.d)
        self.qa = QARunner(browser_path=None)

    def _run(self, checks, **contract_extra):
        c = make_contract("Q1", acceptance_checks=checks, **contract_extra)
        return self.qa.run_checks(c, self.d, "Q1", "Q1-A1", **{k: v for k, v in {}.items()})

    def test_real_exit_codes_captured(self):
        rep = self._run([PY + '"import sys; sys.exit(0)"', PY + '"import sys; sys.exit(3)"'])
        self.assertEqual([c.exit_code for c in rep.checks], [0, 3])
        self.assertEqual([c.status for c in rep.checks], ["PASSED", "FAILED"])
        self.assertFalse(rep.all_exit_zero)
        self.assertTrue(rep.verify_seal())

    def test_all_pass(self):
        rep = self._run([PY + '"print(1)"'])
        self.assertTrue(rep.all_exit_zero)
        self.assertEqual(rep.job_id, "Q1")
        self.assertEqual(rep.attempt_id, "Q1-A1")

    def test_secret_redaction_and_violation(self):
        script = ("import sys;print('key sk-secret-1234567890abcdef');"
                  "print('API_KEY=abcd1234efgh');print('Authorization: Bearer abcdef0123456789');"
                  "print('MY_SECRET=hunter2hunter2')")
        c = make_contract("Q1", acceptance_checks=[PY + '"' + script + '"'])
        rep = self.qa.run_checks(c, self.d, "Q1", "Q1-A1", disallow_secret_leak=True)
        out = rep.checks[0].stdout
        for raw in ("sk-secret-1234567890abcdef", "abcd1234efgh", "abcdef0123456789", "hunter2hunter2"):
            self.assertNotIn(raw, out)
        self.assertIn("[REDACTED]", out)
        self.assertTrue(rep.checks[0].secret_leak_detected)
        self.assertTrue(rep.security_violation)
        self.assertEqual(rep.checks[0].status, "FAILED")  # exit 0 but leak => not a pass
        self.assertFalse(rep.all_exit_zero)
        rep2 = self.qa.run_checks(c, self.d, "Q1", "Q1-A2", disallow_secret_leak=False)
        self.assertTrue(rep2.checks[0].secret_leak_detected)
        self.assertFalse(rep2.security_violation)
        self.assertNotIn("sk-secret-1234567890abcdef", rep2.checks[0].stdout)

    def test_secret_helpers(self):
        self.assertTrue(contains_secret("x sk-abcdefghij1234"))
        self.assertTrue(contains_secret("Bearer abcdefgh12345"))
        self.assertTrue(contains_secret("API_KEY=zzzzzz"))
        self.assertTrue(contains_secret("SECRET=zzzzzz"))
        self.assertFalse(contains_secret("nothing to see here, sk-short"))
        self.assertEqual(redact("a sk-abcdefghij1234 b"), "a [REDACTED] b")

    def test_environment_sanitized(self):
        os.environ["ORCH_TEST_LEAKY_VAR"] = "1"
        os.environ["OPENAI_API_KEY"] = "sk-should-never-reach-child-1234"
        self.addCleanup(os.environ.pop, "ORCH_TEST_LEAKY_VAR", None)
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY", None)
        env = sanitized_env(self.d)
        self.assertNotIn("ORCH_TEST_LEAKY_VAR", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        script = "import os,sys;sys.exit(1 if ('OPENAI_API_KEY' in os.environ or 'ORCH_TEST_LEAKY_VAR' in os.environ) else 0)"
        rep = self._run([PY + '"' + script + '"'])
        self.assertTrue(rep.all_exit_zero)

    def test_timeout_is_failure(self):
        rep = self._run([{"command": PY + '"import time; time.sleep(30)"', "timeout_seconds": 1}])
        self.assertTrue(rep.checks[0].timed_out)
        self.assertEqual(rep.checks[0].status, "FAILED")
        self.assertFalse(rep.all_exit_zero)

    def test_allowed_commands_enforced(self):
        self.assertTrue(command_allowed("python -m unittest x", ["python"]))
        self.assertTrue(command_allowed("python3 -m x", ["python"]))
        self.assertFalse(command_allowed("cmd /c echo hi", ["python"]))
        self.assertFalse(command_allowed("powershell -c x", ["python", "git"]))
        self.assertTrue(command_allowed("anything", []))
        rep = self._run(["cmd /c echo hi"], allowed_commands=["python"])
        self.assertEqual(rep.checks[0].status, "POLICY_BLOCKED")
        self.assertTrue(rep.security_violation)

    def test_required_browser_unavailable_is_not_verified(self):
        chk = {"type": "browser", "url": "http://127.0.0.1:1/", "required": True}
        rep = self._run([chk])
        self.assertEqual(rep.checks[0].status, "NOT_VERIFIED")
        self.assertFalse(rep.all_exit_zero)
        self.assertTrue(rep.not_verified)

    def test_optional_browser_unavailable_does_not_block(self):
        rep = self._run([PY + '"print(1)"', {"type": "browser", "url": "http://127.0.0.1:1/", "required": False}])
        self.assertTrue(rep.all_exit_zero)

    @unittest.skipUnless(os.name == "nt", "uses a .cmd stub as fake Edge")
    def test_browser_available_runs_headless(self):
        stub = os.path.join(self.d, "fake_edge.cmd")
        Path(stub).write_text("@echo off\r\necho ^<html^>ok^</html^>\r\nexit /b 0\r\n")
        qa = QARunner(browser_path=stub)
        c = make_contract("Q1", acceptance_checks=[{"type": "browser", "url": "http://localhost/", "required": True}])
        rep = qa.run_checks(c, self.d, "Q1", "Q1-A1")
        self.assertEqual(rep.checks[0].status, "PASSED", rep.checks[0])
        self.assertTrue(rep.all_exit_zero)

    def test_report_seal_detects_modification(self):
        rep = self._run([PY + '"import sys; sys.exit(1)"'])
        self.assertTrue(rep.verify_seal())
        rep.checks[0].exit_code = 0
        rep.checks[0].status = "PASSED"
        self.assertFalse(rep.verify_seal())
        again = QAReport.from_dict(json.loads(json.dumps(rep.to_dict())))
        self.assertFalse(again.verify_seal())

    def test_normalize_check_stable_ids(self):
        a = normalize_check("python -c pass")
        b = normalize_check({"command": "python -c pass"})
        self.assertEqual(a["check_id"], b["check_id"])
        self.assertNotEqual(a["check_id"], normalize_check("python -c pass2")["check_id"])


class RoleDTests(unittest.TestCase):
    def test_verify_review(self):
        base = {"role": "D", "job_id": "J", "attempt_id": "J-A1", "evidence_sha256": "e" * 64, "verdict": "APPROVE",
                "reviewer": "chatgpt"}
        good = seal_review(base)
        self.assertEqual(verify_review(good, job_id="J", attempt_id="J-A1", evidence_sha256="e" * 64), (True, []))
        for bad, why in [
            (seal_review(dict(base, job_id="X")), "job_id"),
            (seal_review(dict(base, attempt_id="J-A0")), "attempt_id"),
            (seal_review(dict(base, evidence_sha256="f" * 64)), "evidence"),
            (seal_review(dict(base, verdict="REJECT")), "REJECT"),
            (seal_review(dict(base, verdict="MAYBE")), "malformed"),
            (dict(good, verdict="APPROVE", findings="edited after seal"), "tampered"),
            (None, "missing"),
        ]:
            ok, reasons = verify_review(bad, job_id="J", attempt_id="J-A1", evidence_sha256="e" * 64)
            self.assertFalse(ok, why)
            self.assertTrue(any(why.lower() in r.lower() for r in reasons), (why, reasons))
        ok, reasons = verify_review(good, job_id="J", attempt_id="J-A1", evidence_sha256="e" * 64, worker_type="ChatGPT")
        self.assertFalse(ok)
        self.assertIn("independent", reasons[0])

    def test_adapter_binds_request(self):
        req = build_review_request("J", "J-A1", "e" * 64, "c" * 64, ["b", "a"])
        self.assertEqual(req["changed_files"], ["a", "b"])
        ad = RoleDAdapter(lambda r: {"verdict": "APPROVE", "findings": "ok"}, "claude")
        rv = ad.review(req)
        self.assertEqual(verify_review(rv, job_id="J", attempt_id="J-A1", evidence_sha256="e" * 64), (True, []))
        self.assertIsNone(RoleDAdapter().review(req))


class GateTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(qa_runner=QARunner(browser_path=None))
        self.sup = self.env.sup
        self.addCleanup(self.env.cleanup)

    def _run(self, job_id, worker, **kw):
        return self.sup.run_job_attempt(job_id, worker, source_root=self.env.src, **kw)

    def _verifications(self, job_id):
        return self.env.db.query("SELECT * FROM qa_verifications WHERE job_id=? ORDER BY rowid", (job_id,))

    def test_accept_path_records_evidence(self):
        self.env.job("J")
        out = self._run("J", MockWorker())
        self.assertEqual(out.final_state, "ACCEPTED")
        self.assertEqual(out.gate_verdict, "ACCEPTED")
        v = self._verifications("J")[-1]
        self.assertEqual(v["gate_verdict"], "ACCEPTED")
        self.assertEqual((v["all_exit_zero"], v["allowlist_diff_ok"], v["protected_roots_untouched"]), (1, 1, 1))
        self.assertEqual(len(v["evidence_manifest_sha256"]), 64)
        cmds = json.loads(v["commands_run_json"])
        self.assertEqual([c["exit_code"] for c in cmds], [0])
        gate = json.loads(Path(self.sup.runtime_root, "evidence", "J", "attempt-1", "gate.json").read_text())
        self.assertTrue(all(g["ok"] for g in gate["gates"].values()), gate)
        self.assertEqual(set(gate["gates"]), {"G0", "G1", "G2", "G3", "G4", "G5"})

    def test_worker_self_certification_rejected(self):
        self.env.job("J")
        out = self._run("J", MockWorker("fake_pass_with_bad_code"))
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertEqual(out.gate_verdict, "REJECTED")
        self.assertEqual(out.failure_class, "CODE_DEFECT")
        self.assertEqual(out.final_state, "REWORK_REQUIRED")
        self.assertTrue(any("G4" in r for r in out.reasons))
        self.assertEqual(self.env.db.get_attempt("J-A1")["worker_status"], "COMPLETED")  # worker claimed success

    def test_diff_from_disk_ignores_worker_claims(self):
        self.env.job("J")

        def sneaky(sb):
            good_result(sb)
            write_files(sb, {"keep.txt": "modified outside allowlist\n"})

        out = self._run("J", ScriptedWorker(sneaky))  # claims changed_files=[] and COMPLETED
        self.assertEqual(out.final_state, "BLOCKED")
        self.assertEqual(out.failure_class, "SCOPE_VIOLATION")
        self.assertIn("keep.txt", out.changed_files)
        v = self._verifications("J")[-1]
        self.assertEqual((v["gate_verdict"], v["allowlist_diff_ok"]), ("REJECTED", 0))

    def test_forbidden_root_beats_allowlist(self):
        self.env.job("J", allowed_write_paths=["**"], forbidden_write_roots=["secrets/"])
        out = self._run("J", ScriptedWorker(lambda sb: (good_result(sb), write_files(sb, {"secrets/k.txt": "x"}))))
        self.assertEqual((out.final_state, out.failure_class), ("BLOCKED", "SCOPE_VIOLATION"))

    def test_deleting_a_file_is_a_change(self):
        self.env.job("J")

        def deleter(sb):
            good_result(sb)
            os.remove(os.path.join(sb, "keep.txt"))

        out = self._run("J", ScriptedWorker(deleter))
        self.assertEqual(out.failure_class, "SCOPE_VIOLATION")

    def test_protected_external_root_change_rejected(self):
        prot = tempfile.mkdtemp(prefix="orch_prot_")
        self.addCleanup(rmtree_retry, prot)
        Path(prot, "game.dat").write_text("original")
        self.env.job("J", protected=[prot])

        def attacker(sb):
            good_result(sb)
            Path(prot, "game.dat").write_text("MUTATED")

        out = self._run("J", ScriptedWorker(attacker))
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertEqual(out.failure_class, "SCOPE_VIOLATION")
        v = self._verifications("J")[-1]
        self.assertEqual(v["protected_roots_untouched"], 0)

    def test_protected_untouched_accepts(self):
        prot = tempfile.mkdtemp(prefix="orch_prot_")
        self.addCleanup(rmtree_retry, prot)
        Path(prot, "game.dat").write_text("original")
        self.env.job("J", protected=[prot])
        self.assertEqual(self._run("J", MockWorker()).final_state, "ACCEPTED")

    def test_stale_qa_report_from_other_job_rejected(self):
        self.env.job("A", 1)
        self.assertEqual(self._run("A", MockWorker()).final_state, "ACCEPTED")
        self.sup.finalize_handoff("A", "B")
        stale = QAReport.from_dict(json.loads(Path(self.sup.runtime_root, "evidence", "A", "attempt-1", "qa_report.json").read_text()))
        self.assertTrue(stale.all_exit_zero and stale.verify_seal())
        self.env.job("B", 2)

        def inject(state):
            state["report"] = stale

        self.sup.fault_hooks["before_gate"] = inject
        out = self._run("B", MockWorker("fake_pass_with_bad_code"))
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertEqual(out.gate_verdict, "REJECTED")
        self.assertTrue(any("belongs to job A" in r for r in out.reasons), out.reasons)

    def test_stale_qa_report_from_earlier_attempt_rejected(self):
        self.env.job("A")
        saved = {}

        def grab_then_crash(state):
            saved["report"] = state["report"]
            raise SimulatedCrash("die after QA, before gate")

        self.sup.fault_hooks["before_gate"] = grab_then_crash
        with self.assertRaises(SimulatedCrash):
            self._run("A", MockWorker())
        self.assertEqual(self.env.state("A"), "REVIEWING")  # crashed mid-review: nothing accepted
        self.sup.fault_hooks.clear()
        self.sup.reconciler.reconcile(assume_all_stale=True)
        self.sup.reconciler.resolve("A", "requeue")
        self.sup.fault_hooks["before_gate"] = lambda state: state.__setitem__("report", saved["report"])
        out = self._run("A", MockWorker("fake_pass_with_bad_code"))
        self.assertEqual(out.attempt_id, "A-A2")
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertTrue(any("belongs to attempt A-A1" in r for r in out.reasons), out.reasons)

    def test_report_with_fewer_checks_than_contract_rejected(self):
        second = PY + '"import sys; sys.exit(0)"'
        self.env.job("A", acceptance_checks=[make_contract("x")["acceptance_checks"][0], second])

        def drop_check(state):
            r = state["report"]
            r.checks = r.checks[:1]
            r.seal()

        self.sup.fault_hooks["before_gate"] = drop_check
        out = self._run("A", MockWorker())
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertTrue(any("differ from contract" in r for r in out.reasons), out.reasons)

    def test_unsealed_report_modification_rejected(self):
        self.env.job("A")

        def flip(state):
            state["report"].checks[0].exit_code = 0
            state["report"].checks[0].status = "PASSED"

        self.sup.fault_hooks["before_gate"] = flip
        out = self._run("A", MockWorker("fake_pass_with_bad_code"))
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertTrue(any("seal invalid" in r for r in out.reasons), out.reasons)

    def test_contract_tamper_after_approval_rejected_g0(self):
        self.env.job("A")

        def tamper(state):
            db = self.env.db
            row = db.get_contract_row("A")
            c = json.loads(row["contract_json"])
            c["acceptance_checks"] = [PY + '"pass"']  # weaker checks
            db.execute("DROP TRIGGER trg_contract_immutable")  # attacker with raw DB access
            db.execute("UPDATE job_contracts SET contract_json=? WHERE job_id='A'", (json.dumps(c),))

        self.sup.fault_hooks["before_gate"] = tamper
        out = self._run("A", MockWorker())
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertTrue(any(r.startswith("G0") for r in out.reasons), out.reasons)

    def test_contract_and_sha_both_tampered_still_caught_by_audit_log(self):
        self.env.job("A")

        def tamper(state):
            from src.state.db import contract_digest

            db = self.env.db
            c = json.loads(db.get_contract_row("A")["contract_json"])
            c["goal"] = "evil goal"
            c["contract_sha256"] = contract_digest(c)
            db.execute("DROP TRIGGER trg_contract_immutable")
            db.execute("UPDATE job_contracts SET contract_json=?, contract_sha256=? WHERE job_id='A'",
                       (json.dumps(c), c["contract_sha256"]))

        self.sup.fault_hooks["before_gate"] = tamper
        out = self._run("A", MockWorker())
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertTrue(any("audit log" in r for r in out.reasons), out.reasons)

    def test_fenced_lease_fails_g1(self):
        self.env.job("A")
        self.sup.fault_hooks["before_gate"] = lambda state: self.sup.fsm.fence("A", "test fence")
        out = self._run("A", MockWorker())
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertTrue(any(r.startswith("G1") for r in out.reasons), out.reasons)

    def test_g5_role_d_required(self):
        self.env.job("A", role_d_review_required=True)
        out = self._run("A", MockWorker())  # no role-D adapter configured
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertTrue(any(r.startswith("G5") for r in out.reasons), out.reasons)

    def test_g5_role_d_approve_accepts_reject_blocks(self):
        self.sup.role_d = RoleDAdapter(lambda req: {"verdict": "APPROVE", "findings": "fine"}, "claude")
        self.env.job("A", role_d_review_required=True)
        out = self._run("A", MockWorker())
        self.assertEqual(out.final_state, "ACCEPTED", out.reasons)
        v = self._verifications("A")[-1]
        self.assertEqual(json.loads(v["role_d_review_json"])["verdict"], "APPROVE")
        self.sup.finalize_handoff("A", "B")
        self.sup.role_d = RoleDAdapter(lambda req: {"verdict": "REJECT", "findings": "bad"}, "claude")
        self.env.job("B", 2, role_d_review_required=True)
        out = self._run("B", MockWorker())
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertTrue(any("REJECT" in r for r in out.reasons), out.reasons)

    def test_g5_review_bound_to_evidence(self):
        self.sup.role_d = RoleDAdapter(lambda req: {"verdict": "APPROVE", "evidence_sha256": "0" * 64}, "claude")
        self.env.job("A", role_d_review_required=True)
        out = self._run("A", MockWorker())
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertTrue(any("evidence" in r for r in out.reasons), out.reasons)

    def test_required_browser_unavailable_not_accepted(self):
        self.env.job("A", acceptance_checks=[make_contract("x")["acceptance_checks"][0],
                                             {"type": "browser", "url": "http://127.0.0.1:1/", "required": True}])
        out = self._run("A", MockWorker())
        self.assertEqual(out.gate_verdict, "NOT_VERIFIED")
        self.assertNotEqual(out.final_state, "ACCEPTED")
        self.assertEqual((out.final_state, out.failure_class), ("BLOCKED", "TOOL_UNAVAILABLE"))
        self.assertEqual(self._verifications("A")[-1]["gate_verdict"], "NOT_VERIFIED")

    def test_secret_in_check_output_blocks(self):
        leak = PY + '"print(\'sk-leakyleakyleaky12345\')"'
        self.env.job("A", acceptance_checks=[leak], allowed_write_paths=["out/result.txt"])
        out = self._run("A", MockWorker())
        self.assertEqual((out.final_state, out.failure_class), ("BLOCKED", "POLICY_BLOCK"))
        rep = Path(self.sup.runtime_root, "evidence", "A", "attempt-1", "qa_report.json").read_text()
        self.assertNotIn("sk-leakyleakyleaky12345", rep)


if __name__ == "__main__":
    unittest.main()
