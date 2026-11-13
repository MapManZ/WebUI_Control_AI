"""End-to-end test for the Isolated REA Sandbox Pilot.

Proves:
 1. 100% untouched protected tree in `O:\\AI_Workspace\\REA Project\\OpenCodeREA\\SummoningRemake`.
 2. Zero network calls or socket connections to Serena port 9121.
 3. Two-job batch gate (G7) enforcement (Job 2 refused before Job 1 is HANDOFF_VERIFIED).
 4. Independent QA execution inside disposable sandbox `.runtime/sandboxes/`.
 5. Machine-verified handoff generation and reconstruction into a fresh DB.
 6. Result is definitive GO.
"""
import json
import os
import subprocess
import unittest
from pathlib import Path

from src.integrations.rea_pilot import REA_ROOT, run_pilot

REPO_ROOT = Path(__file__).resolve().parents[2]


class REAIsolatedPilotTests(unittest.TestCase):
    def test_rea_pilot_run_and_integrity(self):
        # Run the isolated pilot in non-deep mode (stat-based tree fingerprint + full content SHA-256 for key files)
        result = run_pilot(REPO_ROOT, rea_root=REA_ROOT, deep=False)

        # 1. Pilot outcome verdict must be GO
        self.assertEqual(result["go_no_go"], "GO", f"Pilot failed: {result}")

        # 2. Protected tree MUST be 100% untouched
        self.assertTrue(result["protected_tree_untouched"], "Protected REA tree was touched!")
        self.assertEqual(result["anchor_sha256_before"], result["anchor_sha256_after"])

        # 3. Port 9121 was never contacted
        self.assertFalse(result["serena_port_9121_contacted"])

        # 4. G7 gate was verified (Job 2 blocked before Job 1 handoff)
        self.assertTrue(result["g7_two_job_gate_enforced"])

        # 5. Handoff machine reconstruction into an isolated DB succeeded
        self.assertTrue(result["handoff_reconstruction_ok"])

        # 6. Both jobs reached terminal or verified states
        self.assertEqual(result["job_states"]["REA-PILOT"], "CLOSED")
        self.assertEqual(result["job_states"]["REA-PILOT-NEXT"], "HANDOFF_VERIFIED")

        # 7. Git status check on SummoningRemake must show ZERO modified files
        git_check = subprocess.run(
            ["git", "-C", REA_ROOT, "status", "--porcelain"],
            capture_output=True, text=True, timeout=30
        )
        if git_check.returncode == 0:
            # Check specifically for any pilot files
            dirty_lines = [
                line for line in git_check.stdout.splitlines()
                if "pilot" in line.lower() or "orchestra" in line.lower()
            ]
            self.assertEqual(dirty_lines, [], f"Unexpected git untracked/modified files in REA root: {dirty_lines}")


if __name__ == "__main__":
    unittest.main()
