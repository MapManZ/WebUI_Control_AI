"""Data models for Job Contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, List, Dict


@dataclass
class JobContract:
    """Immutable Job Contract defining bounds, dependencies, and QA acceptance criteria."""

    schema_version: str
    plan_id: str
    phase_id: str
    job_id: str
    batch_id: str
    batch_ordinal: int
    project_id: str
    workspace_root_ref: str
    goal: str
    in_scope: List[str] = field(default_factory=list)
    out_of_scope: List[str] = field(default_factory=list)
    allowed_read_paths: List[str] = field(default_factory=list)
    allowed_write_paths: List[str] = field(default_factory=list)
    forbidden_write_roots: List[str] = field(default_factory=list)
    allowed_commands: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    acceptance_checks: List[Any] = field(default_factory=list)
    required_evidence: List[str] = field(default_factory=list)
    max_rework_cycles: int = 3
    max_infra_retries: int = 2
    timeout_seconds: int = 180
    role_d_review_required: bool = False
    approval_state: str = "APPROVED"
    approved_by: str = ""
    approved_at: str = ""
    contract_sha256: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert contract to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> JobContract:
        """Construct JobContract from dictionary, handling default fields."""
        fields = {
            "schema_version": data.get("schema_version", "1.0.0"),
            "plan_id": data.get("plan_id", ""),
            "phase_id": data.get("phase_id", ""),
            "job_id": data.get("job_id", ""),
            "batch_id": data.get("batch_id", ""),
            "batch_ordinal": int(data.get("batch_ordinal", 1)),
            "project_id": data.get("project_id", ""),
            "workspace_root_ref": data.get("workspace_root_ref", "."),
            "goal": data.get("goal", ""),
            "in_scope": list(data.get("in_scope", [])),
            "out_of_scope": list(data.get("out_of_scope", [])),
            "allowed_read_paths": list(data.get("allowed_read_paths", [])),
            "allowed_write_paths": list(data.get("allowed_write_paths", [])),
            "forbidden_write_roots": list(data.get("forbidden_write_roots", [])),
            "allowed_commands": list(data.get("allowed_commands", [])),
            "dependencies": list(data.get("dependencies", [])),
            "acceptance_checks": list(data.get("acceptance_checks", [])),
            "required_evidence": list(data.get("required_evidence", [])),
            "max_rework_cycles": int(data.get("max_rework_cycles", 3)),
            "max_infra_retries": int(data.get("max_infra_retries", 2)),
            "timeout_seconds": int(data.get("timeout_seconds", 180)),
            "role_d_review_required": bool(data.get("role_d_review_required", False)),
            "approval_state": data.get("approval_state", "APPROVED"),
            "approved_by": data.get("approved_by", ""),
            "approved_at": data.get("approved_at", ""),
            "contract_sha256": data.get("contract_sha256", ""),
        }
        return cls(**fields)
