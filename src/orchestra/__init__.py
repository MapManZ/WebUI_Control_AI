"""Supervisor, repair loop and failure classification."""
from .repair import (  # noqa: F401
    RepairController, RepairDecision, RepairViolation, classify_failure, FAILURE_CLASSES, CODE_DEFECT, TEST_DEFECT,
    INFRA_TRANSIENT, TOOL_UNAVAILABLE, SCOPE_VIOLATION, POLICY_BLOCK, AMBIGUOUS,
)
from .supervisor import Supervisor, AttemptOutcome, BatchGateError, ContractRejected, SimulatedCrash  # noqa: F401
