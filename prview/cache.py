"""In-process PR+diff cache. Never persisted.

Keyed by the normalized PR key (see state_store.pr_key). AI/file endpoints
read PRInfo + parsed chunks from here; a miss returns CACHE_MISS so callers
can map it to a 409 without colliding with a real (possibly falsy) entry.
"""
from prview.core import FileDiff, PRInfo


class _CacheMiss:
    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "CACHE_MISS"


CACHE_MISS = _CacheMiss()


class PRCache:
    def __init__(self):
        self._store: dict[str, dict] = {}

    def set(self, key: str, *, pr: PRInfo, files: list[FileDiff]):
        self._store[key] = {"pr": pr, "files": files}

    def get(self, key: str):
        return self._store.get(key, CACHE_MISS)
