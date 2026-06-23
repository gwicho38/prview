"""AI job manager: claude runs as cancellable background jobs (FR-5).

Ported from the mcli `pr-review` workflow's `invoke_claude` (src 299-306).
The claude argv, env-strip, prompt, and timeout are byte-for-byte identical to
source. Prompts come from prview.core builders (never inlined here).

Thread-based on purpose: a sync `def` FastAPI route enqueues a job and returns
immediately, so the event loop is never blocked by a 300s claude call. The
registry is in-memory and never persisted.
"""
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field

from prview.core import (
    FileDiff,
    PRInfo,
    build_ask_prompt,
    build_explain_prompt,
    build_explain_selection_prompt,
    build_summary_prompt,
)


@dataclass
class AIJob:
    id: str
    kind: str
    status: str = "running"          # running | done | error | cancelled
    result: str = ""
    error: str | None = None
    started_at: float = 0.0
    _proc: "subprocess.Popen | None" = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


jobs: dict[str, AIJob] = {}


def _claude_argv(prompt: str) -> list[str]:
    # Flags EXACTLY as source invoke_claude; prompt is a discrete argv element.
    return ["claude", "--print", "--dangerously-skip-permissions", "-p", prompt]


def _claude_env() -> dict[str, str]:
    # CLAUDECODE stripped, every other var preserved — identical to source.
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


def _run_claude(job: AIJob, prompt: str, timeout: int):
    """The Popen wrapper. Intentional deviation from source `invoke_claude`:
    source used a blocking `subprocess.run`; we hold a `subprocess.Popen` kill
    handle so POST /job/{id}/cancel can terminate the child. The argv, env
    (CLAUDECODE stripped), prompt, and timeout are byte-for-byte identical to
    source — only run→Popen+communicate differs."""
    try:
        proc = subprocess.Popen(
            _claude_argv(prompt),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_claude_env(),
        )
    except Exception as exc:  # spawn failure
        with job._lock:
            if job.status == "running":
                job.status = "error"
                job.error = str(exc)
        return

    with job._lock:
        if job.status == "cancelled":
            proc.kill()
            return
        job._proc = proc

    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        with job._lock:
            if job.status == "running":
                job.status = "error"
                job.error = f"Claude CLI timed out after {timeout}s"
        return
    except Exception as exc:
        with job._lock:
            if job.status == "running":
                job.status = "error"
                job.error = str(exc)
        return

    with job._lock:
        if job.status == "cancelled":
            return
        if proc.returncode != 0:
            job.status = "error"
            job.error = f"Claude CLI failed (exit {proc.returncode}): {err[:500]}"
        else:
            job.status = "done"
            job.result = out.strip()


def start_job(kind: str, prompt: str, timeout: int) -> str:
    """Mint a job, run the Popen wrapper on a background thread, return its id."""
    job = AIJob(id=str(uuid.uuid4()), kind=kind, started_at=time.time())
    jobs[job.id] = job
    threading.Thread(
        target=_run_claude, args=(job, prompt, timeout), daemon=True,
    ).start()
    return job.id


def get_job(job_id: str) -> dict | None:
    """Snapshot a job for GET /job/{id}: status, result?, error?, elapsed."""
    job = jobs.get(job_id)
    if job is None:
        return None
    return {
        "status": job.status,
        "result": job.result,
        "error": job.error,
        "elapsed": time.time() - job.started_at,
    }


def cancel_job(job_id: str) -> bool:
    """Best-effort kill the Popen child and mark the job (POST /job/{id}/cancel)."""
    job = jobs.get(job_id)
    if job is None:
        return False
    with job._lock:
        if job.status not in ("running",):
            return False
        job.status = "cancelled"
        proc = job._proc
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
    return True


_KIND_TIMEOUTS = {"summary": 60, "explain": 300, "ask": 300, "explain-selection": 300}


def start_summary(pr: PRInfo, fd: FileDiff) -> str:
    """Wire summary via summarize_file_change shape (src 367-380): 60s timeout."""
    return start_job("summary", build_summary_prompt(pr, fd), timeout=_KIND_TIMEOUTS["summary"])


def start_explain(pr: PRInfo, fd: FileDiff) -> str:
    return start_job("explain", build_explain_prompt(pr, fd), timeout=_KIND_TIMEOUTS["explain"])


def start_ask(pr: PRInfo, fd: FileDiff, question: str) -> str:
    return start_job("ask", build_ask_prompt(pr, fd, question), timeout=_KIND_TIMEOUTS["ask"])


def start_explain_selection(pr: PRInfo, fd: FileDiff, selection: str) -> str:
    return start_job(
        "explain-selection",
        build_explain_selection_prompt(pr, fd, selection),
        timeout=_KIND_TIMEOUTS["explain-selection"],
    )
