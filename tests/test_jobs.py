"""Tests for prview.jobs — claude invocation as cancellable background jobs.
No real claude process is spawned; subprocess.Popen is patched."""
import os
import time
from unittest.mock import patch

from prview.core import FileDiff, PRInfo
from prview import jobs


def _wait_status(job_id, target, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = jobs.get_job(job_id)
        if snap and snap["status"] == target:
            return snap
        time.sleep(0.005)
    return jobs.get_job(job_id)


class FakePopen:
    """Stand-in for subprocess.Popen used by the job runner."""

    instances = []

    def __init__(self, argv, *, stdout=None, stderr=None, text=None, env=None):
        self.argv = argv
        self.env = env
        self.text = text
        self.returncode = None
        self._out = ""
        self._err = ""
        self.killed = False
        FakePopen.instances.append(self)

    # subclasses customize communicate / kill

    def kill(self):
        self.killed = True
        self.returncode = -9

    def communicate(self, timeout=None):
        self.returncode = 0
        return self._out, self._err


class SuccessPopen(FakePopen):
    def communicate(self, timeout=None):
        self.returncode = 0
        return "the answer", ""


class ErrorPopen(FakePopen):
    def communicate(self, timeout=None):
        self.returncode = 1
        return "", "claude exploded"


class TimeoutPopen(FakePopen):
    def communicate(self, timeout=None):
        import subprocess
        # First call (with the job timeout) raises; the post-kill drain
        # (timeout=None) returns, mirroring real Popen behavior.
        if timeout is not None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.returncode = -9
        return "", ""


class BlockingPopen(FakePopen):
    """Blocks in communicate until killed — models a long-running claude."""

    def communicate(self, timeout=None):
        deadline = time.time() + 5.0
        while not self.killed and time.time() < deadline:
            time.sleep(0.01)
        if self.killed:
            self.returncode = -9
            return "", "killed"
        self.returncode = 0
        return "late", ""


def setup_function(_):
    FakePopen.instances.clear()
    jobs.jobs.clear()


def test_invoke_claude_argv_and_env_byte_for_byte():
    with patch("prview.jobs.subprocess.Popen", SuccessPopen):
        os.environ["CLAUDECODE"] = "1"
        os.environ["KEEP_ME"] = "yes"
        jid = jobs.start_job("explain", "PROMPT-TEXT", timeout=300)
        _wait_status(jid, "done")

    p = FakePopen.instances[-1]
    # Flags EXACTLY as source invoke_claude, prompt as discrete argv element.
    assert p.argv == ["claude", "--print", "--dangerously-skip-permissions", "-p", "PROMPT-TEXT"]
    # CLAUDECODE stripped from the child env; other vars preserved.
    assert "CLAUDECODE" not in p.env
    assert p.env.get("KEEP_ME") == "yes"


def test_job_lifecycle_success():
    with patch("prview.jobs.subprocess.Popen", SuccessPopen):
        jid = jobs.start_job("explain", "p", timeout=300)
        snap = _wait_status(jid, "done")
    assert snap["status"] == "done"
    assert snap["result"] == "the answer"
    assert "error" not in snap or snap["error"] is None


def test_job_error_on_nonzero_exit():
    with patch("prview.jobs.subprocess.Popen", ErrorPopen):
        jid = jobs.start_job("ask", "p", timeout=300)
        snap = _wait_status(jid, "error")
    assert snap["status"] == "error"
    assert "claude exploded" in snap["error"]


def test_job_error_on_timeout():
    with patch("prview.jobs.subprocess.Popen", TimeoutPopen):
        jid = jobs.start_job("summary", "p", timeout=60)
        snap = _wait_status(jid, "error")
    assert snap["status"] == "error"
    assert snap["error"]


def test_elapsed_increases():
    with patch("prview.jobs.subprocess.Popen", BlockingPopen):
        jid = jobs.start_job("explain", "p", timeout=300)
        _wait_status(jid, "running")
        e1 = jobs.get_job(jid)["elapsed"]
        time.sleep(0.05)
        e2 = jobs.get_job(jid)["elapsed"]
        jobs.cancel_job(jid)
    assert e2 >= e1
    assert e2 > 0


def test_cancel_terminates_popen_child():
    with patch("prview.jobs.subprocess.Popen", BlockingPopen):
        jid = jobs.start_job("explain", "p", timeout=300)
        _wait_status(jid, "running")
        child = FakePopen.instances[-1]
        jobs.cancel_job(jid)
        snap = _wait_status(jid, "error")
    # best-effort kill reached the child.
    assert child.killed is True
    assert snap["status"] in ("error", "cancelled")


def test_get_job_unknown_returns_none():
    assert jobs.get_job("no-such-id") is None
