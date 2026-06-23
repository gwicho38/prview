# prview

[![CI](https://github.com/gwicho38/prview/actions/workflows/ci.yml/badge.svg)](https://github.com/gwicho38/prview/actions/workflows/ci.yml)

A portable, local-server web app for reviewing GitHub pull requests file-by-file in your browser, with AI assistance. It ports the logic of the `mcli` `pr-review` CLI workflow to a graphical UI while reusing your existing `gh` and `claude` CLIs — no API keys, no tokens to manage.

![Review workspace](docs/screenshots/03-review-loaded.png)

See the full [user guide](docs/user-guide.md).

## What it does

- Load a PR by `owner/repo#123` or a GitHub URL.
- Walk the changed files (sorted by change size) with a side-by-side diff.
- Per-file **AI summary** (auto), **Explain**, and **Ask** — powered by your `claude` CLI.
- **Mark viewed**, **flag** with notes, **comment**, and **submit a review** (approve / request changes / comment).
- Resumable: per-PR review state persists in `~/.prview/state`.

## Prerequisites

- [`gh`](https://cli.github.com) installed and authenticated (`gh auth login`).
- `claude` CLI available on your `PATH`.
- Python ≥ 3.10 and [`uv`](https://docs.astral.sh/uv/).

## Install & run

```sh
uv sync
uv run prview          # or: python -m prview
```

`prview` picks a free `127.0.0.1` port, mints a per-session token, starts the server, and opens your browser automatically.

> There is no `./prview` script — the package directory occupies that name. Use `uv run prview` or `python -m prview`.

## Reviewing a PR

1. Enter `owner/repo#123` (or a full GitHub PR URL) and press **Load PR**.
2. Click files in the sidebar (or `j`/`k`) to read each diff.
3. Use the AI panel: a summary auto-loads; **Explain** for a deep walk-through; **Ask** to query the file.
4. **Flag** files with notes, **Comment**, mark **Viewed**, then **Submit** your review.

**Keyboard shortcuts:** `v` viewed · `e` explain · `a` ask · `c` comment · `f` flag · `s` submit · `j`/`k` navigate · `q` back/close.

Reopen `prview` later and pick the PR from the resume list — your viewed/flagged state is restored.

## Security

`prview` binds to `127.0.0.1` only, requires a per-session token on every API call (validated via the `X-Prview-Token` header / `?token=` on first load), and checks the `Origin`/`Host` headers. It runs `claude --dangerously-skip-permissions` locally to drive non-interactive AI calls. Because the server can run privileged `gh`/`claude` commands on your behalf, **do not expose its port** beyond localhost.

## Development

Common tasks are wrapped in the `Makefile` (`make help` to list them):

```sh
make install        # uv sync (deps + dev group)
make test           # uv run pytest — full suite
make run            # launch prview
make docker-build   # build the container image
```

The codebase keeps a pure functional core (`prview/core.py`) with all subprocess / filesystem / network I/O pushed to the edges (`gh.py`, `jobs.py`, `state_store.py`, `server.py`). The diff renderer (diff2html) is vendored under `prview/static/vendor/` — the app makes zero external network requests at runtime.

## Container

```sh
make docker-build   # docker build -t prview:dev .
```

The image builds and runs the server, and CI verifies it on every push. Note: live PR
review shells out to the host's `gh` and `claude` CLIs, which are **not** baked into the
image — so the container is for build/CI verification and reproducible packaging; for
actual reviewing, run prview on your host (`uv run prview`).

## License

[MIT](LICENSE).
