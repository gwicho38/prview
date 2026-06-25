# prview

[![CI](https://github.com/gwicho38/prview/actions/workflows/ci.yml/badge.svg)](https://github.com/gwicho38/prview/actions/workflows/ci.yml)

A portable, local-server web app for reviewing GitHub pull requests file-by-file in your browser, with AI assistance. It ports the logic of the `mcli` `pr-review` CLI workflow to a graphical UI while reusing your existing `gh` and `claude` CLIs — no API keys, no tokens to manage.


See the full [user guide](docs/user-guide.md).

## What it does

- Load a PR by `owner/repo#123` or a GitHub URL.
- Walk the changed files (sorted by change size) with a side-by-side diff.
- Per-file **AI summary** (auto), **Explain**, and **Ask** — powered by your `claude` CLI. Responses are cached per file and the panel scrolls.
- **Mark viewed**, **flag** with notes, **comment** (file-level or anchored to selected diff lines, GitHub-style), and **submit a review** (approve / request changes / comment).
- Optional **Repowise** tab: codebase intelligence (architecture, code health, commits, coverage, AI docs) scoped to the whole repo *or* just the PR's changed files.
- Light & dark themes (toggle in the app bar); resumable per-PR state in `~/.prview/state`.

## Setup

prview drives your existing CLIs instead of managing API keys, so setup is mostly making sure `gh` and `claude` work, then installing prview.

### 1. GitHub CLI (`gh`) — required

prview reads PRs/diffs and posts reviews through `gh`.

```sh
# install (pick your platform)
brew install gh                 # macOS / Linuxbrew
# or: see https://github.com/cli/cli#installation

gh auth login                   # authenticate (choose GitHub.com → HTTPS → browser)
gh auth status                  # verify: should show "Logged in to github.com"
```

### 2. Claude CLI (`claude`) — required for AI features

The AI summary / Explain / Ask features shell out to the `claude` CLI (Claude Code). prview sends it the **full file diff**, so it benefits from a large context window.

```sh
# install Claude Code: https://docs.claude.com/claude-code
claude --version                # verify it's on your PATH
```

> Without `claude`, PR review still works — only the AI panel is disabled.

### 3. prview itself

```sh
# Python ≥ 3.10 and uv (https://docs.astral.sh/uv/)
git clone https://github.com/gwicho38/prview && cd prview
uv sync
uv run prview                   # or: python -m prview
```

`prview` picks a free `127.0.0.1` port, mints a per-session token, starts the server, and opens your browser automatically.

> There is no `./prview` script — the package directory occupies that name. Use `uv run prview` or `python -m prview`.

### 4. Repowise (optional) — for the codebase-intelligence tab

The **Repowise** tab embeds a [repowise](https://github.com/repowise/repowise) dashboard for the PR. It's optional; install it only if you want architecture/health/coverage/docs analysis.

```sh
uv tool install repowise        # provides the `repowise` CLI
node --version                  # Node ≥ 20 required (repowise serves a web UI)
```

For local, no-cost AI docs generation, also install [ollama](https://ollama.com) and pull a model:

```sh
ollama pull qwen2.5:3b          # fast; or a larger model for better prose
```

See **[Repowise tab](#repowise-tab-optional)** below for first-run steps.

## Reviewing a PR

1. Enter `owner/repo#123` (or a full GitHub PR URL) and press **Load PR**.
2. Click files in the sidebar (or `j`/`k`) to read each diff.
3. Use the AI panel: a summary auto-loads; **Explain** for a deep walk-through; **Ask** to query the file.
4. **Flag** files with notes, **Comment**, mark **Viewed**, then **Submit** your review.

**Keyboard shortcuts:** `v` viewed · `e` explain · `a` ask · `c` comment · `f` flag · `s` submit · `j`/`k` navigate · `q` back/close.

Reopen `prview` later and pick the PR from the resume list — your viewed/flagged state is restored.

### Comments

**Comment** posts to the PR. With no diff text selected it's a file-level comment; **select lines in the diff first** and it's posted as a GitHub *review comment* anchored to that line range — and rendered inline at the line, like GitHub's review UI. Your comments are cached per PR and shown back on the file.

### Ask, anchored

When you **Ask** a question that references something specific — a symbol, function, file, or line — the AI treats that reference as the anchor: it starts there and expands outward through the surrounding code as needed, unless you scope it otherwise.

## Repowise tab (optional)

If the [`repowise` CLI is installed](#4-repowise-optional--for-the-codebase-intelligence-tab), a **Repowise** tab appears next to **Review**. It embeds a repowise dashboard for the PR — architecture/knowledge graph, code health, commits, and more.

**First run:** open the tab; prview checks out the PR head into an isolated git worktree (under `~/.prview/worktrees`, so your clone is never touched — a dirty tree won't block it), indexes it, and starts the dashboard. You'll be asked once for the local path to your clone of the repo.

Two scopes, toggled in the tab's bar:

- **Complete** — the full repowise dashboard over the whole codebase.
- **Diff associations** — scoped to the PR's changed files: which files the diff touches, transitively-affected (1-hop+) files *not* in the diff, historical co-change partners missing from the PR, suggested reviewers, and an overall risk score.

Two more actions in the bar:

- **Ingest coverage** — the coverage / risk×coverage panels need a report. Generate one in your clone (e.g. `pytest --cov --cov-report=lcov`), then click **Ingest coverage** (blank path auto-detects `coverage.lcov`, `lcov.info`, `coverage.xml`, …; LCOV/Cobertura/Clover supported).
- **Generate docs** — the docs/wiki panel is AI-generated. Click **Generate docs**, pick a local **ollama** model (e.g. `qwen2.5:3b`), and prview runs the generation locally — free, no cloud key. Larger models give better prose but take longer; progress shows per page.

> The embedded dashboard's own chat defaults to ollama `llama3.2`; if you don't have that model pulled, either `ollama pull llama3.2` or pick a catalog model from its in-dashboard model menu.

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
