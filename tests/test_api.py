"""API endpoint tests. gh/jobs are mocked at the prview.gh / prview.jobs
boundary — never a real subprocess. The security middleware is satisfied with
a valid token + a localhost Host header via the `client` fixture."""
import pytest
from fastapi.testclient import TestClient

import prview.core as core
import prview.gh as gh
import prview.jobs as jobs
import prview.server as server
import prview.state_store as state_store


TOKEN = "test-token-123"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    state_store.reset_locks()
    server.cache._store.clear()
    server.set_session_token(TOKEN)
    c = TestClient(server.app)
    c.headers.update({"X-Prview-Token": TOKEN, "Host": "127.0.0.1"})
    return c


def _fake_pr():
    return core.PRInfo(
        owner="octo", repo="hello", number=7,
        title="Add feature", author="alice", body="desc",
        base="main", head="feat", state="OPEN",
        additions=10, deletions=2, changed_files=2,
    )


def _fake_diff():
    return (
        "diff --git a/big.py b/big.py\n"
        "--- a/big.py\n+++ b/big.py\n"
        "@@ -1,1 +1,5 @@\n+a\n+b\n+c\n+d\n+e\n"
        "diff --git a/small.py b/small.py\n"
        "--- a/small.py\n+++ b/small.py\n"
        "@@ -1,1 +1,2 @@\n+x\n"
    )


def _load_pr(client, monkeypatch):
    monkeypatch.setattr(gh, "fetch_pr_info", lambda o, r, n: _fake_pr())
    monkeypatch.setattr(gh, "fetch_pr_diff", lambda o, r, n: _fake_diff())
    return client.post("/pr", json={"ref": "octo/hello#7"})


def test_post_pr_happy_path(client, monkeypatch):
    resp = _load_pr(client, monkeypatch)
    assert resp.status_code == 200
    data = resp.json()
    assert data["pr"]["owner"] == "octo"
    assert data["pr"]["number"] == 7
    # files sorted by additions+deletions desc, big.py first
    names = [f["filename"] for f in data["files"]]
    assert names == ["big.py", "small.py"]
    # NO diff_text in the list response
    assert all("diff_text" not in f for f in data["files"])
    assert data["state"]["comments"] == 0


def test_post_pr_gh_unauth_returns_structured_4xx(client, monkeypatch):
    def boom(o, r, n):
        raise gh.GhError("Failed to fetch PR: not logged in", hint="run `gh auth login`")
    monkeypatch.setattr(gh, "fetch_pr_info", boom)
    resp = client.post("/pr", json={"ref": "octo/hello#7"})
    assert 400 <= resp.status_code < 500
    body = resp.json()
    assert "error" in body
    assert body["hint"] == "run `gh auth login`"


def test_get_file_serves_diff_text_from_cache(client, monkeypatch):
    _load_pr(client, monkeypatch)
    resp = client.get("/pr/octo/hello/7/file", params={"path": "big.py"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == "big.py"
    assert "diff --git a/big.py" in data["diff_text"]
    assert data["viewed"] is False


def test_get_file_stale_cache_returns_409(client, monkeypatch):
    # never loaded → cache miss
    resp = client.get("/pr/nope/none/1/file", params={"path": "x.py"})
    assert resp.status_code == 409
    assert "error" in resp.json()


def test_ai_summary_lifecycle(client, monkeypatch):
    _load_pr(client, monkeypatch)
    monkeypatch.setattr(jobs, "start_summary", lambda pr, fd: "job-1")
    resp = client.post("/ai/summary", json={"owner": "octo", "repo": "hello", "number": 7, "path": "big.py"})
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "job-1"

    states = iter([
        {"status": "running", "result": "", "error": None, "elapsed": 1.0},
        {"status": "done", "result": "it adds a feature", "error": None, "elapsed": 2.0},
    ])
    monkeypatch.setattr(jobs, "get_job", lambda jid: next(states))
    r1 = client.get("/job/job-1")
    assert r1.json()["status"] == "running"
    r2 = client.get("/job/job-1")
    assert r2.json()["status"] == "done"
    assert r2.json()["result"] == "it adds a feature"

    monkeypatch.setattr(jobs, "cancel_job", lambda jid: True)
    rc = client.post("/job/job-1/cancel")
    assert rc.status_code == 200
    assert rc.json()["ok"] is True


def test_ai_explain_stale_cache_409(client, monkeypatch):
    # uncached PR → AI endpoint must 409 reload
    resp = client.post("/ai/explain", json={"owner": "ghost", "repo": "x", "number": 9, "path": "a.py"})
    assert resp.status_code == 409
    assert "error" in resp.json()


def test_ai_explain_selection(client, monkeypatch):
    _load_pr(client, monkeypatch)
    captured = {}
    monkeypatch.setattr(
        jobs, "start_explain_selection",
        lambda pr, fd, selection: captured.update(selection=selection) or "job-sel",
    )
    resp = client.post("/ai/explain-selection", json={
        "owner": "octo", "repo": "hello", "number": 7, "path": "big.py",
        "selection": "def handle(self):",
    })
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "job-sel"
    assert captured["selection"] == "def handle(self):"


def test_ai_explain_selection_stale_cache_409(client):
    resp = client.post("/ai/explain-selection", json={
        "owner": "ghost", "repo": "x", "number": 9, "path": "a.py", "selection": "xyz",
    })
    assert resp.status_code == 409


def test_file_viewed_remote_ok(client, monkeypatch):
    _load_pr(client, monkeypatch)
    monkeypatch.setattr(gh, "mark_file_viewed", lambda o, r, n, p: True)
    resp = client.post("/file/viewed", json={"owner": "octo", "repo": "hello", "number": 7, "path": "big.py"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["viewed"] is True
    assert data["remote_ok"] is True
    # state persisted
    st = core.load_review_state("octo", "hello", 7)
    assert "big.py" in st["viewed"]


def test_file_viewed_remote_fail_still_saves(client, monkeypatch):
    _load_pr(client, monkeypatch)
    monkeypatch.setattr(gh, "mark_file_viewed", lambda o, r, n, p: False)
    resp = client.post("/file/viewed", json={"owner": "octo", "repo": "hello", "number": 7, "path": "small.py"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["viewed"] is True
    assert data["remote_ok"] is False
    st = core.load_review_state("octo", "hello", 7)
    assert "small.py" in st["viewed"]


def test_comment_preserves_prefix_and_increments(client, monkeypatch):
    _load_pr(client, monkeypatch)
    captured = {}

    def fake_post(o, r, n, path, text):
        captured["path"] = path
        captured["text"] = text
        return True

    monkeypatch.setattr(gh, "post_pr_comment", fake_post)
    resp = client.post("/comment", json={"owner": "octo", "repo": "hello", "number": 7, "path": "big.py", "text": "nit"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # gh.post_pr_comment owns the **path** prefix; verify it received raw args
    assert captured["path"] == "big.py"
    assert captured["text"] == "nit"
    st = core.load_review_state("octo", "hello", 7)
    assert st["comments"] == 1
    # comment text persists per-file so the UI can render it as a bubble
    assert st["comment_threads"] == {"big.py": ["nit"]}
    # a second comment on the same file appends rather than replacing
    client.post("/comment", json={"owner": "octo", "repo": "hello", "number": 7, "path": "big.py", "text": "another"})
    st2 = core.load_review_state("octo", "hello", 7)
    assert st2["comment_threads"]["big.py"] == ["nit", "another"]
    assert st2["comments"] == 2
    # the state endpoint surfaces the threads so the client can hydrate bubbles
    state = client.get("/state/octo/hello/7").json()
    assert state["comment_threads"]["big.py"] == ["nit", "another"]


def test_review_submit_maps_event_and_marks_submitted(client, monkeypatch):
    _load_pr(client, monkeypatch)
    # flag a file first so the body assembly has content
    client.post("/file/flag", json={"owner": "octo", "repo": "hello", "number": 7, "path": "big.py", "flagged": True, "note": "risky"})

    captured = {}

    def fake_submit(o, r, n, event, body):
        captured["event"] = event
        captured["body"] = body
        return True, ""

    monkeypatch.setattr(gh, "submit_review", fake_submit)
    resp = client.post("/review/submit", json={"owner": "octo", "repo": "hello", "number": 7, "event": "request_changes"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert captured["event"] == "request_changes"
    assert "**Flagged files:**" in captured["body"]
    assert "`big.py`" in captured["body"]
    assert "risky" in captured["body"]
    st = core.load_review_state("octo", "hello", 7)
    assert st["submitted"] is True


def test_state_and_reviews(client, monkeypatch):
    _load_pr(client, monkeypatch)
    monkeypatch.setattr(gh, "mark_file_viewed", lambda o, r, n, p: True)
    client.post("/file/viewed", json={"owner": "octo", "repo": "hello", "number": 7, "path": "big.py"})

    rs = client.get("/state/octo/hello/7")
    assert rs.status_code == 200
    assert "big.py" in rs.json()["viewed"]

    rl = client.get("/reviews")
    assert rl.status_code == 200
    rows = rl.json()
    assert any(row["owner"] == "octo" and row["number"] == 7 for row in rows)
