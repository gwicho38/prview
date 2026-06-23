"""Side-effecting `gh` CLI wrappers, ported near-verbatim from the mcli
`pr-review` workflow (src 156-260).

Synchronous by design: these are called from sync `def` FastAPI routes, so
they may block the worker thread but never the event loop. The only behavioral
deviation from source is error handling — click.ClickException is replaced by a
structured GhError(message, hint) so the API can return actionable hints.

All argv is fixed; client-supplied strings (paths, comment bodies) are passed
as discrete argv elements and are never shell-interpolated.
"""
import json
import subprocess
from dataclasses import dataclass

from prview.core import PRInfo


@dataclass
class GhError(Exception):
    message: str
    hint: str = ""

    def __str__(self) -> str:
        return f"{self.message} ({self.hint})" if self.hint else self.message


_AUTH_HINT = "run `gh auth login`"
_MISSING_HINT = "install the GitHub CLI (https://cli.github.com) and run `gh auth login`"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a gh command, mapping a missing `gh` binary to a structured GhError.

    Without this, a missing binary raises FileNotFoundError → an unhandled 500
    with a leaked stack trace; this surfaces an actionable install hint instead.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise GhError("GitHub CLI (`gh`) not found", hint=_MISSING_HINT)


def fetch_pr_info(owner: str, repo: str, number: int) -> PRInfo:
    """Fetch PR metadata via gh CLI (src 156-201)."""
    fields = (
        "title,author,body,baseRefName,headRefName,state,"
        "reviewDecision,statusCheckRollup,additions,deletions,changedFiles"
    )
    result = _run(
        ["gh", "pr", "view", str(number), "--repo", f"{owner}/{repo}",
         "--json", fields],
    )
    if result.returncode != 0:
        raise GhError(
            f"Failed to fetch PR: {result.stderr.strip()}",
            hint=_AUTH_HINT,
        )

    data = json.loads(result.stdout)

    checks = data.get("statusCheckRollup", []) or []
    if not checks:
        ci = "none"
    elif all(c.get("conclusion") == "SUCCESS" for c in checks):
        ci = "pass"
    elif any(c.get("conclusion") == "FAILURE" for c in checks):
        ci = "fail"
    else:
        ci = "pending"

    author = data.get("author", {})
    author_login = author.get("login", "unknown") if isinstance(author, dict) else str(author)

    return PRInfo(
        owner=owner,
        repo=repo,
        number=number,
        title=data.get("title", ""),
        author=author_login,
        body=data.get("body", "") or "",
        base=data.get("baseRefName", ""),
        head=data.get("headRefName", ""),
        state=data.get("state", ""),
        review_decision=data.get("reviewDecision", "") or "",
        ci_status=ci,
        additions=data.get("additions", 0),
        deletions=data.get("deletions", 0),
        changed_files=data.get("changedFiles", 0),
    )


def fetch_pr_diff(owner: str, repo: str, number: int) -> str:
    """Fetch the full PR diff (src 204-212)."""
    result = _run(
        ["gh", "pr", "diff", str(number), "--repo", f"{owner}/{repo}"],
    )
    if result.returncode != 0:
        raise GhError(
            f"Failed to fetch diff: {result.stderr.strip()}",
            hint=_AUTH_HINT,
        )
    return result.stdout


def submit_review(owner: str, repo: str, number: int, event: str, body: str):
    """Submit a PR review via gh (src 215-223)."""
    flag_map = {"approve": "--approve", "request_changes": "--request-changes", "comment": "--comment"}
    flag = flag_map.get(event, "--comment")
    cmd = ["gh", "pr", "review", str(number), "--repo", f"{owner}/{repo}", flag]
    if body:
        cmd.extend(["--body", body])
    result = _run(cmd)
    return result.returncode == 0, result.stderr.strip()


def mark_file_viewed(owner: str, repo: str, number: int, path: str) -> bool:
    """Mark a file as viewed via GitHub GraphQL (src 226-250).

    Two-step: resolve the PR node id, then markFileAsViewed. Returns False
    (never raises) on graphql failure so the API can report a local-only save.
    """
    try:
        result = _run(
            ["gh", "pr", "view", str(number), "--repo", f"{owner}/{repo}",
             "--json", "id", "-q", ".id"],
        )
        if result.returncode != 0:
            return False
        pr_id = result.stdout.strip()

        mutation = (
            "mutation($prId: ID!, $path: String!) { "
            "markFileAsViewed(input: {pullRequestId: $prId, path: $path}) { "
            "clientMutationId } }"
        )
        result = _run(
            ["gh", "api", "graphql",
             "-f", f"query={mutation}",
             "-f", f"prId={pr_id}",
             "-f", f"path={path}"],
        )
        return result.returncode == 0
    except GhError:
        # Missing gh binary → treat as a failed remote sync (local save still
        # succeeds); the real error already surfaced at PR load.
        return False


def post_pr_comment(owner: str, repo: str, number: int, path: str, text: str) -> bool:
    """Post a general comment on a PR (src 253-260).

    Body keeps the `**{path}**\\n\\n{text}` prefix; text is a discrete argv
    element, never shell-interpolated.
    """
    body = f"**{path}**\n\n{text}"
    result = _run(
        ["gh", "pr", "comment", str(number), "--repo", f"{owner}/{repo}",
         "--body", body],
    )
    return result.returncode == 0
