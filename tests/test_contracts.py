"""Unit tests for Job Contracts, canonical SHA-256 integrity, and DAG validation."""

import json
import unittest
from pathlib import Path

from src.contracts.models import JobContract
from src.contracts.validator import (
    compute_contract_sha256,
    is_safe_relative_path,
    validate_contract,
    validate_backlog_dag,
)


class TestJobContracts(unittest.TestCase):
    """Test suite for JobContract data model and validators."""

    def setUp(self):
        self.sample_contract_data = {
            "schema_version": "1.0.0",
            "plan_id": "TEST-PLAN-01",
            "phase_id": "PHASE-0",
            "job_id": "TEST-001",
            "batch_id": "B1",
            "batch_ordinal": 1,
            "project_id": "WebUI_Control_AI",
            "workspace_root_ref": ".",
            "goal": "Verify test contract validation",
            "in_scope": ["src/test.py"],
            "out_of_scope": ["O:\\AI_Workspace\\REA Project"],
            "allowed_read_paths": ["src"],
            "allowed_write_paths": ["src/test.py", "docs/test.md"],
            "forbidden_write_roots": ["O:\\AI_Workspace\\REA Project"],
            "allowed_commands": ["python"],
            "dependencies": [],
            "acceptance_checks": [
                {"name": "Check", "command": "python --version", "expected_exit_code": 0}
            ],
            "required_evidence": ["test.log"],
            "max_rework_cycles": 3,
            "max_infra_retries": 2,
            "timeout_seconds": 180,
            "role_d_review_required": False,
            "approval_state": "APPROVED",
            "approved_by": "tester",
            "approved_at": "2026-11-13T01:00:00Z",
        }

    def test_job_contract_dataclass_roundtrip(self):
        contract = JobContract.from_dict(self.sample_contract_data)
        self.assertEqual(contract.job_id, "TEST-001")
        self.assertEqual(contract.batch_ordinal, 1)

        d = contract.to_dict()
        self.assertEqual(d["job_id"], "TEST-001")
        self.assertEqual(d["goal"], "Verify test contract validation")

    def test_canonical_sha256_determinism(self):
        d1 = dict(self.sample_contract_data)
        d2 = dict(self.sample_contract_data)
        # Reverse insertion order of keys
        d2_reversed = {k: d2[k] for k in reversed(list(d2.keys()))}

        h1 = compute_contract_sha256(d1)
        h2 = compute_contract_sha256(d2_reversed)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

        # Ensure contract_sha256 field is ignored
        d1["contract_sha256"] = "deadbeef" * 8
        h3 = compute_contract_sha256(d1)
        self.assertEqual(h1, h3)

    def test_validate_valid_contract(self):
        data = dict(self.sample_contract_data)
        data["contract_sha256"] = compute_contract_sha256(data)

        ok, errors = validate_contract(data)
        self.assertTrue(ok)
        self.assertEqual(len(errors), 0)

    def test_validate_missing_required_fields(self):
        data = dict(self.sample_contract_data)
        del data["goal"]
        del data["job_id"]
        data["contract_sha256"] = compute_contract_sha256(data)

        ok, errors = validate_contract(data)
        self.assertFalse(ok)
        self.assertTrue(any("Missing required field: 'goal'" in e for e in errors))
        self.assertTrue(any("Missing required field: 'job_id'" in e for e in errors))

    def test_validate_safe_relative_path(self):
        valid_paths = [
            "src/test.py",
            "docs/readme.md",
            "a/b/c/d.json",
            "file.txt",
        ]
        for p in valid_paths:
            ok, msg = is_safe_relative_path(p)
            self.assertTrue(ok, f"Expected valid: {p}, got {msg}")

        invalid_paths = [
            "../secret.txt",
            "foo/../../bar.txt",
            "C:\\Windows\\System32\\calc.exe",
            "D:/data/repo",
            "/etc/passwd",
            "\\Windows\\Temp",
            "\\\\server\\share\\file.txt",
            "//network/path",
            "",
            "   ",
        ]
        for p in invalid_paths:
            ok, msg = is_safe_relative_path(p)
            self.assertFalse(ok, f"Expected invalid: {p}")

    def test_validate_contract_rejects_unsafe_write_paths(self):
        data = dict(self.sample_contract_data)
        data["allowed_write_paths"] = ["../outside.txt", "C:\\evil.bat"]
        data["contract_sha256"] = compute_contract_sha256(data)

        ok, errors = validate_contract(data)
        self.assertFalse(ok)
        self.assertTrue(any("Parent traversal" in e for e in errors))
        self.assertTrue(any("Absolute drive path" in e for e in errors))

    def test_validate_contract_rejects_empty_acceptance_checks(self):
        data = dict(self.sample_contract_data)
        data["acceptance_checks"] = []
        data["contract_sha256"] = compute_contract_sha256(data)

        ok, errors = validate_contract(data)
        self.assertFalse(ok)
        self.assertTrue(any("acceptance_checks" in e for e in errors))

    def test_validate_contract_sha256_mismatch(self):
        data = dict(self.sample_contract_data)
        data["contract_sha256"] = "0" * 64

        ok, errors = validate_contract(data)
        self.assertFalse(ok)
        self.assertTrue(any("contract_sha256 mismatch" in e for e in errors))

    def test_validate_backlog_dag_valid(self):
        c1 = dict(self.sample_contract_data, job_id="J1", batch_id="B1", dependencies=[])
        c2 = dict(self.sample_contract_data, job_id="J2", batch_id="B1", dependencies=["J1"])
        c3 = dict(self.sample_contract_data, job_id="J3", batch_id="B2", dependencies=["J2"])

        ok, errors = validate_backlog_dag([c1, c2, c3])
        self.assertTrue(ok)
        self.assertEqual(len(errors), 0)

    def test_validate_backlog_dag_cycle_detection(self):
        c1 = dict(self.sample_contract_data, job_id="J1", batch_id="B1", dependencies=["J2"])
        c2 = dict(self.sample_contract_data, job_id="J2", batch_id="B1", dependencies=["J1"])

        ok, errors = validate_backlog_dag([c1, c2])
        self.assertFalse(ok)
        self.assertTrue(any("Dependency cycle detected" in e for e in errors))

    def test_validate_backlog_dag_missing_dependency(self):
        c1 = dict(self.sample_contract_data, job_id="J1", batch_id="B1", dependencies=["NONEXISTENT"])

        ok, errors = validate_backlog_dag([c1])
        self.assertFalse(ok)
        self.assertTrue(any("unknown dependency 'NONEXISTENT'" in e for e in errors))

    def test_validate_backlog_dag_g7_batch_gate_limit(self):
        # 3 jobs in B1 violates G7 Two-Job Batch Gate
        c1 = dict(self.sample_contract_data, job_id="J1", batch_id="B1", dependencies=[])
        c2 = dict(self.sample_contract_data, job_id="J2", batch_id="B1", dependencies=["J1"])
        c3 = dict(self.sample_contract_data, job_id="J3", batch_id="B1", dependencies=["J2"])

        ok, errors = validate_backlog_dag([c1, c2, c3])
        self.assertFalse(ok)
        self.assertTrue(any("G7 Two-Job Batch Gate" in e for e in errors))

    def test_actual_backlog_file_integrity(self):
        backlog_path = Path(__file__).resolve().parent.parent / "contracts" / "REA_ORCHESTRA_JOB_BACKLOG_V2.json"
        self.assertTrue(backlog_path.exists())

        with open(backlog_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        contracts = manifest["contracts"]
        self.assertGreaterEqual(len(contracts), 10)

        # Validate each individual contract
        for c in contracts:
            ok, errors = validate_contract(c)
            self.assertTrue(ok, f"Contract {c.get('job_id')} failed validation: {errors}")

        # Validate DAG and G7 Batch Gate
        ok_dag, dag_errors = validate_backlog_dag(contracts)
        self.assertTrue(ok_dag, f"Backlog DAG failed validation: {dag_errors}")


if __name__ == "__main__":
    unittest.main()
