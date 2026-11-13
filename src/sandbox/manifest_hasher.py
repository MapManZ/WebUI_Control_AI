"""Deterministic SHA-256 manifests, pre/post diffs and protected-root integrity checks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Iterable

from .path_guard import is_reparse_point

DEFAULT_EXCLUDE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache"})
DEFAULT_EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def sha256_file(path: str | os.PathLike, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def build_manifest(
    root: str | os.PathLike,
    *,
    exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIRS,
    exclude_suffixes: Iterable[str] = DEFAULT_EXCLUDE_SUFFIXES,
) -> dict[str, str]:
    """rel/posix/path -> sha256. Links are recorded as ``LINK:<target>`` and never followed."""
    root = str(root)
    exd = set(exclude_dirs)
    exs = tuple(exclude_suffixes)
    out: dict[str, str] = {}
    for dp, dns, fns in os.walk(root, followlinks=False):
        keep = []
        for d in sorted(dns):
            full = os.path.join(dp, d)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if is_reparse_point(full):
                out[rel] = "LINK:" + os.path.realpath(full)
            elif d not in exd:
                keep.append(d)
        dns[:] = keep
        for f in sorted(fns):
            if f.endswith(exs):
                continue
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if is_reparse_point(full):
                out[rel] = "LINK:" + os.path.realpath(full)
            else:
                out[rel] = sha256_file(full)
    return dict(sorted(out.items()))


def manifest_digest(manifest: dict[str, str]) -> str:
    blob = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def diff_manifests(pre: dict[str, str], post: dict[str, str]) -> dict[str, list[str]]:
    added = sorted(set(post) - set(pre))
    deleted = sorted(set(pre) - set(post))
    modified = sorted(k for k in set(pre) & set(post) if pre[k] != post[k])
    return {"added": added, "modified": modified, "deleted": deleted}


def changed_paths(diff: dict[str, list[str]]) -> list[str]:
    return sorted(set(diff["added"]) | set(diff["modified"]) | set(diff["deleted"]))


def anchor_manifest(files: Iterable[str | os.PathLike]) -> dict[str, str]:
    """Content hashes of specific protected anchor files (missing files recorded as MISSING)."""
    out = {}
    for f in files:
        p = str(f)
        out[p] = sha256_file(p) if os.path.isfile(p) else "MISSING"
    return dict(sorted(out.items()))


def fingerprint_tree(
    root: str | os.PathLike,
    *,
    content: bool = False,
    max_content_files: int | None = None,
) -> dict:
    """Fingerprint an external protected root.

    ``content=False`` hashes (relpath,size,mtime_ns) of every file (fast, catches any write);
    ``content=True`` hashes file bytes (exact). When ``max_content_files`` is set and the
    tree exceeds that count, automatically falls back to stat mode to avoid multi-minute
    stalls on multi-gigabyte external trees. Returns {root, mode, file_count, digest}.
    """
    root = str(root)
    h = hashlib.sha256()
    n = 0
    if not os.path.exists(root):
        return {"root": root, "mode": "content" if content else "stat", "file_count": 0, "digest": "MISSING"}
    if os.path.isfile(root):
        try:
            st_single = os.lstat(root)
        except OSError:
            st_single = None
        entries: list[tuple[str, str | None, os.stat_result | None, bool]] = [
            ("", root, st_single, is_reparse_point(root))
        ]
        file_total = 1
    else:
        entries = []
        file_total = 0
        base = root.rstrip("\\/")
        stack = [root]
        while stack:
            dp = stack.pop()
            rel_dir = dp[len(base):].lstrip("\\/").replace("\\", "/")
            prefix = (rel_dir + "/") if rel_dir else ""
            subdirs: list[tuple[str, str]] = []
            subfiles: list[tuple[str, str, os.stat_result | None, bool]] = []
            try:
                with os.scandir(dp) as it:
                    for entry in it:
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            is_dir = False
                        if is_dir:
                            subdirs.append((entry.name, entry.path))
                        else:
                            try:
                                st_e = entry.stat(follow_symlinks=False)
                            except OSError:
                                st_e = None
                            try:
                                is_sym = entry.is_symlink()
                            except OSError:
                                is_sym = True
                            subfiles.append((entry.name, entry.path, st_e, is_sym))
            except OSError:
                continue
            subdirs.sort(key=lambda x: x[0])
            subfiles.sort(key=lambda x: x[0])
            for fname, fpath, st_e, is_sym in subfiles:
                entries.append((prefix + fname, fpath, st_e, is_sym))
                file_total += 1
            for dname, _dpath in subdirs:
                entries.append((prefix + dname + "/", None, None, False))
            for _dname, dpath in reversed(subdirs):
                stack.append(dpath)

    effective_content = bool(content) and (max_content_files is None or file_total <= max_content_files)
    for rel, full, st, is_sym in entries:
        if full is None:
            h.update(f"D|{rel}\n".encode("utf-8", "surrogatepass"))
            continue
        if st is None:
            h.update(f"E|{rel}\n".encode("utf-8", "surrogatepass"))
            continue
        n += 1
        if effective_content and not (is_sym or is_reparse_point(full)):
            try:
                body = sha256_file(full)
            except OSError:
                body = "UNREADABLE"
        else:
            body = f"{st.st_size}:{st.st_mtime_ns}"
        h.update(f"F|{rel}|{body}\n".encode("utf-8", "surrogatepass"))
    return {"root": root, "mode": "content" if effective_content else "stat", "file_count": n, "digest": h.hexdigest()}


def fingerprint_roots(
    roots: Iterable[str | os.PathLike],
    *,
    content: bool = False,
    max_content_files: int | None = None,
) -> dict[str, dict]:
    return {str(r): fingerprint_tree(r, content=content, max_content_files=max_content_files) for r in roots}


def verify_protected_untouched(before: dict[str, dict], after: dict[str, dict]) -> tuple[bool, list[str]]:
    """0 bytes changed in protected roots: compare per-root fingerprints."""
    problems: list[str] = []
    for root in sorted(set(before) | set(after)):
        b, a = before.get(root), after.get(root)
        if b is None or a is None:
            problems.append(f"{root}: fingerprint missing ({'before' if b is None else 'after'})")
        elif b["digest"] != a["digest"] or b["file_count"] != a["file_count"]:
            problems.append(f"{root}: CHANGED digest {b['digest'][:12]}.. -> {a['digest'][:12]}.. "
                            f"files {b['file_count']} -> {a['file_count']}")
    return (not problems), problems
