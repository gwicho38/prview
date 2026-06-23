"""Pure functional core for prview.

No subprocess, network, or other side-effecting imports at module load.
Ported verbatim from the mcli `pr-review` workflow with three documented
deviations:
  1. _CACHE_DIR moved from ~/.mcli/cache/pr-review to ~/.prview/state.
  2. parse_pr_ref raises a typed ValueError instead of click.BadParameter
     (no click dependency).
  3. On-disk state schema gains an additive `submitted: bool = False` field.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path


_CACHE_DIR = Path.home() / ".prview" / "state"


def _state_path(owner: str, repo: str, number: int) -> Path:
    return _CACHE_DIR / f"{owner}-{repo}-{number}.json"


def load_review_state(owner: str, repo: str, number: int) -> dict:
    """Load persisted review state for a PR."""
    defaults = {"viewed": [], "flagged": {}, "comments": 0, "submitted": False}
    path = _state_path(owner, repo, number)
    if path.exists():
        try:
            return {**defaults, **json.loads(path.read_text())}
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def save_review_state(owner: str, repo: str, number: int, state: dict):
    """Persist review state for a PR."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path(owner, repo, number).write_text(json.dumps(state, indent=2) + "\n")


def apply_saved_state(files: list, state: dict):
    """Apply saved viewed/flagged state to file list."""
    viewed_set = set(state.get("viewed", []))
    flagged_map = state.get("flagged", {})
    for fd in files:
        if fd.filename in viewed_set:
            fd.viewed = True
        if fd.filename in flagged_map:
            fd.flagged = True
            fd.flag_note = flagged_map[fd.filename]


def collect_state(files: list, comments_posted: int) -> dict:
    """Collect current review state from file list."""
    return {
        "viewed": [f.filename for f in files if f.viewed],
        "flagged": {f.filename: f.flag_note for f in files if f.flagged},
        "comments": comments_posted,
    }


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PRInfo:
    owner: str
    repo: str
    number: int
    title: str = ""
    author: str = ""
    body: str = ""
    base: str = ""
    head: str = ""
    state: str = ""
    review_decision: str = ""
    ci_status: str = ""
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0


@dataclass
class FileDiff:
    filename: str
    diff_text: str
    additions: int = 0
    deletions: int = 0
    flagged: bool = False
    flag_note: str = ""
    viewed: bool = False


# ---------------------------------------------------------------------------
# PR reference parsing
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)"
)
_OWNER_REPO_RE = re.compile(r"^([^/]+)/([^#]+)#(\d+)$")


def parse_pr_ref(ref: str) -> tuple[str | None, str | None, int]:
    """Parse a PR reference into (owner, repo, number).

    Accepts a full GitHub URL or `owner/repo#number`. A bare number is
    rejected with a typed ValueError, as is any unparseable input.
    """
    # Full URL
    m = _URL_RE.search(ref)
    if m:
        return m.group(1), m.group(2), int(m.group(3))

    # owner/repo#number
    m = _OWNER_REPO_RE.match(ref)
    if m:
        return m.group(1), m.group(2), int(m.group(3))

    raise ValueError(f"Cannot parse PR reference: {ref}")


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)


def parse_diff(raw: str) -> list[FileDiff]:
    """Split a unified diff into per-file chunks."""
    files: list[FileDiff] = []
    parts = re.split(r"(?=^diff --git )", raw, flags=re.MULTILINE)

    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = _DIFF_HEADER_RE.match(part)
        if not m:
            continue
        filename = m.group(2)
        adds = part.count("\n+") - part.count("\n+++")
        dels = part.count("\n-") - part.count("\n---")
        files.append(FileDiff(
            filename=filename,
            diff_text=part,
            additions=max(adds, 0),
            deletions=max(dels, 0),
        ))

    return files


# ---------------------------------------------------------------------------
# Prompt builders (pure)
# ---------------------------------------------------------------------------

def build_summary_prompt(pr: PRInfo, fd: FileDiff) -> str:
    """Assemble the 1-2 sentence file-summary prompt (src 367-380)."""
    diff_preview = fd.diff_text[:4000]
    return (
        f"PR: {pr.title} by {pr.author}\n"
        f"File: {fd.filename} (+{fd.additions} -{fd.deletions})\n"
        f"Diff:\n```diff\n{diff_preview}\n```\n\n"
        "In 1-2 sentences, summarize what changed in this file and why. Be direct."
    )


def build_explain_prompt(pr: PRInfo, fd: FileDiff) -> str:
    """Assemble the code-explanation prompt (src 490-502)."""
    return (
        f"You are a code reviewer.\n\n"
        f"PR: {pr.title} (#{pr.number}) by {pr.author}\n\n"
        f"File: {fd.filename}\n"
        f"Diff:\n```diff\n{fd.diff_text[:8000]}\n```\n\n"
        f"Explain the code in this file. Focus on:\n"
        f"- What does this file do? What is its role in the codebase?\n"
        f"- Walk through the key functions, classes, or data structures line by line\n"
        f"- How do the changed/added parts work mechanically?\n"
        f"- Flag any bugs, logic errors, or edge cases in the implementation\n\n"
        f"Do NOT summarize the PR or describe what changed at a high level. "
        f"Explain the actual code — what it does, how it works, and what could break."
    )


def build_ask_prompt(pr: PRInfo, fd: FileDiff, question: str) -> str:
    """Assemble the ask-about-file prompt (src 514-522)."""
    return (
        f"You are reviewing a pull request.\n\n"
        f"PR: {pr.title} (#{pr.number}) by {pr.author}\n"
        f"Description: {pr.body[:1000]}\n\n"
        f"File: {fd.filename}\n"
        f"Diff:\n```diff\n{fd.diff_text[:8000]}\n```\n\n"
        f"User question: {question}\n\n"
        f"Answer concisely based on the diff and PR context."
    )
