"""Strict PathGuard validation for active working directory selection."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from src.sandbox.path_guard import (
    PathViolation,
    is_reparse_point,
    is_within,
    matches_any,
    resolve_in_root,
    validate_relative_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "harnesses.json"
PROTECTED_ABS_ROOTS: List[str] = [
    r"O:\AI_Workspace\REA Project",
]
PROTECTED_REL_SUBPATHS: List[str] = [
    ".git",
    ".runtime/keys",
    ".runtime/state",
    ".runtime/evidence",
]


def get_protected_roots(config_path: Optional[Path | str] = None) -> List[str]:
    """Return protected absolute roots combining built-ins, config/harnesses.json, and ORCHESTRA_PROTECTED_ROOTS."""
    candidates: List[str] = list(PROTECTED_ABS_ROOTS)

    cfg_target = Path(config_path) if config_path else CONFIG_PATH
    if cfg_target.is_file():
        try:
            data = json.loads(cfg_target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw_roots = data.get("protected_roots")
                if isinstance(raw_roots, list):
                    candidates.extend(str(r) for r in raw_roots if str(r).strip())
                sec = data.get("security")
                if isinstance(sec, dict) and isinstance(sec.get("protected_roots"), list):
                    candidates.extend(str(r) for r in sec["protected_roots"] if str(r).strip())
        except Exception:
            pass

    env_val = os.environ.get("ORCHESTRA_PROTECTED_ROOTS", "").strip()
    if env_val:
        if env_val.startswith("["):
            try:
                parsed = json.loads(env_val)
                if isinstance(parsed, list):
                    candidates.extend(str(r) for r in parsed if str(r).strip())
            except Exception:
                candidates.extend(p for p in env_val.split(os.pathsep) if p.strip())
        else:
            candidates.extend(p for p in env_val.split(os.pathsep) if p.strip())

    deduped: List[str] = []
    seen: set[str] = set()
    for entry in candidates:
        expanded = os.path.expandvars(str(entry).strip())
        if not expanded:
            continue
        key = os.path.normcase(os.path.abspath(expanded))
        if key not in seen:
            seen.add(key)
            deduped.append(expanded)
    return deduped


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
    protected_abs = list(get_protected_roots()) + list(extra_protected_roots or [])

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
        real_prot = os.path.realpath(os.path.abspath(prot))
        if is_within(resolved, prot) or is_within(resolved, real_prot):
            raise PathViolation("PROTECTED_ROOT", f"Access to protected root is forbidden: {prot}")

    if not resolved.is_dir():
        raise PathViolation("NOT_A_DIRECTORY", f"Working directory does not exist or is not a directory: {raw}")

    return {
        "working_directory": str(resolved),
        "workspace_root": str(real_root),
        "relative_path": rel_norm,
    }


def validate_sandbox_dir(
    candidate: str | os.PathLike,
    *,
    workspace_root: Path | str = REPO_ROOT,
    require_sandbox_subroot: bool = False,
    create: bool = False,
    extra_protected_roots: Optional[List[str]] = None,
) -> Path:
    """Validate a worker/job sandbox directory before creating or using it.

    Rejects raw ``..`` traversal, UNC/NUL paths, ``workspace_root`` itself and its ancestors,
    protected roots/subpaths, and symlink/junction escapes. When ``require_sandbox_subroot`` is
    True, requires the target to be strictly inside ``<workspace_root>/.runtime/sandboxes``
    or ``tempfile.gettempdir()``.
    """
    raw_input = str(candidate) if candidate is not None else ""
    if not raw_input.strip():
        raise PathViolation("EMPTY_PATH", "Sandbox directory must be a non-empty path")
    raw = raw_input.strip()
    if "\x00" in raw:
        raise PathViolation("NUL_BYTE", "NUL byte in sandbox directory path")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise PathViolation("UNC_PATH", f"UNC paths are not allowed: {raw}")
    if any(part == ".." for part in re.split(r"[\\/]", raw)):
        raise PathViolation("PATH_TRAVERSAL", f"Parent traversal ('..') is not allowed in sandbox_dir: {raw}")

    root_real = Path(os.path.realpath(os.path.abspath(str(workspace_root))))
    temp_real = Path(os.path.realpath(os.path.abspath(tempfile.gettempdir())))
    sandboxes_real = Path(os.path.realpath(os.path.abspath(str(root_real / ".runtime" / "sandboxes"))))

    is_abs = os.path.isabs(raw) or (len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha())
    abs_cand = Path(os.path.abspath(raw)) if is_abs else Path(os.path.abspath(str(root_real / raw)))
    real_cand = Path(os.path.realpath(abs_cand))

    if abs_cand == root_real or real_cand == root_real:
        raise PathViolation("PROTECTED_ROOT", f"Sandbox directory cannot be repository root: {root_real}")
    if is_within(root_real, abs_cand) or is_within(root_real, real_cand):
        raise PathViolation("PROTECTED_ROOT", f"Sandbox directory cannot be an ancestor of repository root: {raw}")

    protected_abs = list(get_protected_roots()) + list(extra_protected_roots or [])
    for prot in protected_abs:
        real_prot = os.path.realpath(os.path.abspath(prot))
        if (
            is_within(abs_cand, prot)
            or is_within(real_cand, prot)
            or is_within(abs_cand, real_prot)
            or is_within(real_cand, real_prot)
        ):
            raise PathViolation("PROTECTED_ROOT", f"Access to protected root is forbidden: {prot}")

    if require_sandbox_subroot:
        in_sandboxes = (
            real_cand != sandboxes_real
            and is_within(abs_cand, sandboxes_real)
            and is_within(real_cand, sandboxes_real)
        )
        in_temp = (
            real_cand != temp_real
            and is_within(abs_cand, temp_real)
            and is_within(real_cand, temp_real)
        )
        if in_sandboxes:
            allowed_root = root_real
        elif in_temp:
            allowed_root = temp_real
        else:
            raise PathViolation(
                "OUTSIDE_WORKSPACE",
                f"Sandbox directory must be inside {sandboxes_real} or {temp_real}: {raw}",
            )
    else:
        in_workspace = (
            real_cand != root_real
            and is_within(abs_cand, root_real)
            and is_within(real_cand, root_real)
        )
        in_temp = (
            real_cand != temp_real
            and is_within(abs_cand, temp_real)
            and is_within(real_cand, temp_real)
        )
        if in_workspace:
            allowed_root = root_real
        elif in_temp:
            allowed_root = temp_real
        else:
            raise PathViolation(
                "OUTSIDE_WORKSPACE",
                f"Sandbox directory must be inside {root_real} or {temp_real}: {raw}",
            )

    rel = os.path.relpath(str(real_cand), str(allowed_root))
    rel_norm = validate_relative_path(rel)
    if allowed_root == root_real and matches_any(rel_norm, PROTECTED_REL_SUBPATHS):
        raise PathViolation(
            "PROTECTED_PATH",
            f"Protected internal directory cannot be used as sandbox: {rel_norm}",
        )

    # Reject reparse points on any existing component (from abs_cand) before mkdir
    abs_rel = os.path.relpath(str(abs_cand), str(allowed_root)).replace("\\", "/")
    cur = allowed_root
    for part in [p for p in abs_rel.split("/") if p not in ("", ".")]:
        cur = cur / part
        if os.path.lexists(cur) and is_reparse_point(cur):
            target = os.path.realpath(cur)
            if not is_within(target, allowed_root):
                raise PathViolation("LINK_ESCAPE", f"{abs_rel} -> {target}")
            raise PathViolation("LINK_IN_SANDBOX", f"{abs_rel} contains a symlink/junction")

    if create and not real_cand.exists():
        real_cand.mkdir(parents=True, exist_ok=True)

    info = validate_workdir(str(real_cand), workspace_root=allowed_root, extra_protected_roots=extra_protected_roots)
    return Path(info["working_directory"])
