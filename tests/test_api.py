"""API endpoint tests. gh/jobs are mocked at the prview.gh / prview.jobs
boundary — never a real subprocess. The security middleware is satisfied with
a valid token + a localhost Host header via the `client` fixture."""
import pytest
from fastapi.testclient import TestClient

import prview.core as core
import prview.gh as gh
import prview.jobs as jobs
import prview.order as order
import prview.server as server
import prview.state_store as state_store


TOKEN = "test-token-123"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    state_store.reset_locks()
    server.cache._store.clear()
    server._BEHAVIOR_CACHE.clear()
    server.set_session_token(TOKEN)
    c = TestClient(server.app)
    c.headers.update({"X-Prview-Token": TOKEN, "Host": "127.0.0.1"})
    return c


def _fake_pr(head_sha="sha-a"):
    return core.PRInfo(
        owner="octo", repo="hello", number=7,
        title="Add feature", author="alice", body="desc",
        base="main", head="feat", state="OPEN",
        additions=10, deletions=2, changed_files=2,
        head_sha=head_sha,
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
    # PR lifecycle state (OPEN/CLOSED/MERGED) is exposed for the frontend's PR badge.
    assert data["pr"]["state"] == "OPEN"


def test_pr_response_carries_every_review_order(client, monkeypatch):
    data = _load_pr(client, monkeypatch).json()
    names = {f["filename"] for f in data["files"]}
    assert set(data["orders"]) == set(order.MODES)
    for mode, listed in data["orders"].items():
        assert set(listed) == names, mode
    # `files` stays largest-first whatever the client later displays.
    assert [f["filename"] for f in data["files"]] == data["orders"]["churn"]


def test_pr_response_carries_head_sha(client, monkeypatch):
    resp = _load_pr(client, monkeypatch)
    assert resp.status_code == 200
    assert resp.json()["pr"]["head_sha"] == "sha-a"


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
    # file-level comment persists as an entry with no line anchor
    assert st["comment_threads"] == {"big.py": [{"text": "nit", "line": None, "start_line": None}]}
    # a second comment on the same file appends rather than replacing
    client.post("/comment", json={"owner": "octo", "repo": "hello", "number": 7, "path": "big.py", "text": "another"})
    st2 = core.load_review_state("octo", "hello", 7)
    assert [c["text"] for c in st2["comment_threads"]["big.py"]] == ["nit", "another"]
    assert st2["comments"] == 2
    # the state endpoint surfaces the threads so the client can hydrate bubbles
    state = client.get("/state/octo/hello/7").json()
    assert [c["text"] for c in state["comment_threads"]["big.py"]] == ["nit", "another"]


def test_comment_with_line_posts_review_comment(client, monkeypatch):
    _load_pr(client, monkeypatch)
    captured = {}

    def fake_review(owner, repo, number, path, text, commit_id, line, side="RIGHT",
                    start_line=None, start_side=None):
        captured.update(path=path, text=text, commit_id=commit_id, line=line,
                        side=side, start_line=start_line)
        return True

    monkeypatch.setattr(gh, "pr_head_sha", lambda o, r, n: "deadbeef")
    monkeypatch.setattr(gh, "post_pr_review_comment", fake_review)
    # a general PR comment must NOT be used when a line anchor is present
    monkeypatch.setattr(gh, "post_pr_comment",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("used general comment")))

    resp = client.post("/comment", json={
        "owner": "octo", "repo": "hello", "number": 7, "path": "big.py",
        "text": "range nit", "line": 5, "start_line": 3, "side": "RIGHT",
    })
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert captured == {"path": "big.py", "text": "range nit", "commit_id": "deadbeef",
                        "line": 5, "side": "RIGHT", "start_line": 3}
    # persisted with its line anchor so the UI can render it inline
    st = core.load_review_state("octo", "hello", 7)
    assert st["comment_threads"]["big.py"] == [{"text": "range nit", "line": 5, "start_line": 3}]
    state = client.get("/state/octo/hello/7").json()
    assert state["comment_threads"]["big.py"][0]["line"] == 5


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
    monkeypatch.setattr(gh, "latest_review_url", lambda o, r, n: None)
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


def test_archive_review_hides_then_unarchive_restores(client, monkeypatch):
    _load_pr(client, monkeypatch)
    monkeypatch.setattr(gh, "mark_file_viewed", lambda o, r, n, p: True)
    client.post("/file/viewed", json={"owner": "octo", "repo": "hello", "number": 7, "path": "big.py"})

    resp = client.post("/review/archive", json={"owner": "octo", "repo": "hello", "number": 7})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    active = client.get("/reviews").json()
    assert not any(row["number"] == 7 for row in active)

    everything = client.get("/reviews", params={"include_archived": True}).json()
    row = next(row for row in everything if row["number"] == 7)
    assert row["archived"] is True
    assert row["viewed_count"] == 1  # progress survives the archive

    resp = client.post("/review/archive", json={"owner": "octo", "repo": "hello", "number": 7, "archived": False})
    assert resp.status_code == 200
    restored = client.get("/reviews").json()
    assert any(row["number"] == 7 and row["archived"] is False for row in restored)


def test_file_full_returns_content_and_added_lines(client, monkeypatch):
    _load_pr(client, monkeypatch)
    monkeypatch.setattr(gh, "pr_head_sha", lambda o, r, n: "headsha")
    captured = {}

    def fake_fetch(o, r, path, ref):
        captured.update(path=path, ref=ref)
        return "line1\nline2\nline3\n"

    monkeypatch.setattr(gh, "fetch_file_at_ref", fake_fetch)
    resp = client.get("/pr/octo/hello/7/file/full", params={"path": "big.py"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "line1\nline2\nline3\n"
    assert captured == {"path": "big.py", "ref": "headsha"}
    assert isinstance(body["added_lines"], list)  # parsed from the cached diff


def test_file_full_surfaces_gh_error(client, monkeypatch):
    _load_pr(client, monkeypatch)
    monkeypatch.setattr(gh, "pr_head_sha", lambda o, r, n: "headsha")

    def boom(o, r, path, ref):
        raise gh.GhError("Failed to fetch file", hint="binary or too large")

    monkeypatch.setattr(gh, "fetch_file_at_ref", boom)
    resp = client.get("/pr/octo/hello/7/file/full", params={"path": "big.py"})
    assert resp.status_code == 400
    assert "Failed to fetch file" in resp.json()["error"]


def test_get_overview_no_cache(client, monkeypatch):
    _load_pr(client, monkeypatch)
    resp = client.get("/overview/octo/hello/7")
    assert resp.status_code == 200
    assert resp.json() == {"markdown": None, "sha": None, "stale": False}


def test_get_overview_hit_and_sha_invalidation(client, monkeypatch):
    _load_pr(client, monkeypatch)                      # head sha-a
    core.save_overview("octo", "hello", 7, "sha-a", "## OV")
    data = client.get("/overview/octo/hello/7").json()
    assert data == {"markdown": "## OV", "sha": "sha-a", "stale": False}

    # New head SHA → cached overview is stale → markdown withheld.
    monkeypatch.setattr(gh, "fetch_pr_info", lambda o, r, n: _fake_pr(head_sha="sha-b"))
    monkeypatch.setattr(gh, "fetch_pr_diff", lambda o, r, n: _fake_diff())
    client.post("/pr", json={"ref": "octo/hello#7"})
    data = client.get("/overview/octo/hello/7").json()
    assert data == {"markdown": None, "sha": "sha-a", "stale": True}


def test_get_overview_409_when_pr_not_loaded(client):
    assert client.get("/overview/octo/hello/7").status_code == 409


def test_post_ai_overview_starts_job(client, monkeypatch):
    _load_pr(client, monkeypatch)
    calls = {}

    def fake_start(pr, files, sha):
        calls["args"] = (pr.owner, len(files), sha)
        return "job-1"

    monkeypatch.setattr(jobs, "start_overview", fake_start)
    resp = client.post("/ai/overview", json={"owner": "octo", "repo": "hello", "number": 7})
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "job-1"
    assert calls["args"] == ("octo", 2, "sha-a")


def test_post_overview_comment_404_without_cache(client, monkeypatch):
    _load_pr(client, monkeypatch)
    resp = client.post("/overview/comment", json={"owner": "octo", "repo": "hello", "number": 7})
    assert resp.status_code == 404


def test_post_overview_comment_posts_stored_markdown(client, monkeypatch):
    _load_pr(client, monkeypatch)
    core.save_overview("octo", "hello", 7, "sha-a", "## OV body")
    posted = {}

    def fake_post(owner, repo, number, body):
        posted["body"] = body
        return True

    monkeypatch.setattr(gh, "post_pr_comment_file", fake_post)
    resp = client.post("/overview/comment", json={"owner": "octo", "repo": "hello", "number": 7})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert posted["body"] == "## OV body"


def test_review_submit_carries_review_url(client, monkeypatch):
    _load_pr(client, monkeypatch)
    monkeypatch.setattr(gh, "submit_review", lambda o, r, n, e, b: (True, ""))
    monkeypatch.setattr(gh, "latest_review_url",
                        lambda o, r, n: "https://github.com/octo/hello/pull/7#pullrequestreview-5")
    resp = client.post("/review/submit", json={"owner": "octo", "repo": "hello", "number": 7, "event": "comment"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["url"] == "https://github.com/octo/hello/pull/7#pullrequestreview-5"


def test_review_submit_url_null_when_lookup_fails(client, monkeypatch):
    _load_pr(client, monkeypatch)
    monkeypatch.setattr(gh, "submit_review", lambda o, r, n, e, b: (True, ""))
    monkeypatch.setattr(gh, "latest_review_url", lambda o, r, n: None)
    resp = client.post("/review/submit", json={"owner": "octo", "repo": "hello", "number": 7, "event": "comment"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "error": None, "url": None}


def _fake_commits():
    return [
        {"sha": "s1", "subject": "feat: big", "is_merge": False},
        {"sha": "s2", "subject": "fix: small", "is_merge": False},
    ]


def _wire_commits(monkeypatch, calls=None):
    monkeypatch.setattr(gh, "fetch_pr_commits", lambda o, r, n: _fake_commits())

    def files(o, r, sha):
        if calls is not None:
            calls.append(sha)
        return {"s1": ["big.py"], "s2": ["small.py"]}[sha]

    monkeypatch.setattr(gh, "fetch_commit_files", files)


def test_behaviors_endpoint_groups_the_prs_files(client, monkeypatch):
    _load_pr(client, monkeypatch)
    _wire_commits(monkeypatch)
    resp = client.get("/pr/octo/hello/7/behaviors")
    assert resp.status_code == 200
    data = resp.json()
    assert data["groupable"] is True
    assert data["head_sha"] == "sha-a"
    assert [(b["id"], b["title"], b["filenames"]) for b in data["behaviors"]] == [
        ("b1", "feat: big", ["big.py"]),
        ("b2", "fix: small", ["small.py"]),
    ]


def test_behaviors_are_cached_per_head_sha(client, monkeypatch):
    _load_pr(client, monkeypatch)
    calls = []
    _wire_commits(monkeypatch, calls)
    client.get("/pr/octo/hello/7/behaviors")
    client.get("/pr/octo/hello/7/behaviors")
    assert sorted(calls) == ["s1", "s2"], "second request must not re-fetch commit files"


def test_behaviors_cache_hit_never_refetches_commits(client, monkeypatch):
    _load_pr(client, monkeypatch)
    calls = []

    def commits(o, r, n):
        calls.append(1)
        return _fake_commits()

    monkeypatch.setattr(gh, "fetch_pr_commits", commits)
    monkeypatch.setattr(gh, "fetch_commit_files",
                        lambda o, r, sha: {"s1": ["big.py"], "s2": ["small.py"]}[sha])
    client.get("/pr/octo/hello/7/behaviors")
    client.get("/pr/octo/hello/7/behaviors")
    assert len(calls) == 1, "a cache hit must not spawn a gh subprocess"


def test_behaviors_endpoint_reports_a_single_commit_pr_as_ungroupable(client, monkeypatch):
    _load_pr(client, monkeypatch)
    monkeypatch.setattr(gh, "fetch_pr_commits",
                        lambda o, r, n: [{"sha": "s1", "subject": "feat: only", "is_merge": False}])
    monkeypatch.setattr(gh, "fetch_commit_files", lambda o, r, sha: ["big.py"])
    data = client.get("/pr/octo/hello/7/behaviors").json()
    assert data["groupable"] is False


def test_behaviors_endpoint_409s_when_commits_are_unreachable(client, monkeypatch):
    _load_pr(client, monkeypatch)

    def boom(o, r, n):
        raise gh.GhError("Failed to fetch PR commits: gone", hint="run `gh auth login`")

    monkeypatch.setattr(gh, "fetch_pr_commits", boom)
    resp = client.get("/pr/octo/hello/7/behaviors")
    assert resp.status_code == 409
    assert resp.json()["hint"]


def test_behaviors_endpoint_409s_without_a_loaded_pr(client):
    assert client.get("/pr/octo/hello/7/behaviors").status_code == 409


def test_behaviors_endpoint_409s_when_a_commits_file_list_is_unreachable(client, monkeypatch):
    _load_pr(client, monkeypatch)
    monkeypatch.setattr(gh, "fetch_pr_commits", lambda o, r, n: _fake_commits())

    def boom(o, r, sha):
        raise gh.GhError(f"Failed to fetch commit {sha[:7]}: gone", hint="run `gh auth login`")

    monkeypatch.setattr(gh, "fetch_commit_files", boom)
    resp = client.get("/pr/octo/hello/7/behaviors")
    assert resp.status_code == 409
    assert resp.json()["hint"]


def test_ai_behavior_names_starts_a_job(client, monkeypatch):
    _load_pr(client, monkeypatch)
    _wire_commits(monkeypatch)
    client.get("/pr/octo/hello/7/behaviors")
    monkeypatch.setattr(jobs, "start_behavior_names", lambda pr, derived, on_done=None: "job-xyz")
    resp = client.post("/ai/behavior-names", json={"owner": "octo", "repo": "hello", "number": 7})
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "job-xyz"


def test_behavior_naming_applies_a_valid_reply_and_ignores_a_bad_one(client, monkeypatch):
    _load_pr(client, monkeypatch)
    _wire_commits(monkeypatch)
    client.get("/pr/octo/hello/7/behaviors")
    captured = {}

    def _start(pr, derived, on_done=None):
        captured["cb"] = on_done
        return "j1"

    monkeypatch.setattr(jobs, "start_behavior_names", _start)
    client.post("/ai/behavior-names", json={"owner": "octo", "repo": "hello", "number": 7})

    captured["cb"]("b1 -> Handle the big thing\nb2 -> Handle the small thing")
    titles = [b["title"] for b in client.get("/pr/octo/hello/7/behaviors").json()["behaviors"]]
    assert titles == ["Handle the big thing", "Handle the small thing"]

    captured["cb"]("b9 -> Ghost")
    titles = [b["title"] for b in client.get("/pr/octo/hello/7/behaviors").json()["behaviors"]]
    assert titles == ["Handle the big thing", "Handle the small thing"], "bad reply must be ignored"


def test_behavior_comment_anchors_to_the_highest_tier_files_first_hunk(client, monkeypatch):
    _load_pr(client, monkeypatch)
    _wire_commits(monkeypatch)
    client.get("/pr/octo/hello/7/behaviors")
    seen = {}

    def capture(owner, repo, number, path, text, commit_id, line, side="RIGHT",
               start_line=None, start_side=None):
        seen.update(path=path, text=text, line=line, side=side, start_line=start_line)
        return True

    monkeypatch.setattr(gh, "post_pr_review_comment", capture)
    monkeypatch.setattr(gh, "pr_head_sha", lambda o, r, n: "sha-a")
    resp = client.post("/behaviors/comment", json={
        "owner": "octo", "repo": "hello", "number": 7,
        "behavior_id": "b1", "text": "why the churn tiebreak?",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "anchored": True, "path": "big.py", "line": 5}
    assert seen["path"] == "big.py"
    assert seen["line"] == 5
    assert seen["side"] == "RIGHT"
    assert seen["start_line"] == 1
    assert seen["text"].startswith("**On behavior: feat: big**")
    assert "(big.py)" in seen["text"]
    assert "why the churn tiebreak?" in seen["text"]


def test_behavior_comment_falls_back_to_a_file_comment_without_an_anchor(client, monkeypatch):
    _load_pr(client, monkeypatch)
    _wire_commits(monkeypatch)
    client.get("/pr/octo/hello/7/behaviors")
    monkeypatch.setattr(core, "first_hunk_range", lambda diff_text: None)
    posted = {}

    def capture_file(owner, repo, number, body):
        posted["body"] = body
        return True

    monkeypatch.setattr(gh, "post_pr_comment_file", capture_file)
    body = client.post("/behaviors/comment", json={
        "owner": "octo", "repo": "hello", "number": 7,
        "behavior_id": "b1", "text": "no anchor here",
    }).json()
    assert body["ok"] is True
    assert body["anchored"] is False
    assert "**On behavior: feat: big**" in posted["body"]


def test_behavior_comment_404s_on_an_unknown_behavior_id(client, monkeypatch):
    _load_pr(client, monkeypatch)
    _wire_commits(monkeypatch)
    client.get("/pr/octo/hello/7/behaviors")
    resp = client.post("/behaviors/comment", json={
        "owner": "octo", "repo": "hello", "number": 7,
        "behavior_id": "b99", "text": "ghost",
    })
    assert resp.status_code == 404


def test_behavior_comment_is_recorded_in_comment_threads(client, monkeypatch):
    _load_pr(client, monkeypatch)
    _wire_commits(monkeypatch)
    client.get("/pr/octo/hello/7/behaviors")
    monkeypatch.setattr(gh, "post_pr_review_comment", lambda *a, **k: True)
    monkeypatch.setattr(gh, "pr_head_sha", lambda o, r, n: "sha-a")
    client.post("/behaviors/comment", json={
        "owner": "octo", "repo": "hello", "number": 7,
        "behavior_id": "b1", "text": "on the record",
    })
    st = core.load_review_state("octo", "hello", 7)
    assert st["comment_threads"]["big.py"] == [{"text": "on the record", "line": 5, "start_line": 1}]


def test_behavior_comment_fallback_does_not_record_a_thread(client, monkeypatch):
    _load_pr(client, monkeypatch)
    _wire_commits(monkeypatch)
    client.get("/pr/octo/hello/7/behaviors")
    monkeypatch.setattr(core, "first_hunk_range", lambda diff_text: None)
    monkeypatch.setattr(gh, "post_pr_comment_file", lambda *a, **k: True)
    client.post("/behaviors/comment", json={
        "owner": "octo", "repo": "hello", "number": 7,
        "behavior_id": "b1", "text": "no anchor here",
    })
    st = core.load_review_state("octo", "hello", 7)
    assert st.get("comment_threads", {}) == {}


def test_behavior_comment_increments_the_review_comment_count(client, monkeypatch):
    _load_pr(client, monkeypatch)
    _wire_commits(monkeypatch)
    client.get("/pr/octo/hello/7/behaviors")
    monkeypatch.setattr(gh, "post_pr_review_comment", lambda *a, **k: True)
    monkeypatch.setattr(gh, "pr_head_sha", lambda o, r, n: "sha-a")
    client.post("/behaviors/comment", json={
        "owner": "octo", "repo": "hello", "number": 7,
        "behavior_id": "b1", "text": "counted",
    })
    assert client.get("/state/octo/hello/7").json()["comments"] == 1
