#!/usr/bin/env python3
# @description: Interactive file-by-file PR review with Claude-powered explanations
# @version: 1.0.0
# @requires: rich, click
"""
Interactive PR review.

Usage:
  mcli run -g pr-review 123              — Review PR #123 in current repo
  mcli run -g pr-review owner/repo#123   — Review PR in specific repo
  mcli run -g pr-review <github-url>     — Review PR from URL
"""
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import click


# ---------------------------------------------------------------------------
# Review state persistence
# ---------------------------------------------------------------------------

_CACHE_DIR = Path.home() / ".mcli" / "cache" / "pr-review"


def _state_path(owner: str, repo: str, number: int) -> Path:
    return _CACHE_DIR / f"{owner}-{repo}-{number}.json"


def load_review_state(owner: str, repo: str, number: int) -> dict:
    """Load persisted review state for a PR."""
    path = _state_path(owner, repo, number)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"viewed": [], "flagged": {}, "comments": 0}


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

    Returns (None, None, number) if only a number is given — repo resolved later.
    """
    # Full URL
    m = _URL_RE.search(ref)
    if m:
        return m.group(1), m.group(2), int(m.group(3))

    # owner/repo#number
    m = _OWNER_REPO_RE.match(ref)
    if m:
        return m.group(1), m.group(2), int(m.group(3))

    # Plain number
    try:
        return None, None, int(ref)
    except ValueError:
        raise click.BadParameter(f"Cannot parse PR reference: {ref}")


def resolve_repo() -> tuple[str, str]:
    """Get owner/repo from current directory via gh."""
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(
            "Not in a GitHub repo or gh not authenticated.\n" + result.stderr.strip()
        )
    parts = result.stdout.strip().split("/")
    if len(parts) != 2:
        raise click.ClickException(f"Unexpected repo format: {result.stdout.strip()}")
    return parts[0], parts[1]


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def fetch_pr_info(owner: str, repo: str, number: int) -> PRInfo:
    """Fetch PR metadata via gh CLI."""
    fields = (
        "title,author,body,baseRefName,headRefName,state,"
        "reviewDecision,statusCheckRollup,additions,deletions,changedFiles"
    )
    result = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", f"{owner}/{repo}",
         "--json", fields],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(f"Failed to fetch PR: {result.stderr.strip()}")

    data = json.loads(result.stdout)

    # CI status from statusCheckRollup
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
    """Fetch the full PR diff."""
    result = subprocess.run(
        ["gh", "pr", "diff", str(number), "--repo", f"{owner}/{repo}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(f"Failed to fetch diff: {result.stderr.strip()}")
    return result.stdout


def submit_review(owner: str, repo: str, number: int, event: str, body: str):
    """Submit a PR review via gh."""
    flag_map = {"approve": "--approve", "request_changes": "--request-changes", "comment": "--comment"}
    flag = flag_map.get(event, "--comment")
    cmd = ["gh", "pr", "review", str(number), "--repo", f"{owner}/{repo}", flag]
    if body:
        cmd.extend(["--body", body])
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


def mark_file_viewed(owner: str, repo: str, number: int, path: str) -> bool:
    """Mark a file as viewed on a PR via GitHub GraphQL API."""
    # Get the PR node ID
    result = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", f"{owner}/{repo}",
         "--json", "id", "-q", ".id"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    pr_id = result.stdout.strip()

    mutation = (
        "mutation($prId: ID!, $path: String!) { "
        "markFileAsViewed(input: {pullRequestId: $prId, path: $path}) { "
        "clientMutationId } }"
    )
    result = subprocess.run(
        ["gh", "api", "graphql",
         "-f", f"query={mutation}",
         "-f", f"prId={pr_id}",
         "-f", f"path={path}"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def post_pr_comment(owner: str, repo: str, number: int, body: str):
    """Post a general comment on a PR."""
    result = subprocess.run(
        ["gh", "pr", "comment", str(number), "--repo", f"{owner}/{repo}",
         "--body", body],
        capture_output=True, text=True,
    )
    return result.returncode == 0


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
# Claude helper
# ---------------------------------------------------------------------------

def invoke_claude(prompt: str, timeout: int = 300) -> str:
    """Invoke Claude CLI in print mode."""
    cmd = ["claude", "--print", "--dangerously-skip-permissions", "-p", prompt]
    env = {k: v for k, v in subprocess.os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI failed (exit {result.returncode}): {result.stderr[:500]}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def show_summary(pr: PRInfo):
    """Print PR summary panel."""
    from rich.console import Console
    from rich.panel import Panel
    console = Console()

    ci_icon = {"pass": "[green]pass[/green]", "fail": "[red]fail[/red]",
               "pending": "[yellow]pending[/yellow]", "none": "[dim]none[/dim]"}
    review_icon = {
        "APPROVED": "[green]approved[/green]",
        "CHANGES_REQUESTED": "[red]changes requested[/red]",
        "": "[dim]no reviews[/dim]",
    }

    text = (
        f"[bold]{pr.title}[/bold]\n"
        f"[dim]{pr.author}[/dim] | {pr.base} <- {pr.head}\n\n"
        f"Files: {pr.changed_files}  "
        f"[green]+{pr.additions}[/green] [red]-{pr.deletions}[/red]\n"
        f"CI: {ci_icon.get(pr.ci_status, pr.ci_status)}  "
        f"Review: {review_icon.get(pr.review_decision, pr.review_decision)}"
    )
    console.print(Panel(text, title=f"PR #{pr.number}", border_style="blue"))


def _has_delta() -> bool:
    """Check if delta is available."""
    return subprocess.run(["delta", "--version"], capture_output=True).returncode == 0


def _has_bat() -> bool:
    """Check if bat is available."""
    return subprocess.run(["bat", "--version"], capture_output=True).returncode == 0


def _show_diff_inline(text: str):
    """Display diff inline via delta (side-by-side) or bat fallback.

    Prints directly to the terminal — no pager, stays in context.
    """
    if _has_delta():
        subprocess.run(
            ["delta", "--side-by-side", "--paging", "never"],
            input=text, text=True,
        )
    elif _has_bat():
        subprocess.run(
            ["bat", "--language", "diff", "--style", "plain", "--paging=never"],
            input=text, text=True,
        )
    else:
        print(text)


def summarize_file_change(pr: PRInfo, fd: FileDiff) -> str:
    """Get a 1-2 sentence AI summary of what changed in this file."""
    # Keep the diff short for the summary — just enough for context
    diff_preview = fd.diff_text[:4000]
    prompt = (
        f"PR: {pr.title} by {pr.author}\n"
        f"File: {fd.filename} (+{fd.additions} -{fd.deletions})\n"
        f"Diff:\n```diff\n{diff_preview}\n```\n\n"
        "In 1-2 sentences, summarize what changed in this file and why. Be direct."
    )
    try:
        return invoke_claude(prompt, timeout=60)
    except (RuntimeError, subprocess.TimeoutExpired):
        return ""


def show_file_diff(pr: PRInfo, fd: FileDiff):
    """Show file header with AI summary, then inline diff."""
    from rich.console import Console
    from rich.panel import Panel
    console = Console()

    header = (
        f"[bold]{fd.filename}[/bold]  "
        f"[green]+{fd.additions}[/green] [red]-{fd.deletions}[/red]"
    )
    if fd.viewed:
        header += "  [green]VIEWED[/green]"
    if fd.flagged:
        header += "  [yellow]FLAGGED[/yellow]"

    console.print(f"\n{header}")

    # AI summary before the diff
    console.print("[dim]Summarizing...[/dim]", end="\r")
    summary = summarize_file_change(pr, fd)
    if summary:
        console.print(Panel(summary, border_style="cyan", title="Summary"))
    else:
        console.print("")  # clear the "Summarizing..." line

    _show_diff_inline(fd.diff_text)


# ---------------------------------------------------------------------------
# Review loop
# ---------------------------------------------------------------------------

def review_loop(pr: PRInfo, files: list[FileDiff]):
    """Main file-by-file review loop."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt
    console = Console()

    comments_posted = 0
    total = len(files)
    already_viewed = sum(1 for f in files if f.viewed)

    if already_viewed:
        console.print(
            f"[dim]{already_viewed}/{total} files already viewed from previous session.[/dim]"
        )

    # Find first unviewed file to start at
    idx = 0
    while idx < total and files[idx].viewed:
        console.print(
            f"[dim]\\[{idx + 1}/{total}] {files[idx].filename} — already viewed[/dim]"
        )
        idx += 1

    while idx < total:
        fd = files[idx]

        if not fd.viewed:
            show_file_diff(pr, fd)

        while True:
            status = "[green]VIEWED[/green] " if fd.viewed else ""
            action = Prompt.ask(
                f"{status}\\[{idx + 1}/{total}] "
                f"\\[v]iewed  \\[e]xplain  \\[a]sk  \\[c]omment  \\[f]lag  "
                f"\\[s]kip  \\[b]ack  \\[q]uit  \\[d]iff",
                default="v" if not fd.viewed else "s",
            )
            action = action.lower().strip()

            if action == "d":
                show_file_diff(pr, fd)
                continue

            if action == "b":
                if idx > 0:
                    idx -= 1
                else:
                    console.print("[dim]Already at first file.[/dim]")
                break

            if action == "q":
                console.print("[yellow]Jumping to review summary.[/yellow]")
                save_review_state(pr.owner, pr.repo, pr.number,
                                  collect_state(files, comments_posted))
                return comments_posted

            if action == "s":
                idx += 1
                break

            if action == "v":
                ok = mark_file_viewed(pr.owner, pr.repo, pr.number, fd.filename)
                fd.viewed = True
                if ok:
                    console.print(f"[green]Viewed: {fd.filename}[/green]")
                else:
                    console.print(f"[yellow]Marked locally (GitHub API call failed)[/yellow]")
                save_review_state(pr.owner, pr.repo, pr.number,
                                  collect_state(files, comments_posted))
                idx += 1
                break

            if action == "e":
                console.print("[blue]Analyzing...[/blue]")
                prompt = (
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
                try:
                    explanation = invoke_claude(prompt)
                    console.print(Panel(explanation, title="Explanation", border_style="green"))
                except (RuntimeError, subprocess.TimeoutExpired) as e:
                    console.print(f"[red]Error: {e}[/red]")
                continue

            if action == "a":
                question = Prompt.ask("Ask about this file")
                if question.strip():
                    console.print("[blue]Thinking...[/blue]")
                    prompt = (
                        f"You are reviewing a pull request.\n\n"
                        f"PR: {pr.title} (#{pr.number}) by {pr.author}\n"
                        f"Description: {pr.body[:1000]}\n\n"
                        f"File: {fd.filename}\n"
                        f"Diff:\n```diff\n{fd.diff_text[:8000]}\n```\n\n"
                        f"User question: {question}\n\n"
                        f"Answer concisely based on the diff and PR context."
                    )
                    try:
                        answer = invoke_claude(prompt)
                        console.print(Panel(answer, title="Answer", border_style="green"))
                    except (RuntimeError, subprocess.TimeoutExpired) as e:
                        console.print(f"[red]Error: {e}[/red]")
                continue

            if action == "c":
                comment_text = Prompt.ask("Comment")
                if comment_text.strip():
                    ok = post_pr_comment(pr.owner, pr.repo, pr.number,
                                         f"**{fd.filename}**\n\n{comment_text}")
                    if ok:
                        comments_posted += 1
                        console.print("[green]Comment posted.[/green]")
                    else:
                        console.print("[red]Failed to post comment.[/red]")
                continue

            if action == "f":
                note = Prompt.ask("Flag note (optional)", default="")
                fd.flagged = True
                fd.flag_note = note
                console.print(f"[yellow]Flagged: {fd.filename}[/yellow]")
                save_review_state(pr.owner, pr.repo, pr.number,
                                  collect_state(files, comments_posted))
                continue

    save_review_state(pr.owner, pr.repo, pr.number,
                      collect_state(files, comments_posted))
    return comments_posted


def end_of_review(pr: PRInfo, files: list[FileDiff], comments_posted: int):
    """Show summary and submit review."""
    from rich.console import Console
    from rich.table import Table
    from rich.prompt import Prompt
    console = Console()

    flagged = [f for f in files if f.flagged]
    viewed = sum(1 for f in files if f.viewed)
    skipped = len(files) - viewed - len(flagged)

    console.print("\n[bold]Review Summary[/bold]")
    console.print(f"  Files: {len(files)}  [green]Viewed: {viewed}[/green]  [yellow]Flagged: {len(flagged)}[/yellow]  [dim]Skipped: {skipped}[/dim]")
    console.print(f"  Comments posted: {comments_posted}")

    if flagged:
        table = Table(title="Flagged Files")
        table.add_column("File", style="bold")
        table.add_column("Note")
        table.add_column("+/-")
        for f in flagged:
            table.add_row(f.filename, f.flag_note or "-",
                          f"[green]+{f.additions}[/green] [red]-{f.deletions}[/red]")
        console.print(table)

    action = Prompt.ask(
        "\nSubmit review: \\[a]pprove  \\[r]equest changes  \\[c]omment only  \\[q]uit",
        default="q",
    )

    event_map = {"a": "approve", "r": "request_changes", "c": "comment"}
    event = event_map.get(action.lower())

    if not event:
        console.print("[dim]No review submitted.[/dim]")
        return

    body = ""
    if flagged:
        body = "**Flagged files:**\n"
        for f in flagged:
            body += f"- `{f.filename}`"
            if f.flag_note:
                body += f" — {f.flag_note}"
            body += "\n"

    ok, err = submit_review(pr.owner, pr.repo, pr.number, event, body)
    if ok:
        label = {"approve": "Approved", "request_changes": "Changes requested",
                 "comment": "Comment submitted"}.get(event, event)
        console.print(f"[green]{label}.[/green]")
    else:
        console.print(f"[red]Failed to submit review: {err}[/red]")


# ---------------------------------------------------------------------------
# Click entry point
# ---------------------------------------------------------------------------

@click.command(name="pr-review")
@click.argument("pr_ref")
def app(pr_ref):
    """Interactive file-by-file PR review with Claude-powered explanations.

    \b
    PR_REF can be:
      123                PR number (uses current repo)
      owner/repo#123     Explicit repo
      <github-url>       Full PR URL
    """
    from rich.console import Console
    console = Console()

    # Parse reference
    owner, repo, number = parse_pr_ref(pr_ref)
    if not owner or not repo:
        owner, repo = resolve_repo()

    console.print(f"[dim]Fetching {owner}/{repo}#{number}...[/dim]")

    # Fetch PR info and diff
    pr = fetch_pr_info(owner, repo, number)
    show_summary(pr)

    raw_diff = fetch_pr_diff(owner, repo, number)
    files = parse_diff(raw_diff)

    if not files:
        console.print("[yellow]No file changes found in this PR.[/yellow]")
        return

    # Sort by change size (most changed first)
    files.sort(key=lambda f: f.additions + f.deletions, reverse=True)

    # Restore saved review state
    saved = load_review_state(owner, repo, number)
    apply_saved_state(files, saved)

    viewed = sum(1 for f in files if f.viewed)
    remaining = len(files) - viewed
    console.print(f"\n[bold]{len(files)} files[/bold]  [green]{viewed} viewed[/green]  {remaining} remaining\n")

    comments = review_loop(pr, files)
    end_of_review(pr, files, comments)


if __name__ == "__main__":
    app()
