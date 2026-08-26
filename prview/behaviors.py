"""Behavior units for a PR: one per unit of work the author committed.

Pure — commit metadata in, ordered Behavior list out. A Behavior owns no diff
content; `filenames` are references into the PR's FileDiff list. Every PR file
belongs to exactly one Behavior, because the sidebar navigates files by array
index and a duplicated row would break that.
"""
import re
from dataclasses import dataclass, field


@dataclass
class Behavior:
    id: str
    title: str
    source_shas: tuple[str, ...]
    filenames: tuple[str, ...]
    also_in: dict[str, int] = field(default_factory=dict)
    noise: bool = False


def is_groupable(commits: list[dict]) -> bool:
    """True when the PR has enough authored commits to form a narrative."""
    return sum(1 for c in commits if not c.get("is_merge")) >= 2


def behaviors_from_commits(
    commits: list[dict],
    files_by_sha: dict[str, list[str]],
    pr_filenames: set[str],
) -> list[Behavior]:
    """One Behavior per non-merge commit, each owning the files it touched first.

    A merge commit's file list is not attributable to the author's intent, so
    merges are skipped. Files reachable from a commit but absent from the PR's
    final diff (force-pushed, reverted) are dropped, and a Behavior left with no
    files goes with them.
    """
    authored = [c for c in commits if not c.get("is_merge")]
    touches: dict[str, int] = {}
    for c in authored:
        for name in files_by_sha.get(c["sha"], []):
            if name in pr_filenames:
                touches[name] = touches.get(name, 0) + 1

    claimed: set[str] = set()
    out: list[Behavior] = []
    for c in authored:
        mine = [
            name
            for name in files_by_sha.get(c["sha"], [])
            if name in pr_filenames and name not in claimed
        ]
        if not mine:
            continue
        claimed.update(mine)
        out.append(Behavior(
            id="",
            title=c["subject"],
            source_shas=(c["sha"],),
            filenames=tuple(mine),
            also_in={n: touches[n] - 1 for n in mine if touches[n] > 1},
        ))

    for i, b in enumerate(out, start=1):
        b.id = f"b{i}"
    return out


# `b1 -> Title`, `b1+b2 → Title [noise]`. Prose around the mapping lines is fine;
# the content rules below are what make a hallucinated reply unusable.
_NAME_LINE_RE = re.compile(
    r"^\s*(b\d+(?:\s*\+\s*b\d+)*)\s*(?:->|→)\s*(.*?)\s*(\[noise\])?\s*$",
    re.IGNORECASE,
)


def apply_behavior_names(derived: list[Behavior], reply: str) -> list[Behavior] | None:
    """Apply an AI reply's renames, adjacent merges and noise flags to `derived`.

    Returns None — reject the whole reply, keep the commit-derived grouping — on
    an unknown id, a missing or repeated id, a non-adjacent merge, or an empty
    title. File lists are never read from the reply: a merge concatenates its
    inputs' files in order, so the AI cannot move a file between behaviors.
    """
    index = {b.id: i for i, b in enumerate(derived)}
    groups: list[tuple[list[int], str, bool]] = []
    used: list[int] = []

    for line in reply.splitlines():
        m = _NAME_LINE_RE.match(line)
        if not m:
            continue
        ids = [i.strip().lower() for i in m.group(1).split("+")]
        title = m.group(2).strip()
        if not title or any(i not in index for i in ids):
            return None
        positions = [index[i] for i in ids]
        if positions != list(range(positions[0], positions[0] + len(positions))):
            return None
        groups.append((positions, title, bool(m.group(3))))
        used.extend(positions)

    if sorted(used) != list(range(len(derived))):
        return None

    groups.sort(key=lambda g: g[0][0])
    out: list[Behavior] = []
    for n, (positions, title, noise) in enumerate(groups, start=1):
        members = [derived[p] for p in positions]
        merged_also: dict[str, int] = {}
        for b in members:
            merged_also.update(b.also_in)
        out.append(Behavior(
            id=f"b{n}",
            title=title,
            source_shas=tuple(s for b in members for s in b.source_shas),
            filenames=tuple(f for b in members for f in b.filenames),
            also_in=merged_also,
            noise=noise,
        ))
    return out
