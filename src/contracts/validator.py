"""Validators for Job Contracts and Backlog DAGs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import PureWindowsPath, PurePosixPath
from typing import Any, Dict, List, Tuple, Union

from src.contracts.models import JobContract


def compute_contract_sha256(contract: Union[JobContract, Dict[str, Any]]) -> str:
    """Compute canonical SHA-256 digest of contract dictionary excluding contract_sha256."""
    if isinstance(contract, JobContract):
        data = contract.to_dict()
    else:
        data = dict(contract)

    # Exclude contract_sha256 field from canonical digest
    data.pop("contract_sha256", None)

    canonical_json = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def is_safe_relative_path(path_str: str) -> Tuple[bool, str]:
    """Verify that a path is strictly relative and does not escape workspace sandbox."""
    if not path_str or not path_str.strip():
        return False, "Path is empty"

    clean_path = path_str.strip()

    # Reject Windows drive letters (e.g. C:, D:)
    if re.match(r"^[a-zA-Z]:", clean_path):
        return False, f"Absolute drive path rejected: {path_str}"

    # Reject UNC paths (e.g. \\server\share or //server/share)
    if clean_path.startswith("\\\\") or clean_path.startswith("//"):
        return False, f"UNC path rejected: {path_str}"

    # Reject absolute root slashes (/ or \)
    if clean_path.startswith("/") or clean_path.startswith("\\"):
        return False, f"Root-absolute path rejected: {path_str}"

    # Normalize parts to inspect for parent directory traversal
    win_parts = PureWindowsPath(clean_path).parts
    posix_parts = PurePosixPath(clean_path).parts

    if ".." in win_parts or ".." in posix_parts:
        return False, f"Parent traversal ('..') rejected: {path_str}"

    return True, ""


def validate_contract(contract: Union[JobContract, Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """Validate a single JobContract against constraints and integrity requirements."""
    if isinstance(contract, JobContract):
        data = contract.to_dict()
    else:
        data = dict(contract)

    errors: List[str] = []

    # Required string fields
    required_fields = [
        "schema_version",
        "plan_id",
        "phase_id",
        "job_id",
        "batch_id",
        "project_id",
        "workspace_root_ref",
        "goal",
    ]
    for field in required_fields:
        val = data.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(f"Missing required field: '{field}'")

    # Validate allowed_write_paths strictly relative and safe
    write_paths = data.get("allowed_write_paths", [])
    if not isinstance(write_paths, list):
        errors.append("'allowed_write_paths' must be a list")
    else:
        for wp in write_paths:
            ok, reason = is_safe_relative_path(str(wp))
            if not ok:
                errors.append(f"Invalid write path '{wp}': {reason}")

    # Validate acceptance_checks is non-empty
    acceptance_checks = data.get("acceptance_checks", [])
    if not isinstance(acceptance_checks, list) or len(acceptance_checks) == 0:
        errors.append("Contract must have at least one acceptance check in 'acceptance_checks'")

    # Validate contract_sha256 integrity
    contract_sha256 = data.get("contract_sha256", "")
    expected_sha256 = compute_contract_sha256(data)
    if not contract_sha256:
        errors.append(f"Missing 'contract_sha256'; expected: '{expected_sha256}'")
    elif contract_sha256 != expected_sha256:
        errors.append(
            f"contract_sha256 mismatch for job '{data.get('job_id')}': "
            f"got '{contract_sha256}', expected '{expected_sha256}'"
        )

    return len(errors) == 0, errors


def validate_backlog_dag(contracts: List[Union[JobContract, Dict[str, Any]]]) -> Tuple[bool, List[str]]:
    """Validate backlog dependency graph (DAG) for cycles, dangling dependencies, and G7 batch limits."""
    errors: List[str] = []
    job_ids: set[str] = set()
    job_map: Dict[str, Dict[str, Any]] = {}
    batch_counts: Dict[str, int] = {}

    for c in contracts:
        data = c.to_dict() if isinstance(c, JobContract) else dict(c)
        jid = data.get("job_id", "").strip()
        if not jid:
            errors.append("Contract in backlog has empty 'job_id'")
            continue
        if jid in job_ids:
            errors.append(f"Duplicate job_id in backlog: '{jid}'")
        job_ids.add(jid)
        job_map[jid] = data

        batch_id = data.get("batch_id", "default")
        batch_counts[batch_id] = batch_counts.get(batch_id, 0) + 1

    # Check G7 Two-Job Batch Gate: maximum 2 jobs per batch
    for batch_id, count in sorted(batch_counts.items()):
        if count > 2:
            errors.append(
                f"Batch '{batch_id}' violates G7 Two-Job Batch Gate with {count} jobs (maximum allowed is 2)"
            )

    # Check for dangling / missing dependencies
    graph: Dict[str, List[str]] = {jid: [] for jid in job_ids}
    in_degree: Dict[str, int] = {jid: 0 for jid in job_ids}

    for jid, data in job_map.items():
        deps = data.get("dependencies", [])
        for dep in deps:
            if dep not in job_ids:
                errors.append(f"Job '{jid}' depends on unknown dependency '{dep}'")
            else:
                graph[dep].append(jid)
                in_degree[jid] += 1

    # Cycle detection via Kahn's algorithm (topological sort)
    queue = [jid for jid, deg in in_degree.items() if deg == 0]
    visited_count = 0

    while queue:
        curr = queue.pop(0)
        visited_count += 1
        for neighbor in graph[curr]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited_count < len(job_ids):
        # Find which jobs participate in cycles
        unresolved = [jid for jid, deg in in_degree.items() if deg > 0]
        errors.append(f"Dependency cycle detected in backlog DAG among jobs: {unresolved}")

    return len(errors) == 0, errors
