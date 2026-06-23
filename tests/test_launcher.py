"""Launcher tests (G6): free-port pick, token mint + wiring, URL build, and the
browser-open scheduling — all without starting a real server or browser."""
import socket

from fastapi.testclient import TestClient

import prview.launcher as launcher
import prview.server as server


def test_free_port_is_bindable_loopback_int():
    port = launcher.pick_free_port()
    assert isinstance(port, int)
    assert 1 <= port <= 65535
    # the picked port must be free to bind on loopback right now
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_token_is_high_entropy_and_unique():
    a = launcher.mint_token()
    b = launcher.mint_token()
    assert a != b
    assert len(a) >= 32  # secrets.token_urlsafe(32) → ~43 url-safe chars


def test_minted_token_accepted_other_rejected():
    token = launcher.mint_token()
    server.set_session_token(token)
    client = TestClient(server.app)

    ok = client.get("/reviews", headers={"X-Prview-Token": token, "Host": "127.0.0.1"})
    assert ok.status_code == 200

    bad = client.get("/reviews", headers={"X-Prview-Token": "not-the-token", "Host": "127.0.0.1"})
    assert bad.status_code == 401


def test_launch_url_carries_token():
    url = launcher.build_launch_url(8123, "tok-abc")
    assert url == "http://127.0.0.1:8123/?token=tok-abc"


def test_browser_open_scheduled_not_blocking(monkeypatch):
    opened = {}
    monkeypatch.setattr(launcher.webbrowser, "open", lambda u: opened.setdefault("url", u))

    timer = launcher.schedule_browser_open("http://127.0.0.1:9000/?token=z", delay=0.0)
    timer.join(timeout=2.0)
    assert opened["url"] == "http://127.0.0.1:9000/?token=z"
