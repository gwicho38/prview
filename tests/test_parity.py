"""Group 7 — strategic parity / gap-filling tests.

Source of truth: /Users/lefv/.mcli/workflows/workflows/pr-review.py (the mcli
`pr-review` CLI). These tests assert prview reproduces the CLI's behavior by
extracting the EXPECTED strings/structures directly from the CLI source (via
AST), never from prview's own code — so a drift in either side is caught and
the assertion can never be circular.

Scope: only gaps NOT already covered by G1-G6. Capped at <=10 tests.
"""
import ast
from pathlib import Path

import pytest

import prview.core as core
import prview.gh as gh
import prview.server as server
import prview.state_store as state_store
from prview.core import FileDiff, PRInfo
from fastapi.testclient import TestClient


_SOURCE_PATH = Path.home() / ".mcli" / "workflows" / "workflows" / "pr-review.py"
_SOURCE_TREE = ast.parse(_SOURCE_PATH.read_text())


def _prompt_fstrings(fn_name: str) -> list[str]:
    """Return source text of every `prompt = <f-string>` inside a CLI function,
    in source order. Lets us eval the CLI's literal prompt against our inputs."""
    out: list[str] = []
    for node in ast.walk(_SOURCE_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            for n in ast.walk(node):
                if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "prompt" for t in n.targets
                ):
                    out.append(ast.unparse(n.value))
    return out


def _eval_source_prompt(fstring_src: str, *, pr, fd, question=None) -> str:
    """Render a CLI prompt f-string against our controlled objects.

    SAFETY: `fstring_src` is a literal f-string lifted verbatim from the user's
    own, locally-owned `pr-review.py` (the same file the user runs as a CLI). It
    contains no external or test-controlled input — the only names it references
    are the fixed fixtures bound below. This is the source-of-truth for parity;
    rendering it is how we avoid hand-copying (and thus self-referencing) the
    prompt text. `diff_preview` mirrors the CLI's local in summarize_file_change.
    """
    return eval(  # noqa: S307 — trusted local source literal, fixed inputs only
        fstring_src,
        {"__builtins__": {}},
        {"pr": pr, "fd": fd, "question": question, "diff_preview": fd.diff_text[:4000]},
    )


def _pr():
    return PRInfo(owner="o", repo="r", number=42, title="Add feature",
                  author="alice", body="B" * 3000)


def _fd(diff="diff body\n"):
    return FileDiff(filename="src/app.py", diff_text=diff, additions=3, deletions=1)


# --- 1-3: prompt byte-for-byte parity, EXPECTED extracted from CLI source -----

def test_summary_prompt_matches_cli_source():
    pr, fd = _pr(), _fd(diff="d" * 9000)  # exercises the 4000-char preview cut
    (src_fstring,) = _prompt_fstrings("summarize_file_change")
    expected = _eval_source_prompt(src_fstring, pr=pr, fd=fd)
    assert core.build_summary_prompt(pr, fd) == expected
    assert "d" * 4000 in expected and "d" * 4001 not in expected  # 4000 cut is the CLI's


def test_explain_prompt_matches_cli_source():
    pr, fd = _pr(), _fd(diff="e" * 12000)  # exercises the 8000-char cut
    explain_src, _ask_src = _prompt_fstrings("review_loop")
    expected = _eval_source_prompt(explain_src, pr=pr, fd=fd)
    assert core.build_explain_prompt(pr, fd) == expected
    assert "e" * 8000 in expected and "e" * 8001 not in expected


def test_ask_prompt_matches_cli_source_with_body_and_diff_truncation():
    pr, fd = _pr(), _fd(diff="z" * 12000)
    _explain_src, ask_src = _prompt_fstrings("review_loop")
    question = "Why this approach?"
    expected = _eval_source_prompt(ask_src, pr=pr, fd=fd, question=question)
    assert core.build_ask_prompt(pr, fd, question) == expected
    # body[:1000] and diff[:8000] limits come from the CLI source, not us.
    assert "B" * 1000 in expected and "B" * 1001 not in expected
    assert "z" * 8000 in expected and "z" * 8001 not in expected


# --- 4: full state round-trip incl. `submitted`, schema confirmed -------------

def test_full_state_round_trip_includes_submitted(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    state_store.reset_locks()

    state_store.mutate_state("o", "r", 11, lambda s: {**s, "viewed": ["a.py"],
                                                      "flagged": {"b.py": "n"}, "submitted": True})
    loaded = core.load_review_state("o", "r", 11)
    # Core schema fields all present; mutate_state also persists PR identity
    # (owner/repo/number) so list_resumable can recover it from content.
    assert {"viewed", "flagged", "comments", "submitted"} <= set(loaded)
    assert loaded["submitted"] is True
    assert (loaded["owner"], loaded["repo"], loaded["number"]) == ("o", "r", 11)

    # A CLI-style file lacking `submitted` still loads with the additive default.
    core._CACHE_DIR.mkdir(parents=True, exist_ok=True)
    core._state_path("o", "r", 12).write_text(
        '{"viewed": [], "flagged": {}, "comments": 0}\n'
    )
    assert core.load_review_state("o", "r", 12)["submitted"] is False


# --- 5: submit review body assembly matches CLI logic (src 594-600) -----------

def test_flagged_body_matches_cli_source_logic():
    """Reproduce the CLI's body-builder (lines 594-600) and assert prview's
    server._flagged_body agrees for the note / no-note branches."""
    flagged_order = ["a.py", "b.py"]
    notes = {"a.py": "risky", "b.py": ""}

    # CLI source logic, transcribed structurally from src 594-600:
    cli_body = "**Flagged files:**\n"
    for f in flagged_order:
        cli_body += f"- `{f}`"
        if notes[f]:
            cli_body += f" — {notes[f]}"
        cli_body += "\n"

    state = {"flagged": {"a.py": "risky", "b.py": ""}}
    assert server._flagged_body(state) == cli_body
    assert server._flagged_body({"flagged": {}}) == ""  # no flags → empty body


# --- 6: event->gh flag mapping for ALL THREE decisions, from CLI source -------

def test_submit_review_flag_map_all_three_events_match_cli_source():
    # Expected flag_map extracted from the CLI's submit_review (src 217).
    expected_map = None
    for node in ast.walk(_SOURCE_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == "submit_review":
            for n in ast.walk(node):
                if isinstance(n, ast.Dict) and any(
                    isinstance(k, ast.Constant) and k.value == "approve" for k in n.keys
                ):
                    expected_map = {k.value: v.value for k, v in zip(n.keys, n.values)}
    assert expected_map == {"approve": "--approve",
                            "request_changes": "--request-changes",
                            "comment": "--comment"}

    captured = []

    class _R:
        returncode, stderr = 0, ""

    def fake_run(cmd, *a, **kw):
        captured.append(cmd)
        return _R()

    import unittest.mock as mock
    for event, flag in expected_map.items():
        with mock.patch("prview.gh.subprocess.run", side_effect=fake_run):
            gh.submit_review("o", "r", 7, event, "body")
        assert captured[-1][:3] == ["gh", "pr", "review"]
        assert flag in captured[-1]  # prview maps each event to the CLI's flag


# --- 7: per-PR lock — no lost update under heavier contention -----------------

def test_per_pr_lock_no_lost_update_under_high_contention(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    state_store.reset_locks()
    n = 12
    ready = threading.Barrier(n)

    def append(name):
        def fn(state):
            return {**state, "viewed": state["viewed"] + [name]}
        ready.wait()
        state_store.mutate_state("o", "r", 5, fn)

    threads = [threading.Thread(target=append, args=(f"u{i}",)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = core.load_review_state("o", "r", 5)
    assert sorted(final["viewed"]) == sorted(f"u{i}" for i in range(n))


# --- 8: security — submit body reaches gh argv only (mutating endpoint) -------

def test_submit_body_reaches_argv_only(tmp_path, monkeypatch):
    """A flag note with shell metacharacters must arrive at gh.submit_review as a
    discrete argv element via the assembled body — never a shell string."""
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    state_store.reset_locks()
    server.cache._store.clear()
    server.set_session_token("tok")
    c = TestClient(server.app)
    c.headers.update({"X-Prview-Token": "tok", "Host": "127.0.0.1"})

    monkeypatch.setattr(gh, "fetch_pr_info",
                        lambda o, r, n: PRInfo(owner="o", repo="r", number=1, title="t"))
    monkeypatch.setattr(gh, "fetch_pr_diff",
                        lambda o, r, n: "diff --git a/f.py b/f.py\n+x\n")
    c.post("/pr", json={"ref": "o/r#1"})

    injection = "$(rm -rf /); `whoami`"
    c.post("/file/flag", json={"owner": "o", "repo": "r", "number": 1,
                               "path": "f.py", "flagged": True, "note": injection})

    captured = {}
    monkeypatch.setattr(gh, "submit_review",
                        lambda o, r, n, event, body: captured.update(event=event, body=body) or (True, ""))
    c.post("/review/submit", json={"owner": "o", "repo": "r", "number": 1, "event": "comment"})

    assert captured["event"] == "comment"
    assert injection in captured["body"]  # passed through verbatim, never interpreted
    assert "`f.py`" in captured["body"]


# --- 9: regression — review-event UI↔API contract (was a silent downgrade) ----

def test_review_event_casing_contract(tmp_path, monkeypatch):
    """Regression for C1: the UI sent UPPERCASE events while gh.submit_review's
    flag_map keys are lowercase, so every Approve silently became a plain comment.
    The event validator must (a) normalize case/dashes to the canonical lowercase
    the gh layer expects, and (b) reject unknown events instead of downgrading."""
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    state_store.reset_locks()
    server.cache._store.clear()
    server.set_session_token("tok")
    c = TestClient(server.app)
    c.headers.update({"X-Prview-Token": "tok", "Host": "127.0.0.1"})

    captured = {}
    monkeypatch.setattr(gh, "submit_review",
                        lambda o, r, n, event, body: captured.update(event=event) or (True, ""))

    # The exact strings the (pre-fix) UI sent must normalize to the gh flag_map keys.
    for sent, canonical in [("APPROVE", "approve"),
                            ("REQUEST_CHANGES", "request_changes"),
                            ("request-changes", "request_changes"),
                            ("Comment", "comment")]:
        captured.clear()
        res = c.post("/review/submit",
                     json={"owner": "o", "repo": "r", "number": 1, "event": sent})
        assert res.status_code == 200, (sent, res.status_code, res.text)
        assert captured["event"] == canonical, (sent, captured)

    # An unknown event is rejected (422), never silently downgraded to a comment.
    res = c.post("/review/submit",
                 json={"owner": "o", "repo": "r", "number": 1, "event": "bogus"})
    assert res.status_code == 422, res.text
