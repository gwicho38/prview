"""Repowise API endpoint tests. The repowise/core/gh boundary is mocked —
NEVER a real subprocess or `repowise serve`. Same `client` fixture style as
test_api.py: valid token + loopback Host header satisfies SecurityMiddleware."""
import pytest
from fastapi.testclient import TestClient

import prview.core as core
import prview.repowise as repowise
import prview.server as server
import prview.state_store as state_store


TOKEN = "test-token-123"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "_REPO_MAP_PATH", tmp_path / "repos.json")
    state_store.reset_locks()
    server.cache._store.clear()
    server.set_session_token(TOKEN)
    c = TestClient(server.app)
    c.headers.update({"X-Prview-Token": TOKEN, "Host": "127.0.0.1"})
    return c


# --- GET /repowise/status ----------------------------------------------------

def test_status_cli_missing_sets_hint_not_500(client, monkeypatch):
    monkeypatch.setattr(repowise, "cli_present", lambda: (False, "install it: uv tool install repowise"))
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: None)
    monkeypatch.setattr(repowise, "get_serve", lambda o, r: None)
    resp = client.get("/repowise/status", params={"owner": "octo", "repo": "hello", "number": 7})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cli_present"] is False
    assert body["cli_hint"] == "install it: uv tool install repowise"
    assert body["repo_path_known"] is False
    assert body["serve_running"] is False
    assert body["frameable"] is None


def test_status_reports_node_not_ok(client, monkeypatch):
    # FR-3: the Node>=20 preflight must reach the client (was dead code: /status
    # never called node_present, so node_ok/node_hint were absent).
    monkeypatch.setattr(repowise, "cli_present", lambda: (True, None))
    monkeypatch.setattr(repowise, "node_present", lambda: (False, "install Node.js 20+"))
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: None)
    monkeypatch.setattr(repowise, "get_serve", lambda o, r: None)
    resp = client.get("/repowise/status", params={"owner": "octo", "repo": "hello", "number": 7})
    assert resp.status_code == 200
    body = resp.json()
    assert body["node_ok"] is False
    assert body["node_hint"] == "install Node.js 20+"


def test_status_repo_known_indexed_no_serve(client, monkeypatch):
    monkeypatch.setattr(repowise, "cli_present", lambda: (True, None))
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: "/code/hello")
    monkeypatch.setattr(repowise, "is_repo_indexed", lambda p: True)
    monkeypatch.setattr(repowise, "get_serve", lambda o, r: None)
    resp = client.get("/repowise/status", params={"owner": "octo", "repo": "hello", "number": 7})
    body = resp.json()
    assert body["cli_present"] is True
    assert body["repo_path_known"] is True
    assert body["repo_path"] == "/code/hello"
    assert body["indexed"] is True
    assert body["serve_running"] is False
    assert body["serve_url"] is None
    assert body["frameable"] is None


def test_status_serve_running_reports_url_port_and_frameable(client, monkeypatch):
    entry = repowise.ServeEntry(
        pid=111, api_port=7337, ui_port=47821,
        url="http://127.0.0.1:47821/", started_at=0.0,
        repo_path="/code/hello", frameable=True,
    )
    monkeypatch.setattr(repowise, "cli_present", lambda: (True, None))
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: "/code/hello")
    monkeypatch.setattr(repowise, "is_repo_indexed", lambda p: True)
    monkeypatch.setattr(repowise, "get_serve", lambda o, r: entry)
    resp = client.get("/repowise/status", params={"owner": "octo", "repo": "hello", "number": 7})
    body = resp.json()
    assert body["serve_running"] is True
    assert body["serve_url"] == "http://127.0.0.1:47821/"
    assert body["serve_port"] == 47821
    assert body["frameable"] is True


# --- POST /repowise/repo-path ------------------------------------------------

def test_repo_path_valid_persists(client, monkeypatch):
    monkeypatch.setattr(
        repowise, "validate_and_persist_path",
        lambda o, r, p: {"ok": True, "path": "/Users/me/code/hello"},
    )
    resp = client.post("/repowise/repo-path", json={"owner": "octo", "repo": "hello", "path": "~/code/hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["path"] == "/Users/me/code/hello"


def test_repo_path_not_a_git_repo_returns_400(client, monkeypatch):
    monkeypatch.setattr(
        repowise, "validate_and_persist_path",
        lambda o, r, p: {"ok": False, "error": "Not a git repository",
                         "hint": "expected a directory containing .git"},
    )
    resp = client.post("/repowise/repo-path", json={"owner": "octo", "repo": "hello", "path": "~/code/typo"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "Not a git repository"
    assert body["hint"] == "expected a directory containing .git"


def test_repo_path_remote_mismatch_returns_400(client, monkeypatch):
    monkeypatch.setattr(
        repowise, "validate_and_persist_path",
        lambda o, r, p: {"ok": False, "error": "Remote does not match octo/hello",
                         "hint": "this clone's origin is other/repo"},
    )
    resp = client.post("/repowise/repo-path", json={"owner": "octo", "repo": "hello", "path": "~/code/other"})
    assert resp.status_code == 400
    assert "Remote does not match" in resp.json()["error"]


# --- POST /repowise/prepare --------------------------------------------------

def test_prepare_happy_returns_job_id(client, monkeypatch):
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: "/code/hello")
    monkeypatch.setattr(repowise, "start_prepare", lambda o, r, n: "job-xyz")
    resp = client.post("/repowise/prepare", json={"owner": "octo", "repo": "hello", "number": 123})
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "job-xyz"


def test_prepare_path_unknown_returns_409(client, monkeypatch):
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: None)
    resp = client.post("/repowise/prepare", json={"owner": "octo", "repo": "hello", "number": 123})
    assert resp.status_code == 409
    body = resp.json()
    assert "error" in body
    assert "hint" in body


def test_prepare_cli_missing_surfaces_hint_not_500(client, monkeypatch):
    # repowise boundary raises the structured error → mapped to 400 hint, not 500
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: "/code/hello")

    def boom(o, r, n):
        raise repowise.RepowiseError("`repowise` not found", hint="install it: uv tool install repowise")

    monkeypatch.setattr(repowise, "start_prepare", boom)
    resp = client.post("/repowise/prepare", json={"owner": "octo", "repo": "hello", "number": 123})
    assert 400 <= resp.status_code < 500
    body = resp.json()
    assert body["error"] == "`repowise` not found"
    assert body["hint"] == "install it: uv tool install repowise"


# --- GET /repowise/prepare/{job_id} ------------------------------------------

def test_prepare_snapshot_running_shape(client, monkeypatch):
    snap = {
        "status": "running",
        "steps": [
            {"key": "resolve_path", "status": "done", "detail": "/code/hello"},
            {"key": "checkout", "status": "running", "detail": ""},
            {"key": "index", "status": "pending", "detail": ""},
            {"key": "serve", "status": "pending", "detail": ""},
            {"key": "open", "status": "pending", "detail": ""},
        ],
        "elapsed": 6.2,
        "dashboard_url": None, "serve_port": None, "frameable": None,
        "error": None, "error_step": None, "error_hint": None, "stderr_tail": None,
    }
    monkeypatch.setattr(repowise, "get_prepare", lambda jid: snap)
    resp = client.get("/repowise/prepare/job-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert [s["key"] for s in body["steps"]] == ["resolve_path", "checkout", "index", "serve", "open"]
    assert body["elapsed"] == 6.2


def test_prepare_snapshot_done_populates_embed_fields(client, monkeypatch):
    snap = {
        "status": "done",
        "steps": [{"key": k, "status": "done", "detail": ""} for k in
                  ("resolve_path", "checkout", "index", "serve", "open")],
        "elapsed": 12.0,
        "dashboard_url": "http://127.0.0.1:47821/", "serve_port": 47821, "frameable": True,
        "error": None, "error_step": None, "error_hint": None, "stderr_tail": None,
    }
    monkeypatch.setattr(repowise, "get_prepare", lambda jid: snap)
    resp = client.get("/repowise/prepare/job-1")
    body = resp.json()
    assert body["status"] == "done"
    assert body["dashboard_url"] == "http://127.0.0.1:47821/"
    assert body["serve_port"] == 47821
    assert body["frameable"] is True


def test_prepare_snapshot_checkout_error(client, monkeypatch):
    snap = {
        "status": "error",
        "steps": [
            {"key": "resolve_path", "status": "done", "detail": "/code/hello"},
            {"key": "checkout", "status": "failed", "detail": ""},
            {"key": "index", "status": "pending", "detail": ""},
            {"key": "serve", "status": "pending", "detail": ""},
            {"key": "open", "status": "pending", "detail": ""},
        ],
        "elapsed": 1.0,
        "dashboard_url": None, "serve_port": None, "frameable": None,
        "error": "git fetch failed: no such ref",
        "error_step": "checkout",
        "error_hint": "confirm origin points at the PR's repo and you have access",
        "stderr_tail": "fatal: couldn't find remote ref",
    }
    monkeypatch.setattr(repowise, "get_prepare", lambda jid: snap)
    resp = client.get("/repowise/prepare/job-1")
    body = resp.json()
    assert body["status"] == "error"
    assert body["error_step"] == "checkout"
    assert body["error_hint"] == "confirm origin points at the PR's repo and you have access"
    assert body["stderr_tail"] == "fatal: couldn't find remote ref"


def test_prepare_snapshot_unknown_id_404(client, monkeypatch):
    monkeypatch.setattr(repowise, "get_prepare", lambda jid: None)
    resp = client.get("/repowise/prepare/nope")
    assert resp.status_code == 404
    assert "error" in resp.json()


# --- POST /repowise/prepare/{job_id}/cancel ----------------------------------

def test_prepare_cancel_ok(client, monkeypatch):
    monkeypatch.setattr(repowise, "cancel_prepare", lambda jid: True)
    resp = client.post("/repowise/prepare/job-1/cancel")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# --- POST /repowise/stop -----------------------------------------------------

def test_stop_serve_ok(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(repowise, "stop_serve",
                        lambda o, r: captured.update(owner=o, repo=r) or True)
    resp = client.post("/repowise/stop", json={"owner": "octo", "repo": "hello"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert captured == {"owner": "octo", "repo": "hello"}


# --- frontend routing contract (renderRepowise branch decisions) ------------
# The repowise screen (app.js renderRepowise) reads exactly these fields to pick
# a branch. These tests pin the contract the JS router depends on so a backend
# shape change that would silently break the UI fails here instead.

def test_status_drives_modal_branch_when_path_unknown(client, monkeypatch):
    """cli present + repo_path_known false → UI opens the repo-path modal."""
    monkeypatch.setattr(repowise, "cli_present", lambda: (True, None))
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: None)
    monkeypatch.setattr(repowise, "get_serve", lambda o, r: None)
    body = client.get("/repowise/status",
                      params={"owner": "octo", "repo": "hello", "number": 7}).json()
    # JS: if (!status.cli_present) -> error; else if (!status.repo_path_known) -> modal
    assert body["cli_present"] is True
    assert body["repo_path_known"] is False


def test_status_drives_prepare_branch_when_path_known(client, monkeypatch):
    """cli present + repo_path_known true → UI proceeds to prepare+poll."""
    monkeypatch.setattr(repowise, "cli_present", lambda: (True, None))
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: "/code/hello")
    monkeypatch.setattr(repowise, "is_repo_indexed", lambda p: False)
    monkeypatch.setattr(repowise, "get_serve", lambda o, r: None)
    body = client.get("/repowise/status",
                      params={"owner": "octo", "repo": "hello", "number": 7}).json()
    assert body["cli_present"] is True
    assert body["repo_path_known"] is True


def test_prepare_done_frameable_true_drives_embed_branch(client, monkeypatch):
    """done + frameable true → UI renders the iframe embed (uses dashboard_url)."""
    snap = {
        "status": "done",
        "steps": [{"key": k, "status": "done", "detail": ""} for k in
                  ("resolve_path", "checkout", "index", "serve", "open")],
        "elapsed": 9.0,
        "dashboard_url": "http://127.0.0.1:47821/", "serve_port": 47821, "frameable": True,
        "error": None, "error_step": None, "error_hint": None, "stderr_tail": None,
    }
    monkeypatch.setattr(repowise, "get_prepare", lambda jid: snap)
    body = client.get("/repowise/prepare/job-1").json()
    # JS: if (snap.frameable) -> embed(snap.dashboard_url) else -> fallback
    assert body["frameable"] is True
    assert body["dashboard_url"].startswith("http://127.0.0.1:")


def test_prepare_done_frameable_false_drives_fallback_branch(client, monkeypatch):
    """done + frameable false → UI renders the link fallback card (same URL)."""
    snap = {
        "status": "done",
        "steps": [{"key": k, "status": "done", "detail": ""} for k in
                  ("resolve_path", "checkout", "index", "serve", "open")],
        "elapsed": 9.0,
        "dashboard_url": "http://127.0.0.1:47821/", "serve_port": 47821, "frameable": False,
        "error": None, "error_step": None, "error_hint": None, "stderr_tail": None,
    }
    monkeypatch.setattr(repowise, "get_prepare", lambda jid: snap)
    body = client.get("/repowise/prepare/job-1").json()
    assert body["frameable"] is False
    assert body["dashboard_url"] == "http://127.0.0.1:47821/"


# --- prepare orchestrator unit (no server) -----------------------------------

def test_start_prepare_runs_steps_to_done(monkeypatch):
    """Drive start_prepare with the subprocess edges stubbed; assert it reaches
    done with embed fields populated and steps all terminal."""
    entry = repowise.ServeEntry(
        pid=222, api_port=7337, ui_port=5000,
        url="http://127.0.0.1:5000/", started_at=0.0,
        repo_path="/code/hello", frameable=True,
    )
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: "/code/hello")
    served = {}
    monkeypatch.setattr(repowise, "prepare_pr_worktree",
                        lambda p, n: ("/wt/hello-pr-123", "a1b2c3d"))

    def fake_index(p):
        served["index"] = p
        return True  # skipped

    def fake_serve(o, r, p):
        served["serve"] = p
        return entry

    monkeypatch.setattr(repowise, "ensure_indexed", fake_index)
    monkeypatch.setattr(repowise, "ensure_serve", fake_serve)

    def fake_probe(e, timeout_s=30.0):
        e.frameable = True
        return True

    monkeypatch.setattr(repowise, "probe_frameability", fake_probe)

    job_id = repowise.start_prepare("octo", "hello", 123)
    snap = _wait_terminal(repowise, job_id)
    assert snap["status"] == "done"
    assert snap["dashboard_url"] == "http://127.0.0.1:5000/"
    assert snap["serve_port"] == 5000
    assert snap["frameable"] is True
    statuses = {s["key"]: s["status"] for s in snap["steps"]}
    assert statuses["index"] == "skipped"
    assert statuses["serve"] == "done"
    assert statuses["open"] == "done"
    # index + serve run against the worktree, not the user's clone
    assert served["index"] == "/wt/hello-pr-123"
    assert served["serve"] == "/wt/hello-pr-123"


def test_start_prepare_worktree_fetch_fails(monkeypatch):
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: "/code/hello")

    def boom(p, n):
        raise repowise.RepowiseError("git fetch failed: no such ref",
                                     hint="confirm origin points at the PR's repo and you have access")

    monkeypatch.setattr(repowise, "prepare_pr_worktree", boom)
    job_id = repowise.start_prepare("octo", "hello", 123)
    snap = _wait_terminal(repowise, job_id)
    assert snap["status"] == "error"
    assert snap["error_step"] == "checkout"
    assert "git fetch failed" in snap["error"]


def _wait_terminal(repowise, job_id, tries=200):
    import time
    for _ in range(tries):
        snap = repowise.get_prepare(job_id)
        if snap and snap["status"] in ("done", "error", "cancelled"):
            return snap
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never reached a terminal state: {repowise.get_prepare(job_id)}")


# --- Group 4 gap-fillers: cancel mid-flight + 409→persist→re-prepare ---------

def test_cancel_mid_flight_stops_before_serve(monkeypatch):
    """The cooperative cancel flag is honored BETWEEN steps: cancelling while
    checkout is in flight lands the job 'cancelled' and never starts serve."""
    import threading

    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: "/code/hello")

    at_checkout = threading.Event()
    release = threading.Event()

    def gated_checkout(p, n):
        at_checkout.set()       # orchestrator is now inside checkout
        release.wait(2.0)       # park until the test has flipped cancel
        return ("/wt/hello-pr-7", "deadbee")

    serve_called = []
    monkeypatch.setattr(repowise, "prepare_pr_worktree", gated_checkout)
    monkeypatch.setattr(repowise, "ensure_indexed", lambda p: True)
    monkeypatch.setattr(repowise, "ensure_serve",
                        lambda o, r, p: serve_called.append(1))

    job_id = repowise.start_prepare("octo", "hello", 7)
    assert at_checkout.wait(2.0)
    assert repowise.cancel_prepare(job_id) is True  # flip flag mid-flight
    release.set()

    snap = _wait_terminal(repowise, job_id)
    assert snap["status"] == "cancelled"
    assert serve_called == []                       # never reached serve
    statuses = {s["key"]: s["status"] for s in snap["steps"]}
    assert statuses["serve"] == "pending" and statuses["open"] == "pending"


def test_cancel_unknown_or_terminal_job_returns_false(monkeypatch):
    """cancel is best-effort: unknown id → False; an already-terminal job → False
    (the flag only takes effect on a still-running job)."""
    assert repowise.cancel_prepare("no-such-job") is False
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: None)
    job_id = repowise.start_prepare("octo", "hello", 7)
    _wait_terminal(repowise, job_id)                # errors out (no path)
    assert repowise.cancel_prepare(job_id) is False


def test_path_unknown_409_then_persist_then_prepare_proceeds(client, monkeypatch):
    """End-to-end recovery: prepare 409s while the path is unknown; POST
    /repowise/repo-path persists it; the SAME prepare call now proceeds (200)."""
    # 1) Path unknown → 409 with actionable hint.
    monkeypatch.setattr(repowise, "resolve_repo_path", lambda o, r: None)
    r1 = client.post("/repowise/prepare",
                     json={"owner": "octo", "repo": "hello", "number": 7})
    assert r1.status_code == 409
    assert "error" in r1.json() and "hint" in r1.json()

    # 2) Persist the repo path (validation edge stubbed; core actually writes).
    def persist(o, r, p):
        core.set_repo_path(o, r, "/code/hello")
        return {"ok": True, "path": "/code/hello"}

    monkeypatch.setattr(repowise, "validate_and_persist_path", persist)
    r2 = client.post("/repowise/repo-path",
                     json={"owner": "octo", "repo": "hello", "path": "~/code/hello"})
    assert r2.status_code == 200 and r2.json()["ok"] is True

    # 3) resolve_repo_path now reads the persisted map (un-stub it) → prepare 200.
    monkeypatch.setattr(repowise, "resolve_repo_path", core.get_repo_path)
    monkeypatch.setattr(repowise, "start_prepare", lambda o, r, n: "job-after-persist")
    r3 = client.post("/repowise/prepare",
                     json={"owner": "octo", "repo": "hello", "number": 7})
    assert r3.status_code == 200
    assert r3.json()["job_id"] == "job-after-persist"


def test_blast_radius_route_returns_model(client, monkeypatch):
    """The /repowise/blast-radius route shapes repowise's response into the
    prview model (direct risks, transitive, co-change, reviewers, test gaps)."""
    captured = {}

    def fake_blast(owner, repo, changed_files, max_depth=3):
        captured.update(owner=owner, repo=repo, files=changed_files, depth=max_depth)
        return {
            "direct_risks": [{"path": "a.py", "risk_score": 1.2, "temporal_hotspot": 0.0, "centrality": 0.5}],
            "transitive_affected": [{"path": "c.py", "depth": 1}],
            "cochange_warnings": [{"changed": "a.py", "missing_partner": "d.py", "score": 0.8}],
            "recommended_reviewers": [{"email": "x@y.z", "files": 3, "ownership_pct": 42.0}],
            "test_gaps": ["a.py"],
            "overall_risk_score": 3.4,
        }

    monkeypatch.setattr(repowise, "blast_radius", fake_blast)
    resp = client.post("/repowise/blast-radius", json={
        "owner": "octo", "repo": "hello", "number": 7, "changed_files": ["a.py", "b.py"],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert captured["files"] == ["a.py", "b.py"] and captured["depth"] == 3
    assert body["overall_risk_score"] == 3.4
    assert body["direct_risks"][0]["path"] == "a.py"
    assert body["transitive_affected"][0]["depth"] == 1
    assert body["cochange_warnings"][0]["missing_partner"] == "d.py"
    assert body["recommended_reviewers"][0]["email"] == "x@y.z"
    assert body["test_gaps"] == ["a.py"]


def test_blast_radius_route_surfaces_repowise_error(client, monkeypatch):
    def boom(owner, repo, changed_files, max_depth=3):
        raise repowise.RepowiseError("repowise serve is not running", hint="prepare first")

    monkeypatch.setattr(repowise, "blast_radius", boom)
    resp = client.post("/repowise/blast-radius", json={
        "owner": "octo", "repo": "hello", "number": 7, "changed_files": ["a.py"],
    })
    assert resp.status_code == 400
    assert "not running" in resp.json()["error"]


def test_coverage_route_returns_model(client, monkeypatch):
    captured = {}

    def fake_ingest(owner, repo, path):
        captured.update(owner=owner, repo=repo, path=path)
        return {"ok": True, "files": 42, "path": "/x/coverage.lcov"}

    monkeypatch.setattr(repowise, "ingest_coverage", fake_ingest)
    resp = client.post("/repowise/coverage",
                       json={"owner": "octo", "repo": "hello", "number": 7, "path": None})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["files"] == 42
    assert body["path"].endswith("coverage.lcov")
    assert captured["path"] is None  # blank → server-side auto-detect


def test_ollama_models_route(client, monkeypatch):
    monkeypatch.setattr(repowise, "list_ollama_models", lambda: ["qwen2.5:3b", "gemma4:latest"])
    resp = client.get("/repowise/ollama-models")
    assert resp.status_code == 200
    assert resp.json()["models"] == ["qwen2.5:3b", "gemma4:latest"]


def test_docs_generate_start_and_status(client, monkeypatch):
    monkeypatch.setattr(repowise, "start_docgen", lambda o, r, m: "docgen-1")
    resp = client.post("/repowise/docs/generate",
                       json={"owner": "octo", "repo": "hello", "number": 7, "model": "gemma4:latest"})
    assert resp.status_code == 200 and resp.json()["job_id"] == "docgen-1"

    monkeypatch.setattr(repowise, "get_docgen",
                        lambda jid: {"status": "running", "elapsed": 3.0, "model": "gemma4:latest",
                                     "error": None, "log_tail": None})
    s = client.get("/repowise/docs/generate/docgen-1")
    assert s.status_code == 200 and s.json()["model"] == "gemma4:latest"

    monkeypatch.setattr(repowise, "get_docgen", lambda jid: None)
    assert client.get("/repowise/docs/generate/nope").status_code == 404
