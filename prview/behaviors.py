"""Behavior units for a PR: one per unit of work the author committed.

Pure — commit metadata in, ordered Behavior list out. A Behavior owns no diff
content; `filenames` are references into the PR's FileDiff list. Every PR file
belongs to exactly one Behavior, because the sidebar navigates files by array
index and a duplicated row would break that.
"""
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
