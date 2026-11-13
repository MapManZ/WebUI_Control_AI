"""Win32 Job Object guard so no child process tree survives timeout/cancellation.

Uses ``ctypes.windll.kernel32``: CreateJobObjectW, SetInformationJobObject
(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE), AssignProcessToJobObject, TerminateJobObject, CloseHandle.
Fallback: ``taskkill /PID <pid> /T /F`` (and ``os.killpg`` on POSIX).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass

IS_WINDOWS = os.name == "nt"

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER), ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]


def pid_alive(pid: int) -> bool:
    """True if a process with this pid is still running."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            _k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def taskkill_tree(pid: int) -> bool:
    """Fallback: kill a process tree via ``taskkill /PID <pid> /T /F``."""
    if IS_WINDOWS:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
        return r.returncode == 0
    try:
        os.killpg(os.getpgid(pid), 9)
        return True
    except OSError:
        return False


class JobObject:
    """A kill-on-close Windows job object (no-op shell on non-Windows)."""

    def __init__(self, name: str | None = None):
        self.handle = None
        self.available = False
        self.error: str | None = None
        if not IS_WINDOWS:
            return
        h = _k32.CreateJobObjectW(None, name)
        if not h:
            self.error = f"CreateJobObjectW failed: {ctypes.get_last_error()}"
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _k32.SetInformationJobObject(h, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info),
                                          ctypes.sizeof(info))
        if not ok:
            self.error = f"SetInformationJobObject failed: {ctypes.get_last_error()}"
            _k32.CloseHandle(h)
            return
        self.handle = h
        self.available = True

    def assign_pid(self, pid: int) -> bool:
        if not self.available:
            return False
        ph = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not ph:
            self.error = f"OpenProcess failed: {ctypes.get_last_error()}"
            return False
        try:
            ok = bool(_k32.AssignProcessToJobObject(self.handle, ph))
            if not ok:
                self.error = f"AssignProcessToJobObject failed: {ctypes.get_last_error()}"
            return ok
        finally:
            _k32.CloseHandle(ph)

    def terminate(self, exit_code: int = 1) -> bool:
        if not self.available or self.handle is None:
            return False
        return bool(_k32.TerminateJobObject(self.handle, exit_code))

    def close(self) -> None:
        if self.handle is not None:
            _k32.CloseHandle(self.handle)  # KILL_ON_JOB_CLOSE reaps survivors
            self.handle = None
            self.available = False

    def __enter__(self) -> "JobObject":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@dataclass
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    cancelled: bool
    duration_ms: int
    pid: int
    job_object_used: bool
    tree_killed: bool
    spawn_error: str | None = None


def run_bounded(
    cmd: list[str],
    *,
    cwd: str,
    env: dict | None = None,
    timeout: float = 180.0,
    stdin_text: str | None = None,
    cancel_event=None,
    max_output_chars: int = 2_000_000,
) -> ProcessResult:
    """Run ``cmd`` inside a job object; on timeout/cancel the entire process tree is killed."""
    t0 = time.monotonic()
    flags = 0
    kwargs: dict = {}
    if IS_WINDOWS:
        flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags, **kwargs,
        )
    except (OSError, ValueError) as exc:
        return ProcessResult(-1, "", f"spawn failed: {exc}", False, False, int((time.monotonic() - t0) * 1000), 0,
                             False, False, spawn_error=str(exc))
    job = JobObject()
    assigned = job.assign_pid(proc.pid) if job.available else False
    timed_out = cancelled = tree_killed = False
    out = err = ""
    try:
        deadline = time.monotonic() + timeout
        pending_input = stdin_text
        while True:
            remaining = deadline - time.monotonic()
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if remaining <= 0:
                timed_out = True
                break
            try:
                out, err = proc.communicate(input=pending_input, timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                pending_input = None  # input already handed to communicate()
                continue
        if timed_out or cancelled:
            tree_killed = _kill_tree(proc, job, assigned)
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:
                out = err = ""
    finally:
        if proc.poll() is None:
            _kill_tree(proc, job, assigned)
        job.close()
    return ProcessResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=_decode(out, max_output_chars), stderr=_decode(err, max_output_chars),
        timed_out=timed_out, cancelled=cancelled,
        duration_ms=int((time.monotonic() - t0) * 1000), pid=proc.pid,
        job_object_used=assigned, tree_killed=tree_killed,
    )


def _kill_tree(proc: subprocess.Popen, job: JobObject, assigned: bool) -> bool:
    killed = False
    if assigned:
        killed = job.terminate(1)
    # always also run the taskkill fallback: harmless if the tree is already gone
    killed = taskkill_tree(proc.pid) or killed
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    return killed


def _decode(data, limit: int) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    return data[:limit]
