"""Agent findings: parse a JSONL stream, then place each finding on the diff.

Pure — no subprocess, no I/O, no state. The agent's stdout goes in, placements
come out.

Findings are validated one at a time, unlike behaviors.apply_behavior_names
which rejects a whole reply: a behavior grouping must partition every file, so a
partial answer there is incoherent, whereas findings are independent.
"""
import json
from dataclasses import dataclass

MAX_FINDINGS_PER_RUN = 200


@dataclass(frozen=True)
class Finding:
    path: str | None
    line: int | None
    start_line: int | None
    severity: str | None
    body: str
    lens: str | None


@dataclass(frozen=True)
class Placement:
    tier: str                  # anchored | file | pr
    path: str | None
    line: int | None
    start_line: int | None
    reason: str | None         # why it moved; None when it landed as claimed


def _str_or_none(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _finding_from(obj: dict) -> Finding | None:
    body = obj.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    line, start = obj.get("line"), obj.get("start_line")
    return Finding(
        path=_str_or_none(obj.get("path")),
        # bool is an int subclass; a JSON true would otherwise read as line 1.
        line=line if isinstance(line, int) and not isinstance(line, bool) else None,
        start_line=start if isinstance(start, int) and not isinstance(start, bool) else None,
        severity=_str_or_none(obj.get("severity")),
        body=body.strip(),
        lens=_str_or_none(obj.get("lens")),
    )


def parse_findings(text: str) -> tuple[list[Finding], int]:
    """Findings from a JSONL stream, plus the number of lines discarded.

    Only `{`-prefixed lines are candidates, so the agent's prose (and bracketed
    log noise) is skipped rather than counted; a candidate that cannot become a
    finding is a drop worth reporting, because a finding was lost.
    """
    out: list[Finding] = []
    dropped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            dropped += 1
            continue
        finding = _finding_from(obj)
        if finding is None or len(out) >= MAX_FINDINGS_PER_RUN:
            dropped += 1
            continue
        out.append(finding)
    return out, dropped


def place(finding: Finding, added_by_path: dict[str, list[int]]) -> Placement:
    """Where a finding may legally attach, demoting rather than guessing.

    A comment anchored to a line the diff never touches is worse than an
    unanchored one, so an invalid anchor falls back to the file and an unknown
    file falls back to the PR.
    """
    if finding.path is None:
        return Placement("pr", None, None, None, None)

    if finding.path not in added_by_path:
        return Placement("pr", None, None, None,
                         f"{finding.path} is not a file this PR changes")

    if finding.line is None:
        return Placement("file", finding.path, None, None, None)

    added = set(added_by_path[finding.path])
    start = finding.start_line
    anchorable = finding.line in added and (
        start is None or (start in added and start < finding.line)
    )
    if not anchorable:
        return Placement("file", finding.path, None, None,
                         f"line {finding.line} is not in the diff for {finding.path}")

    return Placement("anchored", finding.path, finding.line, start, None)
