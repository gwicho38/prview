"""Security middleware tests: per-session token + Origin/Host check, and the
argv-only guarantee (no client string is ever shell-interpolated)."""
import pytest
from fastapi.testclient import TestClient

import prview.core as core
import prview.gh as gh
import prview.server as server
import prview.state_store as state_store


TOKEN = "secret-token-xyz"


@pytest.fixture
def raw_client(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    state_store.reset_locks()
    server.cache._store.clear()
    server.set_session_token(TOKEN)
    return TestClient(server.app)


def test_missing_token_rejected_401(raw_client):
    resp = raw_client.get("/reviews", headers={"Host": "127.0.0.1"})
    assert resp.status_code == 401
    body = resp.json()
    assert "error" in body
    assert "traceback" not in str(body).lower()


def test_wrong_token_rejected_401(raw_client):
    resp = raw_client.get("/reviews", headers={"X-Prview-Token": "wrong", "Host": "127.0.0.1"})
    assert resp.status_code == 401


def test_valid_token_allowed(raw_client):
    resp = raw_client.get("/reviews", headers={"X-Prview-Token": TOKEN, "Host": "127.0.0.1"})
    assert resp.status_code == 200


def test_foreign_host_rejected(raw_client):
    resp = raw_client.get(
        "/reviews",
        headers={"X-Prview-Token": TOKEN, "Host": "evil.example.com"},
    )
    assert resp.status_code == 403
    assert "error" in resp.json()


def test_foreign_origin_rejected(raw_client):
    resp = raw_client.get(
        "/reviews",
        headers={
            "X-Prview-Token": TOKEN,
            "Host": "127.0.0.1",
            "Origin": "https://evil.example.com",
        },
    )
    assert resp.status_code == 403


def test_same_origin_allowed(raw_client):
    resp = raw_client.get(
        "/reviews",
        headers={
            "X-Prview-Token": TOKEN,
            "Host": "127.0.0.1",
            "Origin": "http://127.0.0.1",
        },
    )
    assert resp.status_code == 200


def test_initial_html_get_allowed_without_token(raw_client):
    # the landing page GET must be reachable so the client can grab the token
    resp = raw_client.get("/", headers={"Host": "127.0.0.1"})
    assert resp.status_code in (200, 404)  # 200 with index, 404 if no static index yet
    assert resp.status_code != 401


def test_client_string_reaches_argv_only(tmp_path, monkeypatch):
    """A comment body with shell metacharacters must arrive at gh.post_pr_comment
    verbatim as a discrete argument — never assembled into a shell string."""
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    state_store.reset_locks()
    server.cache._store.clear()
    server.set_session_token(TOKEN)
    c = TestClient(server.app)
    c.headers.update({"X-Prview-Token": TOKEN, "Host": "127.0.0.1"})

    monkeypatch.setattr(gh, "fetch_pr_info", lambda o, r, n: core.PRInfo(owner="o", repo="r", number=1, title="t"))
    monkeypatch.setattr(gh, "fetch_pr_diff", lambda o, r, n: "diff --git a/f.py b/f.py\n+x\n")
    c.post("/pr", json={"ref": "o/r#1"})

    captured = {}
    monkeypatch.setattr(gh, "post_pr_comment", lambda o, r, n, path, text: captured.update(path=path, text=text) or True)

    injection = "$(rm -rf /); `whoami` && echo pwned"
    c.post("/comment", json={"owner": "o", "repo": "r", "number": 1, "path": "f.py", "text": injection})
    # the dangerous string is passed through unmodified as a value, not interpreted
    assert captured["text"] == injection
