"""Persistence + caching layer with per-PR write serialization.

Every mutating endpoint funnels through mutate_state: acquire the PR's lock,
load → fn(state) → save, all under the lock (last-write-wins on the whole
blob, matching core.collect_state). State load/save is delegated to core; this
module owns only the locking, the resumable scan, and the shared key.

Filesystem + in-memory only — no gh/claude calls here.
"""
import json
import threading

import prview.core as core

_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def pr_key(owner: str, repo: str, number: int) -> str:
    """The single PR-key normalizer shared by the lock registry and the cache."""
    return f"{owner}-{repo}-{number}"


def _lock_for(key: str) -> threading.Lock:
    with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def reset_locks():
    """Drop all per-PR locks. For test isolation only."""
    with _registry_lock:
        _locks.clear()


def mutate_state(owner: str, repo: str, number: int, fn) -> dict:
    """Locked read-modify-write for one PR. The choke point for all mutations.

    Takes explicit owner/repo/number (never a flattened key) so identities with
    hyphens — e.g. `my-org/my-repo` — are never mis-parsed. The PR identity is
    also persisted into the blob so list_resumable can recover it from content
    rather than from the (lossy) filename.
    """
    with _lock_for(pr_key(owner, repo, number)):
        state = core.load_review_state(owner, repo, number)
        new_state = {**fn(state), "owner": owner, "repo": repo, "number": number}
        core.save_review_state(owner, repo, number, new_state)
        return new_state


def list_resumable() -> list[dict]:
    """Scan the state dir for resumable reviews (GET /reviews).

    Reads each PR's identity from the blob contents (written by mutate_state),
    not from the filename — the `{owner}-{repo}-{number}.json` scheme is
    ambiguous for hyphenated owner/repo names.
    """
    state_dir = core._CACHE_DIR
    if not state_dir.exists():
        return []

    rows: list[dict] = []
    for path in sorted(state_dir.glob("*.json")):
        try:
            blob = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        owner, repo, number = blob.get("owner"), blob.get("repo"), blob.get("number")
        if owner is None or repo is None or number is None:
            continue  # written before identity was persisted; skip rather than guess
        rows.append({
            "owner": owner,
            "repo": repo,
            "number": int(number),
            "viewed_count": len(blob.get("viewed", [])),
            "flagged_count": len(blob.get("flagged", {})),
            "submitted": bool(blob.get("submitted", False)),
        })
    return rows
