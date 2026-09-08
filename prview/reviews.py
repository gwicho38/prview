"""Review runs: a read-only claude agent in the PR worktree, streaming findings.

Shaped after repowise.DocgenJob — Popen in its own session so a cancel signals
the whole process group, stdout to a log file, polled by a snapshot endpoint.

The agent gets Read/Grep/Glob and nothing else. That is what makes "do not
post" enforced rather than requested: pr-review's review mode ends by posting
with gh and its respond mode implements fixes, and neither is reachable without
Bash or Write.
"""
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import prview.core as core
import prview.repowise as repowise
from prview import state_store
from prview.findings import Finding, Placement, parse_findings, place

REVIEW_TIMEOUT = 1800
_ALLOWED_TOOLS = "Read,Grep,Glob"
_DENIED_TOOLS = "Bash,Write,Edit,WebFetch,NotebookEdit"
_LOG_DIR = Path.home() / ".prview" / "review-logs"
_POLL_SECONDS = 0.5


@dataclass
class ReviewRun:
    id: str
    owner: str
    repo: str
    number: int
    skill: str
    scope: str
    status: str = "running"          # running | done | error | cancelled
    staged: int = 0
    dropped: int = 0
    demoted: int = 0
    error: str | None = None
    logfile: str = ""
    started_at: float = field(default_factory=time.time)
    _proc: "subprocess.Popen | None" = field(default=None, repr=False)
    _cancelled: bool = field(default=False, repr=False)


_runs: dict[str, ReviewRun] = {}
_runs_lock = threading.Lock()


def claude_argv(prompt: str) -> list[str]:
    """Read-only agent invocation.

    No --dangerously-skip-permissions, unlike jobs.py: that is defensible for a
    one-shot text prompt with no tools, not for an agent loose in a worktree of
    someone else's branch.
    """
    return [
        "claude", "-p", prompt,
        "--allowedTools", _ALLOWED_TOOLS,
        "--disallowedTools", _DENIED_TOOLS,
    ]


def _claude_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


def build_prompt(skill: str, scope_paths: list[str], existing_comments: str) -> str:
    paths = "\n".join(f"- {p}" for p in scope_paths) or "- (whole PR)"
    return (
        f"Run the `{skill}` skill's review mode against this checkout.\n\n"
        f"Review only these files:\n{paths}\n\n"
        "You have no network and no shell. Do not attempt to post anything.\n"
        "Existing review comments on this PR are quoted below; do not repeat them.\n"
        f"{existing_comments or '(none)'}\n\n"
        "Report every finding by printing ONE JSON object per line on stdout, "
        "with nothing else on that line:\n"
        '{"path": "<repo-relative path>", "line": <new-side line or null>, '
        '"start_line": <line or null>, "severity": "fix"|"consider", '
        '"lens": "<lens name>", "body": "<the finding>"}\n\n'
        "Anchor `line` to a line this PR actually adds. If a finding is about a "
        "file as a whole, omit `line`. If it is about no single file, omit `path`."
    )


def stage_placements(owner: str, repo: str, number: int, run_id: str,
                     source: str,
                     pairs: list[tuple[Placement, Finding]]) -> dict[str, int]:
    """Persist placed findings as staged records. Returns a per-tier count."""
    counts = {"anchored": 0, "file": 0, "pr": 0}

    def mutate(state: dict) -> dict:
        threads = {k: list(v) for k, v in state.get("comment_threads", {}).items()}
        notes = list(state.get("review_notes", []))
        for placement, finding in pairs:
            body = finding.body
            if placement.reason:
                body = f"{body}\n\n_(prview: {placement.reason})_"
            record = {
                "text": body,
                "staged": True,
                "source": source,
                "severity": finding.severity,
                "id": str(uuid.uuid4()),
                "run_id": run_id,
            }
            if placement.tier == "pr":
                notes.append(record)
            else:
                record["line"] = placement.line
                record["start_line"] = placement.start_line
                threads.setdefault(placement.path, []).append(record)
            counts[placement.tier] += 1
        state["comment_threads"] = threads
        state["review_notes"] = notes
        return state

    state_store.mutate_state(owner, repo, number, mutate)
    return counts


def finish_run(run: ReviewRun, returncode: int, output: str) -> None:
    """Settle a run's terminal status.

    A clean exit with nothing parseable is an error: a review that found nothing
    and a review we could not read must not look the same.
    """
    if run.status == "cancelled" or run._cancelled:
        run.status = "cancelled"
        return
    if returncode != 0:
        run.status = "error"
        run.error = run.error or f"claude exited {returncode}"
        return
    if run.staged == 0:
        parsed, _ = parse_findings(output)
        if not parsed:
            run.status = "error"
            run.error = ("the agent produced no findings in the expected format "
                         "— see the run log")
            return
    run.status = "done"


def _stage_chunk(run: ReviewRun, chunk: str,
                 added_by_path: dict[str, list[int]]) -> None:
    found, dropped = parse_findings(chunk)
    run.dropped += dropped
    if not found:
        return
    pairs = [(place(f, added_by_path), f) for f in found]
    run.demoted += sum(1 for placement, _ in pairs if placement.reason)
    counts = stage_placements(run.owner, run.repo, run.number, run.id,
                              run.skill, pairs)
    run.staged += sum(counts.values())


def _drain(run: ReviewRun, added_by_path: dict[str, list[int]]) -> None:
    """Read the log as it grows, staging complete lines as they appear."""
    seen = 0
    buffered = ""
    while True:
        try:
            text = Path(run.logfile).read_text(errors="replace")
        except OSError:
            text = ""
        fresh = text[seen:]
        if fresh:
            seen = len(text)
            buffered += fresh
            complete, _, buffered = buffered.rpartition("\n")
            if complete:
                _stage_chunk(run, complete + "\n", added_by_path)
        if (run._proc is None or run._proc.poll() is not None) and not fresh:
            break
        time.sleep(_POLL_SECONDS)
    # The final line may have no trailing newline, so rpartition never released it.
    if buffered.strip():
        _stage_chunk(run, buffered + "\n", added_by_path)


def _run_review(run: ReviewRun, prompt: str, worktree: str,
                added_by_path: dict[str, list[int]]) -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    run.logfile = str(_LOG_DIR / f"{run.owner}-{run.repo}-{run.number}-{run.id}.log")
    try:
        with open(run.logfile, "w") as fh:
            proc = subprocess.Popen(
                claude_argv(prompt), cwd=worktree, env=_claude_env(),
                stdout=fh, stderr=subprocess.STDOUT, text=True,
                start_new_session=True,
            )
    except FileNotFoundError:
        run.status = "error"
        run.error = "`claude` not found — install Claude Code and retry"
        return
    except Exception as exc:
        run.status = "error"
        run.error = str(exc)
        return

    run._proc = proc
    if run._cancelled:
        proc.kill()

    watcher = threading.Thread(target=_drain, args=(run, added_by_path), daemon=True)
    watcher.start()
    try:
        rc = proc.wait(timeout=REVIEW_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -9
        run.error = f"review timed out after {REVIEW_TIMEOUT}s"
    watcher.join(timeout=10)
    try:
        output = Path(run.logfile).read_text(errors="replace")
    except OSError:
        output = ""
    finish_run(run, rc, output)


def active_run_for(owner: str, repo: str, number: int) -> str | None:
    with _runs_lock:
        for run in _runs.values():
            if (run.owner, run.repo, run.number) == (owner, repo, number) \
                    and run.status == "running":
                return run.id
    return None


def start_review(owner: str, repo: str, number: int, skill: str, scope: str,
                 scope_paths: list[str], added_by_path: dict[str, list[int]],
                 existing_comments: str = "") -> str:
    """Prepare the worktree and start the agent. Returns the run id."""
    repo_path = repowise.resolve_repo_path(owner, repo)
    if repo_path is None:
        raise RuntimeError(
            f"no local clone configured for {owner}/{repo} — set a repo path first")
    worktree, _ = repowise.prepare_pr_worktree(repo_path, number)

    run = ReviewRun(id=str(uuid.uuid4()), owner=owner, repo=repo, number=number,
                    skill=skill, scope=scope)
    with _runs_lock:
        _runs[run.id] = run
    prompt = build_prompt(skill, scope_paths, existing_comments)
    threading.Thread(target=_run_review,
                     args=(run, prompt, worktree, added_by_path),
                     daemon=True).start()
    return run.id


def get_run(run_id: str) -> dict | None:
    with _runs_lock:
        run = _runs.get(run_id)
    if run is None:
        return None
    return {
        "id": run.id, "status": run.status, "skill": run.skill, "scope": run.scope,
        "staged": run.staged, "dropped": run.dropped, "demoted": run.demoted,
        "error": run.error, "elapsed": time.time() - run.started_at,
    }


def cancel_run(run_id: str) -> bool:
    """Kill the agent's process group. Findings already staged are kept."""
    with _runs_lock:
        run = _runs.get(run_id)
    if run is None or run.status != "running":
        return False
    run._cancelled = True
    run.status = "cancelled"
    proc = run._proc
    if proc is None:
        return True
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    return True


def clear_run(owner: str, repo: str, number: int, run_id: str) -> int:
    """Remove every staged record a run produced. Returns how many went."""
    removed = 0

    def mutate(state: dict) -> dict:
        nonlocal removed
        threads = {}
        for path, entries in state.get("comment_threads", {}).items():
            kept = [e for e in entries
                    if not (isinstance(e, dict) and e.get("run_id") == run_id)]
            removed += len(entries) - len(kept)
            if kept:
                threads[path] = kept
        notes = [n for n in state.get("review_notes", []) if n.get("run_id") != run_id]
        removed += len(state.get("review_notes", [])) - len(notes)
        state["comment_threads"] = threads
        state["review_notes"] = notes
        return state

    state_store.mutate_state(owner, repo, number, mutate)
    return removed
