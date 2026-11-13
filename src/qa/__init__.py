"""Independent QA runner and deterministic acceptance gate."""
from .runner import (  # noqa: F401
    QARunner, QAReport, CheckResult, redact, contains_secret, normalize_check, contract_check_ids, sanitized_env,
    find_browser, command_allowed,
)
from .gate import AcceptanceGate, GateResult, evidence_sha256, ACCEPTED, REJECTED, NOT_VERIFIED  # noqa: F401
from .role_d_adapter import RoleDAdapter, build_review_request, verify_review, seal_review  # noqa: F401
