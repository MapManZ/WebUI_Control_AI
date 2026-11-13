"""Canonical path validation for the Windows non-admin sandbox.

Rejects: ``..`` traversal, absolute paths in relative allowlists, drive letters / cross-drive
paths, UNC (``\\\\server\\share``, ``\\\\?\\``), NTFS alternate data streams (``a.txt:s``),
Windows aliasing (trailing dot/space, reserved device names, 8.3 short names), case-aliasing
bypasses (all comparisons are case-folded) and symlinks/junctions (reparse points) that resolve
outside the sandbox root.
"""
from __future__ import annotations

import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
}
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SHORT_NAME_RE = re.compile(r"~\d")


class PathViolation(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _fold(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def validate_relative_path(rel: str, *, allow_glob: bool = False) -> str:
    """Validate an untrusted *relative* path; returns normalized posix form (case preserved)."""
    if not isinstance(rel, str) or not rel.strip():
        raise PathViolation("EMPTY_PATH", "empty path")
    if "\x00" in rel:
        raise PathViolation("NUL_BYTE", "NUL byte in path")
    if rel.startswith("\\\\") or rel.startswith("//"):
        raise PathViolation("UNC_PATH", rel)
    if rel.startswith("\\") or rel.startswith("/"):
        raise PathViolation("ABSOLUTE_PATH", rel)
    if _DRIVE_RE.match(rel):
        raise PathViolation("DRIVE_PATH", rel)
    if ":" in rel:
        raise PathViolation("ALTERNATE_DATA_STREAM", f"colon in path: {rel}")
    if any(ch in rel for ch in '<>"|') or (not allow_glob and any(ch in rel for ch in "*?")):
        raise PathViolation("ILLEGAL_CHARACTER", rel)
    norm = rel.replace("\\", "/")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if not parts:
        raise PathViolation("EMPTY_PATH", rel)
    for p in parts:
        if p == "..":
            raise PathViolation("PATH_TRAVERSAL", rel)
        if p.endswith(".") or p.endswith(" "):
            raise PathViolation("WINDOWS_ALIAS", f"trailing dot/space in component {p!r}")
        stem = p.split(".")[0].upper()
        if stem in _RESERVED:
            raise PathViolation("RESERVED_DEVICE_NAME", p)
        if _SHORT_NAME_RE.search(p):
            raise PathViolation("SHORT_NAME_ALIAS", p)
    return "/".join(parts)


def is_reparse_point(path: str | os.PathLike) -> bool:
    """True for symlinks and NTFS junctions / any reparse point (uses lstat, never follows)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    try:
        return bool(os.path.isjunction(path))  # py3.12+
    except Exception:
        return False


def is_within(path: str | os.PathLike, root: str | os.PathLike) -> bool:
    p = os.path.normcase(os.path.abspath(str(path)))
    r = os.path.normcase(os.path.abspath(str(root)))
    if p == r:
        return True
    return p.startswith(r.rstrip("\\/") + os.sep)


def resolve_in_root(root: str | os.PathLike, rel: str, *, must_exist: bool = False) -> Path:
    """Resolve ``rel`` under ``root``, refusing escapes through links / junctions / traversal."""
    norm = validate_relative_path(rel)
    root_abs = Path(os.path.abspath(str(root)))
    real_root = Path(os.path.realpath(root_abs))
    cur = root_abs
    for part in norm.split("/"):
        cur = cur / part
        if os.path.lexists(cur) and is_reparse_point(cur):
            target = Path(os.path.realpath(cur))
            if not is_within(target, real_root):
                raise PathViolation("LINK_ESCAPE", f"{rel} -> {target}")
            raise PathViolation("LINK_IN_SANDBOX", f"{rel} is a symlink/junction")
    resolved = Path(os.path.realpath(root_abs / norm))
    if not is_within(resolved, real_root):
        raise PathViolation("ESCAPES_ROOT", f"{rel} -> {resolved}")
    if must_exist and not resolved.exists():
        raise PathViolation("MISSING", rel)
    return resolved


def scan_links(root: str | os.PathLike) -> list[dict]:
    """Return every symlink/junction below root with an ``escapes`` flag (never follows them)."""
    found: list[dict] = []
    real_root = os.path.realpath(str(root))
    for dp, dns, fns in os.walk(str(root), followlinks=False):
        for name in list(dns) + list(fns):
            full = os.path.join(dp, name)
            if is_reparse_point(full):
                target = os.path.realpath(full)
                found.append({
                    "path": os.path.relpath(full, str(root)).replace("\\", "/"),
                    "target": target,
                    "escapes": not is_within(target, real_root),
                })
        # do not descend into links
        dns[:] = [d for d in dns if not is_reparse_point(os.path.join(dp, d))]
    return found


def glob_to_regex(pattern: str) -> re.Pattern:
    """Glob -> regex. ``**`` crosses directories, ``*``/``?`` do not. Trailing ``/`` means subtree."""
    pat = pattern.replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]
    if pat.endswith("/"):
        pat += "**"
    out = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i : i + 2] == "**":
                i += 2
                if pat[i : i + 1] == "/":
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def matches_any(rel: str, patterns: list[str]) -> bool:
    key = _fold(rel.replace("\\", "/"))
    for p in patterns:
        if glob_to_regex(_fold(p)).match(key):
            return True
        # a bare directory name or exact prefix means the whole subtree
        base = _fold(p.replace("\\", "/")).rstrip("/")
        if base and not any(ch in base for ch in "*?") and (key == base or key.startswith(base + "/")):
            return True
    return False


@dataclass
class PolicyResult:
    ok: bool
    violations: list[dict] = field(default_factory=list)
    checked: int = 0

    def codes(self) -> set[str]:
        return {v["code"] for v in self.violations}


class PathPolicy:
    """allowed_write_paths / forbidden_write_roots evaluation (forbidden always wins)."""

    def __init__(self, allowed_write_paths: list[str], forbidden_write_roots: list[str] | None = None,
                 protected_roots: list[str] | None = None):
        self.allowed: list[str] = []
        for a in allowed_write_paths or []:
            try:
                validate_relative_path(a, allow_glob=True)
            except PathViolation as exc:
                raise PathViolation("BAD_ALLOWLIST_ENTRY", f"{a!r}: {exc.code}") from exc
            self.allowed.append(a)
        self.forbidden_rel: list[str] = []
        self.forbidden_abs: list[str] = []
        for f in (forbidden_write_roots or []):
            if _DRIVE_RE.match(f) or f.startswith("\\\\") or os.path.isabs(f):
                self.forbidden_abs.append(os.path.abspath(f))
            else:
                self.forbidden_rel.append(f)
        for p in protected_roots or []:
            self.forbidden_abs.append(os.path.abspath(p))

    def check_path(self, rel: str, sandbox_root: str | os.PathLike | None = None) -> list[dict]:
        v: list[dict] = []
        try:
            norm = validate_relative_path(rel)
        except PathViolation as exc:
            return [{"path": rel, "code": exc.code, "detail": exc.detail}]
        if self.forbidden_rel and matches_any(norm, self.forbidden_rel):
            v.append({"path": rel, "code": "FORBIDDEN_ROOT", "detail": "matches forbidden_write_roots"})
        if sandbox_root is not None:
            try:
                resolved = resolve_in_root(sandbox_root, norm)
                for fa in self.forbidden_abs:
                    if is_within(resolved, fa):
                        v.append({"path": rel, "code": "PROTECTED_ROOT", "detail": fa})
            except PathViolation as exc:
                v.append({"path": rel, "code": exc.code, "detail": exc.detail})
        if not matches_any(norm, self.allowed):
            v.append({"path": rel, "code": "NOT_ALLOWLISTED", "detail": "not in allowed_write_paths"})
        return v

    def check_changed(self, changed: list[str], sandbox_root: str | os.PathLike | None = None) -> PolicyResult:
        violations: list[dict] = []
        for rel in changed:
            violations.extend(self.check_path(rel, sandbox_root))
        return PolicyResult(not violations, violations, len(changed))
