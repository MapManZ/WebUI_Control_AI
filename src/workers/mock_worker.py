"""Mock worker implementation for deterministic test simulation and fault injection."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.workers.base import (
    WorkerAdapter,
    WorkerResult,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    STATUS_CANCELLED,
    STATUS_BLOCKED_TOOL_UNAVAILABLE,
)


class MockWorker(WorkerAdapter):
    """Deterministic worker used for acceptance tests, negative testing, and fault injection."""

    def __init__(self, mode: Optional[str] = None):
        self.default_mode = mode
        self._cancelled_attempts: set[str] = set()

    def capabilities(self) -> Dict[str, Any]:
        return {
            "worker_type": "mock",
            "supported_modes": [
                "pass",
                "fail",
                "fake_pass_with_bad_code",
                "scope_violation",
                "leak_secret",
                "timeout",
                "cancelled",
                "tool_unavailable",
            ],
            "sandboxed": True,
        }

    def cancel(self, attempt_id: str) -> bool:
        self._cancelled_attempts.add(attempt_id)
        return True

    def start(self, job_contract: Any, sandbox_dir: str, attempt_id: str, **kwargs: Any) -> WorkerResult:
        start_time = time.time()
        sandbox = Path(sandbox_dir)
        sandbox.mkdir(parents=True, exist_ok=True)

        # Normalize contract fields whether dict or object
        if hasattr(job_contract, "to_dict"):
            c_dict = job_contract.to_dict()
        elif isinstance(job_contract, dict):
            c_dict = job_contract
        else:
            c_dict = vars(job_contract) if hasattr(job_contract, "__dict__") else {}

        job_id = str(c_dict.get("job_id", kwargs.get("job_id", "mock-job-001")))
        allowed_write_paths: List[str] = list(c_dict.get("allowed_write_paths", ["result.txt"]))
        
        # Determine mode: argument > contract field > default > "pass"
        mode = kwargs.get("mode") or c_dict.get("mock_behavior") or self.default_mode or "pass"

        if attempt_id in self._cancelled_attempts or mode == "cancelled":
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type="mock",
                status=STATUS_CANCELLED,
                exit_code=130,
                duration_ms=duration_ms,
                stdout="Job cancelled by operator.",
            )

        if mode == "timeout":
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type="mock",
                status=STATUS_TIMEOUT,
                exit_code=124,
                duration_ms=duration_ms,
                stdout="Simulated timeout reached.",
                stderr="Timeout expired after configured deadline.",
            )

        if mode == "tool_unavailable":
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type="mock",
                status=STATUS_BLOCKED_TOOL_UNAVAILABLE,
                exit_code=127,
                duration_ms=duration_ms,
                stdout="",
                stderr="Requested tool or binary not available in environment.",
            )

        if mode == "leak_secret":
            # Writes allowed file, but stdout leaks a credential to verify secret redaction
            target_rel = allowed_write_paths[0] if allowed_write_paths else "output.txt"
            target_file = sandbox / target_rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text("ok with leak", encoding="utf-8")
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type="mock",
                status=STATUS_COMPLETED,
                changed_files=[target_rel],
                stdout="Processing job with API key sk-secret-1234567890abcdef completed successfully.",
                exit_code=0,
                duration_ms=duration_ms,
            )

        if mode == "scope_violation":
            # Attempt to touch a file outside allowed_write_paths inside or outside sandbox
            unauthorized_rel = "unauthorized_scope_probe.txt"
            target_file = sandbox / unauthorized_rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text("unauthorized write content", encoding="utf-8")
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type="mock",
                status=STATUS_COMPLETED,
                changed_files=[unauthorized_rel],
                stdout=f"Wrote file outside allowed scope: {unauthorized_rel}",
                exit_code=0,
                duration_ms=duration_ms,
            )

        if mode == "fake_pass_with_bad_code":
            # Claims COMPLETED and exit_code 0, but wrote deliberately broken code
            # so that independent AcceptanceGate / test will fail!
            target_rel = allowed_write_paths[0] if allowed_write_paths else "solution.py"
            target_file = sandbox / target_rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(
                "# Deliberately buggy code that will fail verification tests\n"
                "def solve():\n"
                "    raise RuntimeError('Broken implementation from unverified worker')\n",
                encoding="utf-8"
            )
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type="mock",
                status=STATUS_COMPLETED,
                changed_files=[target_rel],
                stdout="Self-certified SUCCESS: All tasks completed cleanly.",
                exit_code=0,
                duration_ms=duration_ms,
                metadata={"self_certified": True, "intentional_defect": True},
            )

        if mode == "fail":
            target_rel = allowed_write_paths[0] if allowed_write_paths else "error.log"
            target_file = sandbox / target_rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text("Error encountered during task execution.\n", encoding="utf-8")
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type="mock",
                status=STATUS_FAILED,
                changed_files=[target_rel],
                stdout="",
                stderr="Mock failure simulated.",
                exit_code=1,
                duration_ms=duration_ms,
            )

        # Default: "pass"
        target_rel = allowed_write_paths[0] if allowed_write_paths else "result.txt"
        target_file = sandbox / target_rel
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(f"Mock worker completed job {job_id} successfully.\n", encoding="utf-8")
        duration_ms = int((time.time() - start_time) * 1000)
        return WorkerResult(
            job_id=job_id,
            attempt_id=attempt_id,
            worker_type="mock",
            status=STATUS_COMPLETED,
            changed_files=[target_rel],
            stdout=f"Job {job_id} successfully completed in mock mode.",
            exit_code=0,
            duration_ms=duration_ms,
            metadata={"mock_behavior": "pass"},
        )
