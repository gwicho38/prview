"""Review orders for a PR's changed files.

Pure: FileDiff values in, filename lists out. No subprocess, network, or
filesystem access — the server calls `orders_map` once per PR load and ships
every order to the client, so switching order never costs a round-trip.

The default order is `story`: schema and type definitions first, then core
logic, then the call sites that use it, then UI, config, tests, and generated
files. Alphabetical order — GitHub's — scatters a change across unrelated
directories; story order reads a PR the way it was written.
"""
import re

from .core import FileDiff


MODES = ("story", "complexity", "churn", "alpha")
DEFAULT_MODE = "story"
DEFAULT_TIER = 2

# First match wins, so precedence is the rule order here, not the tier number:
# generated and test files are recognised before the source patterns they would
# otherwise match (`tests/test_api.py` is a test, not an adapter).
_TIER_RULES = (
    (7, r"(^|/)(dist|build|vendor|node_modules)/|\.lock$|-lock\.json$|\.snap$"
        r"|\.min\.(js|css)$|\.generated\.|_pb2\.py$"),
    (6, r"(^|/)tests?/|(^|/)spec/|(^|/)test_[^/]+$|_test\.[^/]+$|\.(test|spec)\.[^/]+$"),
    (0, r"\.sql$|(^|/)migrations?/|\.proto$|openapi|\.graphql$|\.avsc$"
        r"|(^|/)schema[^/]*\.(json|ya?ml)$"),
    (5, r"(^|/)\.github/|(^|/)(Makefile|Dockerfile|pyproject\.toml)$"
        r"|docker-compose|\.(ya?ml|toml|ini|cfg|env)$|requirements[^/]*\.txt$"),
    (4, r"(^|/)(static|assets|public|components?|templates?|views?/templates)/"
        r"|\.(jsx|tsx|vue|svelte|css|scss|sass|less|html)$"),
    (1, r"(^|/)(models?|types?|schemas?|entities|dto|interfaces?)([._/]|$)"
        r"|[._](models?|types?|schemas?|dto)\.[^/]+$"),
    (3, r"(^|/)(server|routes?|router|handlers?|controllers?|endpoints?|api"
        r"|cli|main|app|middleware|adapters?|clients?|gh)([._/]|$)"),
    (2, r"(^|/)(core|domain|lib|services?|use_?cases?|logic|utils?|helpers?)([._/]|$)"),
)

_COMPILED_TIERS = tuple((tier, re.compile(pat)) for tier, pat in _TIER_RULES)

# Control-flow tokens: each one is a branch a reviewer has to hold in their head.
# `\?(?![.?=])` catches a ternary while skipping optional chaining and `??`.
_DECISION_RE = re.compile(
    r"\b(?:if|elif|else|for|while|switch|case|try|catch|except|match|when|guard)\b"
    r"|&&|\|\||\?(?![.?=])"
)

# Churn weight: enough that a large mechanical file outranks a lone guard clause,
# small enough that it never outweighs a genuine branch.
_CHURN_WEIGHT = 0.1


def story_tier(filename: str) -> int:
    """Narrative tier of a path — 0 (schema) through 7 (generated)."""
    for tier, pattern in _COMPILED_TIERS:
        if pattern.search(filename):
            return tier
    return DEFAULT_TIER


def _decisions(diff_text: str, marker: str) -> int:
    return sum(
        len(_DECISION_RE.findall(line[1:]))
        for line in diff_text.splitlines()
        if line.startswith(marker) and not line.startswith(marker * 3)
    )


def churn(fd: FileDiff) -> int:
    return fd.additions + fd.deletions


def complexity_delta(fd: FileDiff) -> float:
    """Net decision points this file's diff adds, nudged by its churn.

    Negative for a diff that removes more branches than it adds, so a
    simplification sorts last rather than masquerading as hard reading.
    """
    added = _decisions(fd.diff_text, "+")
    removed = _decisions(fd.diff_text, "-")
    return (added - removed) + _CHURN_WEIGHT * churn(fd)


_KEYS = {
    "story": lambda fd: (story_tier(fd.filename), -churn(fd), fd.filename),
    "complexity": lambda fd: (-complexity_delta(fd), -churn(fd), fd.filename),
    "churn": lambda fd: (-churn(fd), fd.filename),
    "alpha": lambda fd: (fd.filename,),
}


def order_names(files: list[FileDiff], mode: str) -> list[str]:
    """Filenames of `files` in `mode` order. An unknown mode falls back to churn."""
    key = _KEYS.get(mode, _KEYS["churn"])
    return [fd.filename for fd in sorted(files, key=key)]


def orders_map(files: list[FileDiff]) -> dict[str, list[str]]:
    """Every order, keyed by mode — the payload the client switches between."""
    return {mode: order_names(files, mode) for mode in MODES}
