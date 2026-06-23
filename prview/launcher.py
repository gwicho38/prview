"""One-command launcher (G6): pick a free loopback port, mint a session token,
wire it into the server, then run uvicorn on 127.0.0.1 and auto-open the browser
at the token-carrying URL.

The testable pieces (port pick, token mint, URL build, browser-open scheduling)
are pure-ish helpers so tests never call uvicorn.run or open a real browser.
Binds 127.0.0.1 only — never 0.0.0.0.
"""
import secrets
import socket
import threading
import webbrowser

import uvicorn

import prview.server as server

HOST = "127.0.0.1"
_BROWSER_DELAY_S = 0.6


def pick_free_port() -> int:
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


def main() -> None:
    port = pick_free_port()
    token = mint_token()
    server.set_session_token(token)
    url = build_launch_url(port, token)
    print(f"prview → {url}", flush=True)
    schedule_browser_open(url)
    uvicorn.run(server.app, host=HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
