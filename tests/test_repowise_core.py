"""Pure-core tests for the repowise feature (Group 1, step 1.1).

NO mocks, NO subprocess, NO real server. Every test here exercises a pure
decision function or a tolerant file read/write — the functional core. The
side-effecting edges (subprocess preflight, checkout, serve, frameability
probe) are deliberately NOT exercised here; repowise is not installed in CI,
so these must pass without it.
"""
from pathlib import Path

import prview.core as core
import prview.repowise as rw
from prview.core import (
    get_repo_path,
    load_repo_map,
    save_repo_map,
    set_repo_path,
)


# ---------------------------------------------------------------------------
# Repo-path map: read/write round-trip, tolerant of missing/corrupt
# ---------------------------------------------------------------------------

def test_repo_map_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_REPO_MAP_PATH", tmp_path / "repos.json")
    assert load_repo_map() == {}


def test_repo_map_corrupt_returns_empty(tmp_path, monkeypatch):
    p = tmp_path / "repos.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(core, "_REPO_MAP_PATH", p)
    assert load_repo_map() == {}


def test_repo_map_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_REPO_MAP_PATH", tmp_path / "repos.json")
    save_repo_map({"octocat/hello": "/Users/me/code/hello"})
    assert load_repo_map() == {"octocat/hello": "/Users/me/code/hello"}

    set_repo_path("octocat", "widgets", "/Users/me/code/widgets")
    assert get_repo_path("octocat", "hello") == "/Users/me/code/hello"
    assert get_repo_path("octocat", "widgets") == "/Users/me/code/widgets"
    assert get_repo_path("nobody", "nope") is None


# ---------------------------------------------------------------------------
# Repo-path validation decision (PURE — inputs already parsed)
# ---------------------------------------------------------------------------

def test_validate_repo_path_ok():
    decision = rw.validate_repo_path_decision(
        path_exists=True,
        git_dir_present=True,
        remote_url="https://github.com/octocat/hello.git",
        owner="octocat",
        repo="hello",
    )
    assert decision == {"ok": True}


def test_validate_repo_path_missing_dir():
    decision = rw.validate_repo_path_decision(
        path_exists=False,
        git_dir_present=False,
        remote_url="",
        owner="octocat",
        repo="hello",
    )
    assert decision["ok"] is False
    assert "error" in decision and "hint" in decision


def test_validate_repo_path_not_a_git_repo():
    decision = rw.validate_repo_path_decision(
        path_exists=True,
        git_dir_present=False,
        remote_url="",
        owner="octocat",
        repo="hello",
    )
    assert decision["ok"] is False
    assert ".git" in decision["hint"]


def test_validate_repo_path_remote_mismatch_ssh_and_https():
    # SSH remote that matches should pass.
    ok = rw.validate_repo_path_decision(
        path_exists=True,
        git_dir_present=True,
        remote_url="git@github.com:octocat/hello.git",
        owner="octocat",
        repo="hello",
    )
    assert ok == {"ok": True}

    # A clone whose origin is a different repo must fail with a mismatch hint.
    bad = rw.validate_repo_path_decision(
        path_exists=True,
        git_dir_present=True,
        remote_url="git@github.com:other/repo.git",
        owner="octocat",
        repo="hello",
    )
    assert bad["ok"] is False
    assert "other/repo" in bad["hint"]


# ---------------------------------------------------------------------------
# Frameability header parse (PURE)
# ---------------------------------------------------------------------------

def test_frameable_when_no_blocking_headers():
    assert rw.parse_frameability({"content-type": "text/html"}) is True
    assert rw.parse_frameability({}) is True


def test_not_frameable_xfo():
    assert rw.parse_frameability({"X-Frame-Options": "DENY"}) is False
    assert rw.parse_frameability({"x-frame-options": "SAMEORIGIN"}) is False


def test_not_frameable_csp_frame_ancestors():
    assert rw.parse_frameability(
        {"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'"}
    ) is False
    assert rw.parse_frameability(
        {"content-security-policy": "frame-ancestors https://example.com"}
    ) is False


# ---------------------------------------------------------------------------
# Indexed-marker decision (PURE — listing passed in)
# ---------------------------------------------------------------------------

def test_indexed_true_when_wiki_db_present():
    # wiki.db present → indexed → skip init.
    assert rw.is_indexed_decision(repowise_dir_files=["config.yaml", "state.json", "wiki.db"]) is True


def test_not_indexed_bare_repowise_dir():
    # Bare .repowise/ with config but NO wiki.db must NOT skip.
    assert rw.is_indexed_decision(repowise_dir_files=["config.yaml", "state.json"]) is False
    assert rw.is_indexed_decision(repowise_dir_files=[]) is False
    assert rw.is_indexed_decision(repowise_dir_files=None) is False


# ---------------------------------------------------------------------------
# Node version parse + ok decision (PURE)
# ---------------------------------------------------------------------------

def test_node_version_parse_and_ok():
    assert rw.parse_node_major("v20.11.1") == 20
    assert rw.parse_node_major("v18.19.0") == 18
    assert rw.parse_node_major("garbage") is None
    assert rw.node_ok_decision("v20.11.1") is True
    assert rw.node_ok_decision("v22.0.0") is True
    assert rw.node_ok_decision("v18.19.0") is False
    assert rw.node_ok_decision(None) is False


# ---------------------------------------------------------------------------
# Prepare-step state transitions (PURE)
# ---------------------------------------------------------------------------

def test_prepare_steps_initial_ordered_pending():
    steps = rw.PrepareSteps()
    snap = steps.snapshot()
    assert [s["key"] for s in snap["steps"]] == [
        "resolve_path", "checkout", "index", "serve", "open",
    ]
    assert all(s["status"] == "pending" for s in snap["steps"])
    assert snap["error_step"] is None


def test_prepare_steps_running_then_done_and_skip():
    steps = rw.PrepareSteps()
    steps.start("resolve_path", "~/code/hello")
    assert steps.status_of("resolve_path") == "running"
    steps.done("resolve_path", "~/code/hello")
    assert steps.status_of("resolve_path") == "done"

    steps.skip("index", "already indexed")
    assert steps.status_of("index") == "skipped"


def test_prepare_steps_failure_sets_error_step():
    steps = rw.PrepareSteps()
    steps.start("checkout")
    steps.fail("checkout", error="working tree dirty",
               hint="commit/stash in ~/code/hello first")
    snap = steps.snapshot()
    assert steps.status_of("checkout") == "failed"
    assert snap["error_step"] == "checkout"
    assert snap["error"] == "working tree dirty"
    assert "commit/stash" in snap["error_hint"]


# ---------------------------------------------------------------------------
# Group 4 gap-fillers (≤10). The subprocess/serve boundary is mocked — no real
# `repowise serve`, no real `repowise init`, no real ports.
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal Popen stand-in: alive until terminate(), records the call."""

    def __init__(self, pid=4242):
        self.pid = pid
        self._alive = True
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


def _reset_serves(monkeypatch):
    monkeypatch.setattr(rw, "_serves", {})


# --- (a) serve registry REUSE across PRs of the same repo --------------------

def test_ensure_serve_reused_across_prs_no_second_popen(monkeypatch):
    """A second prepare for the same repo must reuse the live serve child — one
    Popen total, one registry entry, same ports — regardless of PR number."""
    _reset_serves(monkeypatch)
    ports = iter([7337, 47821, 9999, 9998])
    monkeypatch.setattr("prview.launcher.pick_free_port", lambda: next(ports))

    spawned = []

    def fake_popen(argv, **kw):
        proc = _FakeProc(pid=1000 + len(spawned))
        spawned.append(argv)
        return proc

    monkeypatch.setattr(rw.subprocess, "Popen", fake_popen)

    first = rw.ensure_serve("octo", "hello", "/code/hello")   # PR #1 prepares
    second = rw.ensure_serve("octo", "hello", "/code/hello")  # PR #2 prepares

    assert len(spawned) == 1                 # only ONE serve child ever spawned
    assert second is first                   # same registry entry reused
    assert second.api_port == 7337 and second.ui_port == 47821
    assert rw.get_serve("octo", "hello") is first


def test_ensure_serve_argv_matches_live_cli_and_runs_in_worktree(monkeypatch):
    """`repowise serve` (0.23) takes no PATH and no --yes/--no-workspace; it
    resolves the repo from cwd. Pin the argv + cwd so the live-CLI mismatch
    that exited serve before the dashboard came up cannot regress."""
    _reset_serves(monkeypatch)
    ports = iter([7337, 47821])
    monkeypatch.setattr("prview.launcher.pick_free_port", lambda: next(ports))

    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw.get("cwd")
        return _FakeProc()

    monkeypatch.setattr(rw.subprocess, "Popen", fake_popen)
    rw.ensure_serve("octo", "hello", "/wt/hello-pr-9")

    assert captured["argv"] == [
        "repowise", "serve",
        "--host", "127.0.0.1", "--port", "7337", "--ui-port", "47821",
    ]
    assert "--yes" not in captured["argv"]
    assert "--no-workspace" not in captured["argv"]
    assert "/wt/hello-pr-9" not in captured["argv"]   # no path positional
    assert captured["cwd"] == "/wt/hello-pr-9"         # serve reads .repowise from here


def test_ensure_serve_respawns_when_child_died(monkeypatch):
    """If the tracked child has exited (poll() != None), a fresh serve starts —
    reuse is gated on the process still being alive, not merely registered."""
    _reset_serves(monkeypatch)
    ports = iter([1, 2, 3, 4])
    monkeypatch.setattr("prview.launcher.pick_free_port", lambda: next(ports))

    procs = []

    def fake_popen(argv, **kw):
        p = _FakeProc(pid=2000 + len(procs))
        procs.append(p)
        return p

    monkeypatch.setattr(rw.subprocess, "Popen", fake_popen)

    first = rw.ensure_serve("octo", "hello", "/code/hello")
    procs[0]._alive = False  # child died
    second = rw.ensure_serve("octo", "hello", "/code/hello")

    assert len(procs) == 2 and second is not first


# --- (b) stop_all() teardown: terminate tracked pids + clear registry --------

def test_stop_all_terminates_all_and_clears_registry(monkeypatch):
    _reset_serves(monkeypatch)
    ports = iter([10, 11, 20, 21])
    monkeypatch.setattr("prview.launcher.pick_free_port", lambda: next(ports))
    monkeypatch.setattr(rw.subprocess, "Popen", lambda argv, **kw: _FakeProc())

    e1 = rw.ensure_serve("octo", "hello", "/code/hello")
    e2 = rw.ensure_serve("octo", "widgets", "/code/widgets")

    rw.stop_all()

    assert e1._proc.terminated is True
    assert e2._proc.terminated is True
    assert rw.get_serve("octo", "hello") is None
    assert rw.get_serve("octo", "widgets") is None
    assert rw._serves == {}


# --- (e) index: runs init on bare .repowise/ vs skips when wiki.db present ----

def test_ensure_indexed_runs_init_on_bare_repowise_dir(tmp_path, monkeypatch):
    """Bare .repowise/ (config only, no wiki.db) → ensure_indexed runs init and
    reports NOT skipped, with the fixed --index-only argv."""
    repo = tmp_path / "clone"
    (repo / ".repowise").mkdir(parents=True)
    (repo / ".repowise" / "config.yaml").write_text("x")

    calls = []

    class _Ok:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(argv, cwd=None):
        calls.append(argv)
        return _Ok()

    monkeypatch.setattr(rw, "_run", fake_run)
    skipped = rw.ensure_indexed(str(repo))

    assert skipped is False
    assert calls == [["repowise", "init", str(repo), "--yes", "--index-only"]]


def test_prepare_pr_worktree_fetches_then_adds_isolated_worktree(tmp_path, monkeypatch):
    """Worktree path: fetch the PR head, remove any stale worktree, add a fresh
    detached one OUTSIDE the clone. No `git status` / dirty-guard is run."""
    monkeypatch.setattr(rw, "_WORKTREE_DIR", tmp_path / "wt")
    rw._worktrees.clear()
    calls = []

    class _Ok:
        returncode, stdout, stderr = 0, "abc1234\n", ""

    monkeypatch.setattr(rw, "_run", lambda argv, cwd=None: (calls.append(argv), _Ok())[1])
    wt, rev = rw.prepare_pr_worktree("/code/hello", 42)

    expected_wt = str(tmp_path / "wt" / "hello-pr-42")
    assert wt == expected_wt
    assert rev == "abc1234"
    # no dirty-guard: nothing ran `git status`
    assert not any("status" in c for c in calls)
    assert calls[0] == ["git", "-C", "/code/hello", "fetch", "origin", "pull/42/head:prview/pr-42"]
    assert ["git", "-C", "/code/hello", "worktree", "remove", "--force", expected_wt] in calls
    assert ["git", "-C", "/code/hello", "worktree", "add", "--force", "--detach", expected_wt, "prview/pr-42"] in calls
    # registered for teardown
    assert rw._worktrees == {"/code/hello#42": ("/code/hello", expected_wt)}


def test_prepare_pr_worktree_raises_on_fetch_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(rw, "_WORKTREE_DIR", tmp_path / "wt")

    class _Fail:
        returncode, stdout, stderr = 1, "", "no such ref"

    monkeypatch.setattr(rw, "_run", lambda argv, cwd=None: _Fail())
    try:
        rw.prepare_pr_worktree("/code/hello", 42)
        assert False, "expected RepowiseError"
    except rw.RepowiseError as exc:
        assert "git fetch failed" in exc.message


def test_remove_all_worktrees_prunes_and_clears_registry(monkeypatch):
    rw._worktrees.clear()
    rw._worktrees["/code/hello#7"] = ("/code/hello", "/wt/hello-pr-7")
    calls = []

    class _Ok:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(rw, "_run", lambda argv, cwd=None: (calls.append(argv), _Ok())[1])
    rw.remove_all_worktrees()
    assert calls == [["git", "-C", "/code/hello", "worktree", "remove", "--force", "/wt/hello-pr-7"]]
    assert rw._worktrees == {}


def test_ensure_indexed_skips_when_wiki_db_present(tmp_path, monkeypatch):
    """wiki.db present → skipped True, NO init subprocess invoked at all."""
    repo = tmp_path / "clone"
    (repo / ".repowise").mkdir(parents=True)
    (repo / ".repowise" / "wiki.db").write_text("db")

    def boom(argv, cwd=None):
        raise AssertionError(f"init must not run when indexed: {argv}")

    monkeypatch.setattr(rw, "_run", boom)
    assert rw.ensure_indexed(str(repo)) is True
