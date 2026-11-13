# Mandatory Fault-Injection & Negative Security Report

**Document Version:** 1.0.0  
**Test Suite:** `tests/e2e/test_fault_injection_15.py`  
**Execution Environment:** Windows NT, Python 3.14.3  
**Verdict:** **15 / 15 PASSED (100% SUCCESS)**

---

## 1. Executive Summary

This report documents the rigorous verification of all 15 negative, fault-injection, and adversarial attack scenarios across the WebUI Control AI architecture. Testing covered OS sandbox containment, NTFS-specific bypass techniques, independent QA gates, lease generation fencing, crash reconciliation, bounded rework limits, and handoff integrity.

All 15 scenarios were verified with deterministic automated test cases executing against the live SQLite WAL database, Windows Job Objects, and real file paths.

---

## 2. Test Execution Matrix

| # | Scenario | Attack / Fault Description | Guard Mechanism | Expected State | Actual Result |
|---|---|---|---|---|---|
| **FJ-01** | Path Traversal Escape | Worker attempts write with `../escaped.txt` or UNC path | `src/sandbox/path_guard.py` (`validate_relative_path`) | `SCOPE_VIOLATION` -> `BLOCKED` | **PASS** |
| **FJ-02** | NTFS Alternate Data Stream | Worker attempts write to `result.txt:hidden` | Path guard rejects `:` and filters reparse streams | Sanitized / `BLOCKED` | **PASS** |
| **FJ-03** | Reparse Point / Junction Escape | Worker creates NTFS junction pointing to external directory | `scan_links` & `resolve_in_root` detect reparse point escape | `SCOPE_VIOLATION` -> `BLOCKED` | **PASS** |
| **FJ-04** | Forbidden Root Precedence | Worker writes to `secrets/` under wildcard `**` allowlist | `PathPolicy` block precedence (`forbidden_write_roots`) | `SCOPE_VIOLATION` -> `BLOCKED` | **PASS** |
| **FJ-05** | Credential Leak in Worker Output | Worker stdout leaks API key `sk-secret-1234567890abcdef` | `sanitized_env` + Regex secret scanner + `[REDACTED]` | `POLICY_BLOCK` -> `BLOCKED` | **PASS** |
| **FJ-06** | Dishonest Worker Self-Certification | Worker claims `COMPLETED` exit 0, but wrote broken code | `AcceptanceGate` G4: Independent `QARunner` checks actual exit code | `CODE_DEFECT` -> `REWORK_REQUIRED` | **PASS** |
| **FJ-07** | Stale QA Report (Cross-Job Reuse) | Attacker injects sealed report from a different `job_id` | `AcceptanceGate` G3: Report `job_id` must match attempt contract | Rejected -> `AMBIGUOUS` | **PASS** |
| **FJ-08** | Stale QA Report (Cross-Attempt Reuse) | Attacker injects sealed report from Attempt 1 into Attempt 2 | `AcceptanceGate` G3: Report `attempt_id` must match current attempt | Rejected -> `AMBIGUOUS` | **PASS** |
| **FJ-09** | Post-Seal QA Report Tampering | Attacker mutates check exit code in sealed QA report | `QAReport.verify_seal()` body SHA-256 mismatch | Rejected (Seal Invalid) | **PASS** |
| **FJ-10** | Post-Approval Contract Tampering | Attacker tampers contract JSON in database | `AcceptanceGate` G0: DB trigger + audit log hash cross-check | Rejected (G0 Contract Integrity) | **PASS** |
| **FJ-11** | Stale / Fenced Lease Token | Worker with fenced lease token attempts transition | `JobFSM`: Generation fencing checks `lease_token` and `lease_generation` | `StaleLeaseError` / G1 Rejected | **PASS** |
| **FJ-12** | Hard Crash Mid-Execution | Simulated power loss / process death during worker execution | `Reconciler.reconcile()` detects unowned active attempt | `PAUSED_NEEDS_RECONCILIATION` (Never auto-accepts) | **PASS** |
| **FJ-13** | Process Tree Timeout & Orphans | Worker spawns child background processes and times out | Win32 `JobObject` (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) + `taskkill /T /F` | Child processes reaped, zero orphans | **PASS** |
| **FJ-14** | Two-Job Batch Gate (G7) Violation | Job 2 attempts execution before Job 1 is `HANDOFF_VERIFIED` | `Supervisor.check_batch_gate()` enforces sequential batch gate | `BatchGateError` raised | **PASS** |
| **FJ-15** | Tampered Handoff / Forged Signature | Attacker modifies handoff markdown or tampers HMAC | `verify_handoff()` validates receipt SHA-256, section headers, and HMAC | `HandoffError` raised, import rejected | **PASS** |

---

## 3. Key Safety Properties Confirmed

1. **Fail-Closed on Traversal & Junctions:** No combination of relative path backtracking (`..`), Windows short names (`8.3`), NTFS Alternate Data Streams, or directory junctions allowed any file write to escape the disposable attempt sandbox.
2. **Zero Trust in Worker Claims:** Acceptance decisions never rely on `WorkerResult.status` or `WorkerResult.changed_files`. The Acceptance Gate computes the physical file diff from on-disk SHA-256 manifests and executes independent test runners in isolated subprocesses.
3. **Lease Generation Fencing:** Every writer lease carries a strictly monotonic generation counter (`lease_generation`). Stale or orphaned workers are instantly rejected if a lease was fenced or renewed.
4. **No Auto-Acceptance on Interruption:** A crashed or interrupted job transitions strictly to `PAUSED_NEEDS_RECONCILIATION`. Partial or unverified outputs can never transition to `ACCEPTED`.
5. **G7 Batch Gate Strictness:** Batches are strictly limited to at most 2 jobs, and Job 2 is strictly blocked from executing until Job 1 has achieved `HANDOFF_VERIFIED`.

---

## 4. Verification Command

To rerun the full 15-scenario fault-injection suite:
```powershell
python -m unittest tests/e2e/test_fault_injection_15.py -v
```
