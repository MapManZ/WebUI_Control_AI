"""Strict PathGuard validation for active working directory selection."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

from src.sandbox.path_guard import (
    PathViolation,
    is_within,
    matches_any,
    resolve_in_root,
    validate_relative_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROTECTED_ABS_ROOTS: List[str] = [
    r"O:\AI_Workspace\REA Project",
]
PROTECTED_REL_SUBPATHS: List[str] = [
    ".git",
    ".runtime/keys",
    ".runtime/state",
    ".runtime/evidence",
]


def validate_workdir(
    candidate: str,
    workspace_root: Path | str = REPO_ROOT,
    extra_protected_roots: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Validate candidate working directory strictly within workspace_root and outside protected paths."""
    if not isinstance(candidate, str) or not candidate.strip():
        raise PathViolation("EMPTY_PATH", "Working directory must be a non-empty path")
    raw = candidate.strip()
    if "\x00" in raw:
        raise PathViolation("NUL_BYTE", "NUL byte in working directory path")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise PathViolation("UNC_PATH", f"UNC paths are not allowed: {raw}")

    real_root = Path(os.path.realpath(os.path.abspath(str(workspace_root))))
    protected_abs = list(PROTECTED_ABS_ROOTS) + list(extra_protected_roots or [])

    if raw in (".", "./", ".\\"):
        resolved = real_root
        rel_norm = "."
    elif os.path.isabs(raw) or (len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha()):
        abs_cand = os.path.abspath(raw)
        real_cand = os.path.realpath(abs_cand)
        for prot in protected_abs:
            if is_within(abs_cand, prot) or is_within(real_cand, prot):
                raise PathViolation("PROTECTED_ROOT", f"Access to protected root is forbidden: {prot}")
        if not is_within(abs_cand, real_root) or not is_within(real_cand, real_root):
            raise PathViolation(
                "OUTSIDE_WORKSPACE",
                f"Working directory must stay inside workspace root ({real_root}): {raw}",
            )
        rel = os.path.relpath(real_cand, str(real_root))
        if rel == ".":
            resolved = real_root
            rel_norm = "."
        else:
            rel_norm = validate_relative_path(rel)
            resolved = resolve_in_root(real_root, rel_norm, must_exist=True)
    else:
        rel_norm = validate_relative_path(raw)
        resolved = resolve_in_root(real_root, rel_norm, must_exist=True)

    if rel_norm != "." and matches_any(rel_norm, PROTECTED_REL_SUBPATHS):
        raise PathViolation(
            "PROTECTED_PATH",
            f"Protected internal directory cannot be selected as working directory: {rel_norm}",
        )

    for prot in protected_abs:
        if is_within(resolved, prot):
            raise PathViolation("PROTECTED_ROOT", f"Access to protected root is forbidden: {prot}")

    if not resolved.is_dir():
        raise PathViolation("NOT_A_DIRECTORY", f"Working directory does not exist or is not a directory: {raw}")

    return {
        "working_directory": str(resolved),
        "workspace_root": str(real_root),
        "relative_path": rel_norm,
    }
