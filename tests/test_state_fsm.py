"""State store, strict FSM, lease fencing and crash reconciler tests."""
import os
import sqlite3
import tempfile
import unittest

from tests.e2e import Clock, Env, make_contract, rmtree_retry

from src.state import (
    ALL_STATES, DONE_STATES, TRANSITIONS, FSMError, GuardError, IllegalTransition, JobFSM, LeaseConflict, Reconciler,
    StaleLeaseError, StateDB, contract_digest,
)
from src.state import fsm as F
from src.state.fsm import is_allowed


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="orch_db_")
        self.db = StateDB(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.db.close()
        rmtree_retry(self.tmp)

    def test_six_tables_wal_fk(self):
        self.assertEqual(
            self.db.table_names(),
            sorted(["projects", "job_contracts", "job_states", "job_attempts", "qa_verifications", "audit_events"]),
        )
        self.assertEqual(str(self.db.pragma("journal_mode")).lower(), "wal")
        self.assertEqual(self.db.pragma("foreign_keys"), 1)

    def test_required_columns(self):
        cols = lambda t: {r["name"] for r in self.db.query(f"PRAGMA table_info({t})")}  # noqa: E731
        self.assertTrue({"job_id", "current_state", "rework_count", "infra_retry_count", "active_attempt_id",
                         "lease_owner", "lease_token", "lease_generation", "lease_expires_at", "updated_at"} <= cols("job_states"))
        self.assertTrue({"event_seq", "idempotency_key", "job_id", "attempt_id", "event_type", "from_state", "to_state",
                         "payload_json", "created_at"} <= cols("audit_events"))
        self.assertTrue({"verification_id", "attempt_id", "job_id", "commands_run_json", "all_exit_zero",
                         "allowlist_diff_ok", "protected_roots_untouched", "role_d_review_json", "gate_verdict",
                         "evidence_manifest_sha256", "verified_at"} <= cols("qa_verifications"))

    def test_foreign_keys_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO job_states(job_id,current_state,updated_at) VALUES ('x','DRAFT','t')")

    def test_audit_append_only(self):
        self.db.append_audit("X", job_id="j")
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute("UPDATE audit_events SET event_type='Y'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute("DELETE FROM audit_events")

    def test_audit_idempotency_key_unique(self):
        a = self.db.append_audit("X", idempotency_key="k1")
        b = self.db.append_audit("X", idempotency_key="k1")
        self.assertIsNotNone(a)
        self.assertIsNone(b)
        self.assertEqual(len(self.db.audit_events()), 1)

    def test_close_releases_files_for_windows_cleanup(self):
        path = os.path.join(self.tmp, "close.db")
        d = StateDB(path)
        d.append_audit("X")
        d.close()
        d.close()  # idempotent
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)  # would raise PermissionError if still locked
        with self.assertRaises(sqlite3.ProgrammingError):
            d.query("SELECT 1")

    def test_transaction_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                self.db.append_audit("X", job_id="a")
                raise RuntimeError("boom")
        self.assertEqual(len(self.db.audit_events()), 0)


class TransitionTableTests(unittest.TestCase):
    def test_table_covers_all_states(self):
        self.assertEqual(set(TRANSITIONS), set(ALL_STATES))
        self.assertEqual(len(ALL_STATES), 18)

    def test_happy_chain_legal(self):
        chain = ["DRAFT", "PLAN_APPROVED", "QUEUED", "LEASED", "PREFLIGHT_PASSED", "RUNNING", "QA_RUNNING",
                 "REVIEWING", "ACCEPTED", "HANDOFF_VERIFIED", "CLOSED"]
        for a, b in zip(chain, chain[1:]):
            self.assertTrue(is_allowed(a, b), f"{a}->{b}")

    def test_forbidden_shortcuts(self):
        for a, b in [("RUNNING", "ACCEPTED"), ("REWORK_REQUIRED", "ACCEPTED"), ("QA_RUNNING", "ACCEPTED"),
                     ("QUEUED", "RUNNING"), ("DRAFT", "RUNNING"), ("LEASED", "RUNNING"), ("RUNNING", "REVIEWING"),
                     ("ACCEPTED", "CLOSED"), ("BLOCKED", "ACCEPTED"), ("FAILED", "QUEUED"), ("CLOSED", "DRAFT"),
                     ("PAUSED_NEEDS_RECONCILIATION", "ACCEPTED"), ("CANCELLED", "QUEUED")]:
            self.assertFalse(is_allowed(a, b), f"{a}->{b} must be forbidden")

    def test_accepted_only_reachable_from_reviewing(self):
        sources = [s for s, t in TRANSITIONS.items() if "ACCEPTED" in t]
        self.assertEqual(sources, ["REVIEWING"])

    def test_terminal_states_have_no_exit(self):
        for s in ("CLOSED", "FAILED", "CANCELLED"):
            self.assertEqual(TRANSITIONS[s], frozenset())


class FsmBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.env = Env(now_fn=self.clock)
        self.fsm = self.env.sup.fsm

    def tearDown(self):
        self.env.cleanup()

    def _to_running(self, job_id, owner="w1"):
        lease = self.fsm.acquire_lease(job_id, owner, 60)
        aid = self.fsm.create_attempt(job_id, worker_type="t", lease_token=lease.token, lease_generation=lease.generation)
        for s in (F.PREFLIGHT_PASSED, F.RUNNING):
            self.fsm.transition(job_id, s, lease_token=lease.token, lease_generation=lease.generation, attempt_id=aid)
        return lease, aid

    def test_cannot_approve_without_contract_approval(self):
        self.env.job("A", approve=False)
        with self.assertRaises(GuardError):
            self.fsm.transition("A", F.PLAN_APPROVED)

    def test_illegal_transition_rejected_and_audited(self):
        self.env.job("A")
        with self.assertRaises(IllegalTransition):
            self.fsm.transition("A", F.RUNNING)
        ev = [e for e in self.env.db.audit_events("A") if e["event_type"] == "TRANSITION_REJECTED"]
        self.assertTrue(ev)
        self.assertEqual(self.env.state("A"), F.QUEUED)

    def test_queued_to_leased_requires_acquire_lease(self):
        self.env.job("A")
        with self.assertRaises(IllegalTransition):
            self.fsm.transition("A", F.LEASED)

    def test_running_to_accepted_and_rework_to_accepted_forbidden(self):
        self.env.job("A")
        lease, aid = self._to_running("A")
        with self.assertRaises(IllegalTransition):
            self.fsm.transition("A", F.ACCEPTED, lease_token=lease.token, lease_generation=lease.generation)
        self.fsm.transition("A", F.REWORK_REQUIRED, lease_token=lease.token, lease_generation=lease.generation,
                            bump_rework=True)
        with self.assertRaises(IllegalTransition):
            self.fsm.transition("A", F.ACCEPTED)
        self.assertEqual(self.fsm.get("A")["rework_count"], 1)

    def test_accepted_from_reviewing_requires_independent_qa_row(self):
        self.env.job("A")
        lease, aid = self._to_running("A")
        for s in (F.QA_RUNNING, F.REVIEWING):
            self.fsm.transition("A", s, lease_token=lease.token, lease_generation=lease.generation)
        with self.assertRaises(GuardError):
            self.fsm.transition("A", F.ACCEPTED, lease_token=lease.token, lease_generation=lease.generation)
        # a REJECTED verification row must not unlock ACCEPTED either
        self.env.db.execute(
            "INSERT INTO qa_verifications VALUES ('v1',?,?,'[]',1,1,1,NULL,'REJECTED',NULL,'t')", (aid, "A"))
        with self.assertRaises(GuardError):
            self.fsm.transition("A", F.ACCEPTED, lease_token=lease.token, lease_generation=lease.generation)
        self.assertEqual(self.env.state("A"), F.REVIEWING)

    def test_lease_exclusive_per_project_and_generation_monotonic(self):
        self.env.job("A", ordinal=1)
        self.env.job("B", ordinal=2)
        self.env.job("C", ordinal=1, batch="B2", project="other")
        la = self.fsm.acquire_lease("A", "w1", 60)
        with self.assertRaises(LeaseConflict):
            self.fsm.acquire_lease("B", "w2", 60)
        lc = self.fsm.acquire_lease("C", "w3", 60)  # different project: independent
        self.assertEqual(la.generation, 1)
        self.assertEqual(lc.generation, 1)
        self.fsm.transition("A", F.PREFLIGHT_FAILED, lease_token=la.token, lease_generation=la.generation)
        self.fsm.transition("A", F.BLOCKED)
        lb = self.fsm.acquire_lease("B", "w2", 60)
        self.assertEqual(lb.generation, 2)

    def test_stale_token_and_generation_rejected(self):
        self.env.job("A")
        lease = self.fsm.acquire_lease("A", "w1", 60)
        with self.assertRaises(StaleLeaseError):
            self.fsm.transition("A", F.PREFLIGHT_PASSED, lease_token="forged", lease_generation=lease.generation)
        with self.assertRaises(StaleLeaseError):
            self.fsm.transition("A", F.PREFLIGHT_PASSED, lease_token=lease.token, lease_generation=lease.generation + 7)
        with self.assertRaises(StaleLeaseError):
            self.fsm.transition("A", F.PREFLIGHT_PASSED)
        self.assertEqual(self.env.state("A"), F.LEASED)

    def test_expired_lease_blocks_writer_and_renew(self):
        self.env.job("A")
        lease = self.fsm.acquire_lease("A", "w1", 10)
        self.clock.advance(5)
        renewed = self.fsm.renew_lease("A", lease.token, lease.generation, 10)
        self.clock.advance(8)  # still alive thanks to renew
        self.assertTrue(self.fsm.check_lease("A", lease.token, lease.generation)[0])
        self.clock.advance(5)  # now expired
        self.assertFalse(self.fsm.check_lease("A", lease.token, lease.generation)[0])
        with self.assertRaises(StaleLeaseError):
            self.fsm.transition("A", F.PREFLIGHT_PASSED, lease_token=lease.token, lease_generation=lease.generation)
        with self.assertRaises(StaleLeaseError):
            self.fsm.renew_lease("A", lease.token, lease.generation)

    def test_idempotency_key_makes_transition_a_noop_replay(self):
        self.env.job("A")
        lease = self.fsm.acquire_lease("A", "w1", 60)
        r1 = self.fsm.transition("A", F.PREFLIGHT_PASSED, lease_token=lease.token, lease_generation=lease.generation,
                                 idempotency_key="idem-1")
        r2 = self.fsm.transition("A", F.PREFLIGHT_PASSED, lease_token=lease.token, lease_generation=lease.generation,
                                 idempotency_key="idem-1")
        self.assertTrue(r1.applied)
        self.assertTrue(r2.duplicate and not r2.applied)
        self.assertEqual(len([e for e in self.env.db.audit_events("A") if e["idempotency_key"] == "idem-1"]), 1)
        self.assertEqual(self.env.state("A"), F.PREFLIGHT_PASSED)

    def test_contract_payload_immutable_by_trigger(self):
        self.env.job("A")
        with self.assertRaises(sqlite3.DatabaseError):
            self.env.db.execute("UPDATE job_contracts SET contract_json='{}' WHERE job_id='A'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.env.db.execute("UPDATE job_contracts SET contract_sha256='x' WHERE job_id='A'")

    def test_contract_digest_ignores_approval_fields_only(self):
        c = make_contract("A")
        d1 = contract_digest(c)
        c2 = dict(c, approval_state="APPROVED", approved_by="x", approved_at="y", contract_sha256="z")
        self.assertEqual(d1, contract_digest(c2))
        self.assertNotEqual(d1, contract_digest(dict(c, goal="changed")))

    def test_duplicate_job_rejected(self):
        self.env.job("A")
        with self.assertRaises(Exception):
            self.env.job("A")


class ReconcilerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.env = Env(now_fn=self.clock)
        self.fsm = self.env.sup.fsm
        self.rec = self.env.sup.reconciler

    def tearDown(self):
        self.env.cleanup()

    def _crash_in_running(self, job_id):
        lease = self.fsm.acquire_lease(job_id, "dead-worker", 30)
        aid = self.fsm.create_attempt(job_id, worker_type="t", lease_token=lease.token, lease_generation=lease.generation)
        for s in (F.PREFLIGHT_PASSED, F.RUNNING):
            self.fsm.transition(job_id, s, lease_token=lease.token, lease_generation=lease.generation, attempt_id=aid)
        return lease, aid

    def test_live_lease_untouched(self):
        self.env.job("A")
        self._crash_in_running("A")
        self.assertEqual(self.rec.reconcile().findings, [])
        self.assertEqual(self.env.state("A"), F.RUNNING)

    def test_expired_lease_paused_fenced_never_accepted(self):
        self.env.job("A")
        lease, aid = self._crash_in_running("A")
        self.clock.advance(31)
        rep = self.rec.reconcile()
        self.assertEqual(rep.paused, ["A"])
        row = self.fsm.get("A")
        self.assertEqual(row["current_state"], F.PAUSED_NEEDS_RECONCILIATION)
        self.assertIsNone(row["lease_token"])
        self.assertGreater(row["lease_generation"], lease.generation)
        att = self.env.db.get_attempt(aid)
        self.assertEqual(att["worker_status"], "INTERRUPTED")
        self.assertEqual(att["failure_class"], "AMBIGUOUS")
        # the zombie writer holding the old token can no longer move the job anywhere
        for target in (F.QA_RUNNING, F.ACCEPTED, F.REVIEWING):
            with self.assertRaises(FSMError):
                self.fsm.transition("A", target, lease_token=lease.token, lease_generation=lease.generation)
        self.assertEqual(self.env.state("A"), F.PAUSED_NEEDS_RECONCILIATION)

    def test_restart_assume_all_stale(self):
        self.env.job("A")
        self._crash_in_running("A")
        rep = self.rec.reconcile(assume_all_stale=True)
        self.assertEqual(rep.paused, ["A"])

    def test_all_interruptible_states_handled(self):
        for jid, upto in (("L", [F.LEASED]), ("P", [F.LEASED, F.PREFLIGHT_PASSED]),
                          ("Q", [F.LEASED, F.PREFLIGHT_PASSED, F.RUNNING, F.QA_RUNNING])):
            c = make_contract(jid, 1, f"B-{jid}", f"pr-{jid}")
            self.env.sup.submit_contract(c, canonical_root=self.env.src)
            self.env.sup.approve_contract(jid, "t")
            lease = self.fsm.acquire_lease(jid, "w", 5)
            for s in upto[1:]:
                self.fsm.transition(jid, s, lease_token=lease.token, lease_generation=lease.generation)
        self.clock.advance(10)
        self.assertEqual(sorted(self.rec.reconcile().paused), ["L", "P", "Q"])

    def test_resolve_requeue_after_pause_allows_new_lease_with_higher_generation(self):
        self.env.job("A")
        lease, _ = self._crash_in_running("A")
        self.clock.advance(31)
        self.rec.reconcile()
        self.rec.resolve("A", "requeue")
        new = self.fsm.acquire_lease("A", "w2", 60)
        self.assertGreater(new.generation, lease.generation)

    def test_reconcile_is_idempotent(self):
        self.env.job("A")
        self._crash_in_running("A")
        self.clock.advance(31)
        self.rec.reconcile()
        self.assertEqual(self.rec.reconcile().findings, [])


if __name__ == "__main__":
    unittest.main()
