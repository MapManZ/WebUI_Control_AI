"""Sandbox security: path guard, manifests, immutable attempts, job-object process-tree kill."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.e2e import make_junction, rmtree_retry

from src.sandbox import (
    JobObject, PathPolicy, PathViolation, SandboxError, WorktreeManager, anchor_manifest, build_manifest,
    changed_paths, diff_manifests, fingerprint_tree, fingerprint_roots, is_reparse_point, manifest_digest, pid_alive,
    resolve_in_root, run_bounded, scan_links, taskkill_tree, validate_relative_path, verify_protected_untouched,
)
from src.sandbox.path_guard import matches_any


def _code(fn, *a, **k):
    try:
        fn(*a, **k)
    except PathViolation as exc:
        return exc.code
    return None


class PathGuardTests(unittest.TestCase):
    def test_rejections(self):
        cases = {
            "../escape.txt": "PATH_TRAVERSAL",
            "a/../../escape.txt": "PATH_TRAVERSAL",
            "a\\..\\..\\escape.txt": "PATH_TRAVERSAL",
            "C:\\Windows\\win.ini": "DRIVE_PATH",
            "D:other.txt": "DRIVE_PATH",
            "\\\\server\\share\\x": "UNC_PATH",
            "//server/share/x": "UNC_PATH",
            "\\\\?\\C:\\x": "UNC_PATH",
            "\\rooted.txt": "ABSOLUTE_PATH",
            "/rooted.txt": "ABSOLUTE_PATH",
            "file.txt:secret": "ALTERNATE_DATA_STREAM",
            "dir/file.txt:secret:$DATA": "ALTERNATE_DATA_STREAM",
            "trailing.": "WINDOWS_ALIAS",
            "space ": "WINDOWS_ALIAS",
            "nul": "RESERVED_DEVICE_NAME",
            "dir/CON.txt": "RESERVED_DEVICE_NAME",
            "PROGRA~1/x": "SHORT_NAME_ALIAS",
            "": "EMPTY_PATH",
            "a\x00b": "NUL_BYTE",
            "bad|name": "ILLEGAL_CHARACTER",
            "glob*.txt": "ILLEGAL_CHARACTER",
        }
        for path, code in cases.items():
            self.assertEqual(_code(validate_relative_path, path), code, path)

    def test_accepts_normal_paths(self):
        self.assertEqual(validate_relative_path("src\\pkg\\mod.py"), "src/pkg/mod.py")
        self.assertEqual(validate_relative_path("./a/./b.txt"), "a/b.txt")
        self.assertEqual(validate_relative_path("dir/**", allow_glob=True), "dir/**")

    def test_allowlist_entries_validated(self):
        for bad in ("../x", "C:/x", "\\\\s\\x", "a.txt:ads"):
            with self.assertRaises(PathViolation):
                PathPolicy([bad])

    def test_policy_allow_and_forbidden_precedence(self):
        pol = PathPolicy(["src/**", "docs/*.md"], ["src/secret/", "docs/private.md"])
        self.assertTrue(pol.check_changed(["src/a.py", "src/deep/b.py", "docs/readme.md"]).ok)
        res = pol.check_changed(["src/secret/key.txt", "docs/private.md", "other.txt", "docs/sub/x.md"])
        self.assertFalse(res.ok)
        flagged = {v["path"]: v["code"] for v in res.violations if v["code"] != "NOT_ALLOWLISTED"}
        self.assertEqual(flagged["src/secret/key.txt"], "FORBIDDEN_ROOT")  # forbidden beats allow
        self.assertEqual(flagged["docs/private.md"], "FORBIDDEN_ROOT")
        paths = {v["path"] for v in res.violations}
        self.assertIn("other.txt", paths)
        self.assertIn("docs/sub/x.md", paths)  # '*' must not cross directories

    def test_case_aliasing_cannot_bypass_forbidden(self):
        pol = PathPolicy(["**"], ["secrets/"])
        for p in ("SECRETS/x.txt", "Secrets/X.txt", "sEcReTs\\x.txt"):
            self.assertIn("FORBIDDEN_ROOT", {v["code"] for v in pol.check_path(p)}, p)

    def test_traversal_in_changed_list_is_violation(self):
        pol = PathPolicy(["**"])
        self.assertIn("PATH_TRAVERSAL", pol.check_changed(["../x"]).codes())
        self.assertIn("ALTERNATE_DATA_STREAM", pol.check_changed(["a.txt:s"]).codes())

    def test_absolute_forbidden_root_blocks_resolved_path(self):
        root = tempfile.mkdtemp(prefix="orch_pg_")
        self.addCleanup(rmtree_retry, root)
        sandbox = os.path.join(root, "sb")
        os.makedirs(sandbox)
        pol = PathPolicy(["**"], [sandbox])  # absolute forbidden root == the sandbox itself
        self.assertIn("PROTECTED_ROOT", pol.check_changed(["x.txt"], sandbox).codes())

    def test_matches_any_subtree_semantics(self):
        self.assertTrue(matches_any("a/b/c.txt", ["a"]))
        self.assertTrue(matches_any("a/b/c.txt", ["a/"]))
        self.assertFalse(matches_any("ab/c.txt", ["a"]))
        self.assertTrue(matches_any("x/y.py", ["**/*.py"]))

    def test_junction_and_symlink_escape_detected(self):
        root = tempfile.mkdtemp(prefix="orch_link_")
        self.addCleanup(rmtree_retry, root)
        sandbox, outside = os.path.join(root, "sb"), os.path.join(root, "outside")
        os.makedirs(sandbox)
        os.makedirs(outside)
        Path(outside, "target.txt").write_text("x")
        link = os.path.join(sandbox, "lnk")
        if not make_junction(link, outside):
            self.skipTest("cannot create junction/symlink in this environment")
        self.assertTrue(is_reparse_point(link))
        self.assertEqual(_code(resolve_in_root, sandbox, "lnk/target.txt"), "LINK_ESCAPE")
        links = scan_links(sandbox)
        self.assertEqual([x["path"] for x in links], ["lnk"])
        self.assertTrue(links[0]["escapes"])
        pol = PathPolicy(["lnk/**", "**"])
        self.assertIn("LINK_ESCAPE", pol.check_changed(["lnk/target.txt"], sandbox).codes())
        man = build_manifest(sandbox)
        self.assertTrue(man["lnk"].startswith("LINK:"))
        self.assertNotIn("lnk/target.txt", man)  # never followed

    def test_real_ads_file_path_rejected(self):
        if os.name != "nt":
            self.skipTest("NTFS only")
        root = tempfile.mkdtemp(prefix="orch_ads_")
        self.addCleanup(rmtree_retry, root)
        base = os.path.join(root, "f.txt")
        Path(base).write_text("x")
        try:
            with open(base + ":hidden", "w") as fh:
                fh.write("secret")
        except OSError:
            self.skipTest("ADS unsupported on this volume")
        self.assertEqual(_code(resolve_in_root, root, "f.txt:hidden"), "ALTERNATE_DATA_STREAM")
        self.assertEqual(list(build_manifest(root)), ["f.txt"])  # ADS invisible to dir walk => path guard is required

    def test_resolve_in_root_normal(self):
        root = tempfile.mkdtemp(prefix="orch_res_")
        self.addCleanup(rmtree_retry, root)
        p = resolve_in_root(root, "a/b.txt")
        self.assertTrue(str(p).lower().startswith(os.path.realpath(root).lower()))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="orch_man_")
        self.addCleanup(rmtree_retry, self.d)
        Path(self.d, "a.txt").write_text("a")
        Path(self.d, "sub").mkdir()
        Path(self.d, "sub", "b.txt").write_text("b")

    def test_deterministic_and_diff(self):
        m1 = build_manifest(self.d)
        self.assertEqual(manifest_digest(m1), manifest_digest(build_manifest(self.d)))
        Path(self.d, "a.txt").write_text("A")
        Path(self.d, "new.txt").write_text("n")
        os.remove(os.path.join(self.d, "sub", "b.txt"))
        m2 = build_manifest(self.d)
        diff = diff_manifests(m1, m2)
        self.assertEqual(diff, {"added": ["new.txt"], "modified": ["a.txt"], "deleted": ["sub/b.txt"]})
        self.assertEqual(changed_paths(diff), ["a.txt", "new.txt", "sub/b.txt"])

    def test_pycache_ignored(self):
        before = build_manifest(self.d)
        Path(self.d, "__pycache__").mkdir()
        Path(self.d, "__pycache__", "x.cpython-314.pyc").write_bytes(b"1")
        self.assertEqual(before, build_manifest(self.d))

    def test_anchor_manifest(self):
        am = anchor_manifest([os.path.join(self.d, "a.txt"), os.path.join(self.d, "missing.txt")])
        self.assertEqual(am[os.path.join(self.d, "missing.txt")], "MISSING")
        self.assertEqual(len(am[os.path.join(self.d, "a.txt")]), 64)

    def test_protected_roots_one_byte_change_detected(self):
        for content in (False, True):
            before = fingerprint_roots([self.d], content=content)
            ok, probs = verify_protected_untouched(before, fingerprint_roots([self.d], content=content))
            self.assertTrue(ok, probs)
            time.sleep(0.02)
            Path(self.d, "a.txt").write_text("b")  # same size, 1 byte different
            ok, probs = verify_protected_untouched(before, fingerprint_roots([self.d], content=content))
            self.assertFalse(ok)
            self.assertIn("CHANGED", probs[0])
            Path(self.d, "a.txt").write_text("a")
        before = fingerprint_roots([self.d])
        Path(self.d, "extra.txt").write_text("z")
        self.assertFalse(verify_protected_untouched(before, fingerprint_roots([self.d]))[0])

    def test_fingerprint_missing_root(self):
        fp = fingerprint_tree(os.path.join(self.d, "nope"))
        self.assertEqual(fp["digest"], "MISSING")


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_wt_")
        self.addCleanup(rmtree_retry, self.root)
        self.src = os.path.join(self.root, "src")
        os.makedirs(os.path.join(self.src, "tools"))
        os.makedirs(os.path.join(self.src, ".git"))
        Path(self.src, "a.txt").write_text("a")
        Path(self.src, "tools", "t.py").write_text("t")
        Path(self.src, "secret.txt").write_text("s")
        Path(self.src, ".git", "config").write_text("g")
        self.wm = WorktreeManager(os.path.join(self.root, "rt"))

    def test_selective_snapshot_layout(self):
        sb = self.wm.create_attempt("JOB-1", source_root=self.src, include=["a.txt", "tools/**"])
        self.assertTrue(sb.path.replace("\\", "/").endswith("/sandboxes/JOB-1/attempt-1"))
        self.assertEqual(sorted(build_manifest(sb.path)), ["a.txt", "tools/t.py"])
        self.assertEqual(sb.files_copied, 2)

    def test_git_dir_never_copied_and_links_skipped(self):
        outside = os.path.join(self.root, "outside")
        os.makedirs(outside)
        Path(outside, "o.txt").write_text("o")
        make_junction(os.path.join(self.src, "jl"), outside)
        sb = self.wm.create_attempt("JOB-2", source_root=self.src)
        man = build_manifest(sb.path)
        self.assertFalse(any(k.startswith(".git") for k in man))
        self.assertFalse(any(k.startswith("jl") for k in man))
        self.assertEqual(scan_links(sb.path), [])

    def test_attempts_immutable(self):
        a1 = self.wm.create_attempt("JOB-3", source_root=self.src, include=["a.txt"])
        Path(a1.path, "work.txt").write_text("attempt1 output")
        digest1 = self.wm.seal_attempt("JOB-3", 1)
        with self.assertRaises(SandboxError):
            self.wm.create_attempt("JOB-3", 1, source_root=self.src)  # never overwrite attempt-1
        a2 = self.wm.create_attempt("JOB-3", source_root=self.src, include=["a.txt"])
        self.assertTrue(a2.path.endswith("attempt-2"))
        self.assertNotIn("work.txt", build_manifest(a2.path))  # attempt-2 starts clean
        Path(a2.path, "other.txt").write_text("attempt2 output")
        self.assertTrue(self.wm.verify_sealed("JOB-3", 1))
        self.assertEqual(manifest_digest(build_manifest(a1.path)), digest1)
        Path(a1.path, "work.txt").write_text("tampered")
        self.assertFalse(self.wm.verify_sealed("JOB-3", 1))
        self.assertEqual(self.wm.list_attempts("JOB-3"), [1, 2])

    def test_unsafe_job_ids(self):
        for bad in ("..", "../x", "a/b", "a\\b", "", "C:evil", "x" * 100):
            with self.assertRaises(SandboxError):
                self.wm.create_attempt(bad)

    def test_cannot_create_attempt_behind_existing(self):
        self.wm.create_attempt("JOB-4", 2)
        with self.assertRaises(SandboxError):
            self.wm.create_attempt("JOB-4", 1)

    def test_bad_include_pattern_rejected(self):
        with self.assertRaises(PathViolation):
            self.wm.create_attempt("JOB-5", source_root=self.src, include=["../x"])
        self.assertEqual(self.wm.list_attempts("JOB-5"), [])


SPAWN_CHILD = (
    "import subprocess,sys,time,os;"
    "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
    "open('child.pid','w').write(str(p.pid));time.sleep(120)"
)


class JobObjectTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="orch_job_")
        self.addCleanup(rmtree_retry, self.d)

    def _child_pid(self):
        for _ in range(50):
            p = Path(self.d, "child.pid")
            if p.exists() and p.read_text().strip():
                return int(p.read_text())
            time.sleep(0.1)
        self.fail("child pid file not written")

    def _assert_dead(self, pid):
        for _ in range(30):
            if not pid_alive(pid):
                return
            time.sleep(0.1)
        self.fail(f"orphan process {pid} survived")

    def test_timeout_kills_entire_tree(self):
        r = run_bounded([sys.executable, "-c", SPAWN_CHILD], cwd=self.d, timeout=2.5)
        self.assertTrue(r.timed_out)
        self.assertTrue(r.tree_killed)
        if os.name == "nt":
            self.assertTrue(r.job_object_used)
        self._assert_dead(self._child_pid())
        self._assert_dead(r.pid)

    def test_cancel_event_kills_tree(self):
        ev = threading.Event()
        threading.Timer(1.5, ev.set).start()
        r = run_bounded([sys.executable, "-c", SPAWN_CHILD], cwd=self.d, timeout=60, cancel_event=ev)
        self.assertTrue(r.cancelled)
        self._assert_dead(self._child_pid())

    def test_normal_exit_codes_and_output(self):
        r = run_bounded([sys.executable, "-c", "import sys;print('out');sys.stderr.write('err');sys.exit(7)"],
                        cwd=self.d, timeout=30)
        self.assertEqual((r.exit_code, r.timed_out), (7, False))
        self.assertIn("out", r.stdout)
        self.assertIn("err", r.stderr)

    def test_spawn_failure_reported(self):
        r = run_bounded(["definitely-not-a-binary-xyz"], cwd=self.d, timeout=5)
        self.assertEqual(r.exit_code, -1)
        self.assertIsNotNone(r.spawn_error)

    @unittest.skipUnless(os.name == "nt", "Windows job objects")
    def test_kill_on_job_close_reaps_survivors(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"])
        job = JobObject()
        self.assertTrue(job.available, job.error)
        self.assertTrue(job.assign_pid(proc.pid))
        self.assertTrue(pid_alive(proc.pid))
        job.close()  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self._assert_dead(proc.pid)
        proc.wait(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows job objects")
    def test_terminate_job_object(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"])
        with JobObject() as job:
            self.assertTrue(job.assign_pid(proc.pid))
            self.assertTrue(job.terminate(3))
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 3)

    def test_taskkill_fallback_kills_tree(self):
        proc = subprocess.Popen([sys.executable, "-c", SPAWN_CHILD], cwd=self.d)
        pid_child = self._child_pid()
        self.assertTrue(taskkill_tree(proc.pid))
        proc.wait(timeout=10)
        self._assert_dead(pid_child)


if __name__ == "__main__":
    unittest.main()
