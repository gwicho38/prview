"""One-command launcher (G6): pick a free loopback port, mint a session token,
wire it into the server, then run uvicorn on 127.0.0.1 and auto-open the browser
at the token-carrying URL.

The testable pieces (port pick, token mint, URL build, browser-open scheduling)
are pure-ish helpers so tests never call uvicorn.run or open a real browser.
Binds 127.0.0.1 only — never 0.0.0.0.

`prview` (no args) keeps that original foreground+auto-open behavior. `start` /
`stop` / `open` manage a detached background server via a daemon state file at
~/.prview/daemon.json, so `prview start` returns immediately and the server
survives the calling shell exiting.
"""
import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

import prview.server as server

HOST = "127.0.0.1"
_BROWSER_DELAY_S = 0.6
_START_TIMEOUT_S = 5.0
_STOP_TIMEOUT_S = 5.0
_POLL_INTERVAL_S = 0.1

STATE_DIR = Path.home() / ".prview"
DAEMON_FILE = STATE_DIR / "daemon.json"
DAEMON_LOG = STATE_DIR / "daemon.log"


# A new port every launch is a new browser origin, which silently discards the
# localStorage prefs and the cached in-browser model weights. Prefer one port.
PREFERRED_PORT = 8420


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def pick_free_port() -> int:
    pinned = os.environ.get("PRVIEW_PORT")
    if pinned:
        return int(pinned)
    if _port_is_free(PREFERRED_PORT):
        return PREFERRED_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def mint_token() -> str:
    return secrets.token_urlsafe(32)


def build_launch_url(port: int, token: str) -> str:
    return f"http://{HOST}:{port}/?token={token}"


def schedule_browser_open(url: str, delay: float = _BROWSER_DELAY_S) -> threading.Timer:
    timer = threading.Timer(delay, lambda: webbrowser.open(url))
    timer.daemon = True
    timer.start()
    return timer


# ----------------------------------------------------------------------------
# Daemon state (~/.prview/daemon.json) — read/write are the only side effects;
# the liveness checks below are pure queries so tests can call them directly.
# ----------------------------------------------------------------------------
def read_daemon_state() -> dict | None:
    try:
        return json.loads(DAEMON_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_daemon_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DAEMON_FILE.write_text(json.dumps(state))


def clear_daemon_state() -> None:
    DAEMON_FILE.unlink(missing_ok=True)


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


def port_is_listening(port: int, timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            return s.connect_ex((HOST, port)) == 0
        except OSError:
            return False


def running_daemon() -> dict | None:
    """Live daemon state, or None — clearing a stale file left by a dead pid."""
    state = read_daemon_state()
    if state is None:
        return None
    if pid_is_alive(state["pid"]):
        return state
    clear_daemon_state()
    return None


# ----------------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------------
def cmd_start() -> int:
    existing = running_daemon()
    if existing:
        print(f"prview already running → {existing['url']}", flush=True)
        return 0
    if sys.platform == "win32":
        print("`prview start` (background mode) isn't supported on Windows — use `prview`.",
              file=sys.stderr)
        return 1

    port = pick_free_port()
    token = mint_token()
    url = build_launch_url(port, token)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(DAEMON_LOG, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "prview", "_serve", "--port", str(port), "--token", token],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
        )

    deadline = time.monotonic() + _START_TIMEOUT_S
    while time.monotonic() < deadline:
        if port_is_listening(port):
            write_daemon_state({
                "pid": proc.pid, "port": port, "token": token, "url": url,
                "started_at": datetime.now(timezone.utc).isoformat(),
            })
            print(f"prview → {url}  (background, pid {proc.pid})", flush=True)
            print("`prview open` to open it in your browser, `prview stop` to stop it.", flush=True)
            return 0
        if proc.poll() is not None:
            break
        time.sleep(_POLL_INTERVAL_S)

    tail = DAEMON_LOG.read_text()[-2000:] if DAEMON_LOG.exists() else ""
    print(f"prview failed to start:\n{tail}", file=sys.stderr)
    return 1


def cmd_stop() -> int:
    state = running_daemon()
    if not state:
        print("prview is not running.", flush=True)
        return 0

    pid = state["pid"]
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _STOP_TIMEOUT_S
    while time.monotonic() < deadline and pid_is_alive(pid):
        time.sleep(_POLL_INTERVAL_S)
    if pid_is_alive(pid):
        os.kill(pid, signal.SIGKILL)
    clear_daemon_state()
    print("prview stopped.", flush=True)
    return 0


def cmd_open() -> int:
    state = running_daemon()
    if not state:
        print("prview is not running — start it first with `prview start`.", file=sys.stderr)
        return 1
    webbrowser.open(state["url"])
    print(f"opened {state['url']}", flush=True)
    return 0


def _serve(port: int, token: str) -> None:
    """Background-daemon entry point (`prview _serve`) — foreground, no browser-open."""
    import prview.repowise as repowise

    server.set_session_token(token)
    try:
        uvicorn.run(server.app, host=HOST, port=port, log_level="warning")
    finally:
        repowise.stop_all()


def _run_foreground() -> None:
    """Legacy default: foreground server, auto-opens the browser."""
    # Imported lazily: repowise imports pick_free_port from this module, so a
    # top-level import here would be a cycle. Nothing cleans up the long-lived
    # `repowise serve` children today — the finally hook below terminates them.
    import prview.repowise as repowise

    port = pick_free_port()
    token = mint_token()
    server.set_session_token(token)
    url = build_launch_url(port, token)
    print(f"prview → {url}", flush=True)
    schedule_browser_open(url)
    try:
        uvicorn.run(server.app, host=HOST, port=port, log_level="warning")
    finally:
        repowise.stop_all()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prview")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("start", help="start prview in the background")
    sub.add_parser("stop", help="stop the background prview")
    sub.add_parser("open", help="open prview in your browser")
    serve_p = sub.add_parser("_serve", help=argparse.SUPPRESS)
    serve_p.add_argument("--port", type=int, required=True)
    serve_p.add_argument("--token", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.command == "start":
        raise SystemExit(cmd_start())
    if args.command == "stop":
        raise SystemExit(cmd_stop())
    if args.command == "open":
        raise SystemExit(cmd_open())
    if args.command == "_serve":
        _serve(args.port, args.token)
        return

    _run_foreground()


if __name__ == "__main__":
    main()
