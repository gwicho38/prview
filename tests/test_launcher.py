"""Launcher tests (G6): free-port pick, token mint + wiring, URL build, and the
browser-open scheduling — all without starting a real server or browser.

Also covers the start/stop/open daemon lifecycle (G7): state file, liveness
checks, and each subcommand — all with subprocess/os.kill/webbrowser mocked out,
so tests never spawn a real background server or touch ~/.prview."""
import os
import socket

import pytest
from fastapi.testclient import TestClient

import prview.launcher as launcher
import prview.server as server


@pytest.fixture(autouse=True)
def isolated_daemon_state(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "STATE_DIR", tmp_path)
    monkeypatch.setattr(launcher, "DAEMON_FILE", tmp_path / "daemon.json")
    monkeypatch.setattr(launcher, "DAEMON_LOG", tmp_path / "daemon.log")


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


# ----------------------------------------------------------------------------
# Daemon state file
# ----------------------------------------------------------------------------
def test_daemon_state_roundtrips_through_write_read_clear():
    assert launcher.read_daemon_state() is None
    launcher.write_daemon_state({"pid": 123, "port": 8000, "token": "t", "url": "u"})
    assert launcher.read_daemon_state() == {"pid": 123, "port": 8000, "token": "t", "url": "u"}
    launcher.clear_daemon_state()
    assert launcher.read_daemon_state() is None


def test_clear_daemon_state_is_idempotent():
    launcher.clear_daemon_state()  # no file yet — must not raise
    launcher.clear_daemon_state()


def test_pid_is_alive_true_for_current_process():
    assert launcher.pid_is_alive(os.getpid()) is True


def test_pid_is_alive_false_for_pid_that_does_not_exist():
    assert launcher.pid_is_alive(2**30) is False


def test_port_is_listening_false_when_nothing_bound():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    assert launcher.port_is_listening(free_port, timeout=0.1) is False


def test_port_is_listening_true_when_bound_and_accepting():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert launcher.port_is_listening(port, timeout=0.5) is True


def test_running_daemon_none_when_no_state_file():
    assert launcher.running_daemon() is None


def test_running_daemon_returns_state_for_live_pid():
    launcher.write_daemon_state({"pid": os.getpid(), "port": 1, "token": "t", "url": "u"})
    assert launcher.running_daemon()["pid"] == os.getpid()


def test_running_daemon_clears_stale_state_for_dead_pid():
    launcher.write_daemon_state({"pid": 2**30, "port": 1, "token": "t", "url": "u"})
    assert launcher.running_daemon() is None
    assert launcher.read_daemon_state() is None  # stale file was cleaned up


# ----------------------------------------------------------------------------
# Subcommands (Popen / os.kill / webbrowser all mocked — no real process work)
# ----------------------------------------------------------------------------
class _FakeProc:
    def __init__(self, pid=4242, exits_immediately=False):
        self.pid = pid
        self._exits_immediately = exits_immediately

    def poll(self):
        return 1 if self._exits_immediately else None


def test_cmd_start_writes_state_and_prints_url(monkeypatch, capsys):
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: _FakeProc(pid=999))
    monkeypatch.setattr(launcher, "port_is_listening", lambda port, timeout=0.3: True)

    assert launcher.cmd_start() == 0

    state = launcher.read_daemon_state()
    assert state["pid"] == 999
    assert "token=" in state["url"]
    assert "prview →" in capsys.readouterr().out


def test_cmd_start_is_idempotent_when_already_running(monkeypatch, capsys):
    launcher.write_daemon_state({
        "pid": os.getpid(), "port": 1, "token": "t", "url": "http://127.0.0.1:1/?token=t",
    })
    called = {"popen": False}
    monkeypatch.setattr(launcher.subprocess, "Popen",
                         lambda *a, **k: called.update(popen=True) or _FakeProc())

    assert launcher.cmd_start() == 0
    assert called["popen"] is False  # no new process spawned
    assert "already running" in capsys.readouterr().out


def test_cmd_start_reports_failure_when_child_exits_immediately(monkeypatch, capsys):
    monkeypatch.setattr(launcher.subprocess, "Popen",
                         lambda *a, **k: _FakeProc(exits_immediately=True))
    monkeypatch.setattr(launcher, "port_is_listening", lambda port, timeout=0.3: False)

    assert launcher.cmd_start() == 1
    assert launcher.read_daemon_state() is None
    assert "failed to start" in capsys.readouterr().err


def test_cmd_stop_kills_pid_and_clears_state(monkeypatch, capsys):
    launcher.write_daemon_state({
        "pid": 999, "port": 1, "token": "t", "url": "http://127.0.0.1:1/?token=t",
    })
    killed = []
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    # first call (inside running_daemon / the wait loop) alive, then dead
    alive = iter([True, False])
    monkeypatch.setattr(launcher, "pid_is_alive", lambda pid: next(alive, False))

    assert launcher.cmd_stop() == 0
    assert killed[0] == (999, launcher.signal.SIGTERM)
    assert launcher.read_daemon_state() is None
    assert "prview stopped" in capsys.readouterr().out


def test_cmd_stop_is_a_noop_when_not_running(capsys):
    assert launcher.cmd_stop() == 0
    assert "not running" in capsys.readouterr().out


def test_cmd_open_opens_the_stored_url(monkeypatch):
    launcher.write_daemon_state({
        "pid": os.getpid(), "port": 1, "token": "t", "url": "http://127.0.0.1:1/?token=t",
    })
    opened = {}
    monkeypatch.setattr(launcher.webbrowser, "open", lambda u: opened.setdefault("url", u))

    assert launcher.cmd_open() == 0
    assert opened["url"] == "http://127.0.0.1:1/?token=t"


def test_cmd_open_errors_when_not_running(capsys):
    assert launcher.cmd_open() == 1
    assert "not running" in capsys.readouterr().err


def test_parser_dispatches_serve_subcommand_with_port_and_token():
    args = launcher.build_parser().parse_args(["_serve", "--port", "8123", "--token", "tok"])
    assert args.command == "_serve"
    assert args.port == 8123
    assert args.token == "tok"


@pytest.fixture
def free_preferred_port(monkeypatch):
    """Point PREFERRED_PORT at a port this test owns — the real one may be in use
    by a prview the developer is running, which is not the tests' business."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((launcher.HOST, 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(launcher, "PREFERRED_PORT", port)
    return port


def test_preferred_port_is_stable_across_launches(free_preferred_port):
    # Browser caches (the in-browser model weights, localStorage) are keyed by
    # origin, so a new port each launch silently discards them.
    assert launcher.pick_free_port() == launcher.pick_free_port() == free_preferred_port


def test_port_falls_back_when_the_preferred_one_is_taken(free_preferred_port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind((launcher.HOST, free_preferred_port))
        taken.listen(1)
        port = launcher.pick_free_port()
        assert port != free_preferred_port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((launcher.HOST, port))


def test_port_can_be_pinned_by_environment():
    import os
    os.environ["PRVIEW_PORT"] = "8123"
    try:
        assert launcher.pick_free_port() == 8123
    finally:
        del os.environ["PRVIEW_PORT"]
