import threading
import time

import prview.core as core
import prview.state_store as state_store
from prview.cache import CACHE_MISS, PRCache
from prview.core import FileDiff, PRInfo
from prview.state_store import list_resumable, mutate_state, pr_key


def test_pr_key_shared_by_cache_and_lock_registry():
    """pr_key is the single normalizer used by both the lock registry and cache."""
    key = pr_key("Owner", "Repo", 42)
    assert key == "Owner-Repo-42"

    lock_a = state_store._lock_for(pr_key("o", "r", 1))
    cache = PRCache()
    cache.set(pr_key("o", "r", 1), pr=PRInfo("o", "r", 1), files=[])
    # Same inputs → same key string → same lock object and same cache slot.
    assert state_store._lock_for(pr_key("o", "r", 1)) is lock_a
    assert cache.get(pr_key("o", "r", 1)) is not CACHE_MISS


def test_concurrent_mutations_same_pr_never_lose_update(tmp_path, monkeypatch):
    """Two real threads mutating the SAME PR key must not lose either update."""
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    state_store.reset_locks()
    ready = threading.Barrier(2)

    def append_viewed(name):
        def fn(state):
            # Read-modify-write with a widened window. Without the per-PR lock
            # both threads would read the same base list and one update would
            # be lost. The lock must serialize them so both names survive.
            new_viewed = state["viewed"] + [name]
            time.sleep(0.05)
            return {**state, "viewed": new_viewed}

        ready.wait()  # release both threads toward the lock simultaneously
        mutate_state("o", "r", 5, fn)

    t1 = threading.Thread(target=append_viewed, args=("alice",))
    t2 = threading.Thread(target=append_viewed, args=("bob",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    final = core.load_review_state("o", "r", 5)
    assert set(final["viewed"]) == {"alice", "bob"}, final["viewed"]


def test_locks_are_per_pr_keyed_and_do_not_serialize_across_prs(monkeypatch, tmp_path):
    """Mutations on different PR keys run concurrently — different locks, no blocking."""
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    state_store.reset_locks()
    order = []
    lock = threading.Lock()
    start = threading.Barrier(2)

    def slow_mutation(number, tag):
        def fn(state):
            start.wait()
            with lock:
                order.append(f"{tag}-enter")
            time.sleep(0.1)
            with lock:
                order.append(f"{tag}-exit")
            return state

        mutate_state("o", "r", number, fn)

    t1 = threading.Thread(target=slow_mutation, args=(1, "A"))
    t2 = threading.Thread(target=slow_mutation, args=(2, "B"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # Both threads enter their critical sections before either exits → genuinely
    # interleaved, not serialized (order between A/B is arbitrary). This ordering is
    # the deterministic proof of non-serialization; a wall-clock threshold would only
    # add timing flakiness under load without strengthening the guarantee.
    assert {order[0], order[1]} == {"A-enter", "B-enter"}, order


def test_list_resumable_scans_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    state_store.reset_locks()
    mutate_state("octo", "cat", 7, lambda s: {
        **s, "viewed": ["a.py", "b.py"], "flagged": {"c.py": "note"}, "submitted": True,
    })
    mutate_state("octo", "dog", 12, lambda s: {**s, "viewed": ["x.py"]})

    rows = list_resumable()
    by_number = {r["number"]: r for r in rows}

    assert by_number[7] == {
        "owner": "octo", "repo": "cat", "number": 7,
        "viewed_count": 2, "flagged_count": 1, "submitted": True,
    }
    assert by_number[12]["owner"] == "octo"
    assert by_number[12]["repo"] == "dog"
    assert by_number[12]["viewed_count"] == 1
    assert by_number[12]["flagged_count"] == 0
    assert by_number[12]["submitted"] is False


def test_cache_set_get_and_miss_sentinel():
    cache = PRCache()
    key = pr_key("o", "r", 3)
    assert cache.get(key) is CACHE_MISS

    pr = PRInfo("o", "r", 3, title="Fix")
    files = [FileDiff(filename="a.py", diff_text="diff")]
    cache.set(key, pr=pr, files=files)

    entry = cache.get(key)
    assert entry is not CACHE_MISS
    assert entry["pr"] is pr
    assert entry["files"] == files
    # Sentinel is distinguishable from a real (possibly falsy) entry.
    assert CACHE_MISS is not None
    assert bool(CACHE_MISS) is False or CACHE_MISS is not entry
