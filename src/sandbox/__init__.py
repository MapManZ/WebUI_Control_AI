"""Non-admin Windows sandbox primitives."""
from .path_guard import (  # noqa: F401
    PathViolation, PathPolicy, PolicyResult, validate_relative_path, resolve_in_root, is_reparse_point, is_within,
    scan_links, matches_any,
)
from .manifest_hasher import (  # noqa: F401
    build_manifest, manifest_digest, diff_manifests, changed_paths, sha256_file, fingerprint_tree,
    fingerprint_roots, verify_protected_untouched, anchor_manifest,
)
from .worktree_manager import WorktreeManager, AttemptSandbox, SandboxError  # noqa: F401
from .win_job_object import JobObject, run_bounded, ProcessResult, pid_alive, taskkill_tree  # noqa: F401
