"""repowise integration: long-lived `repowise serve` registry + prepare edges.

A NEW module — NOT in jobs.py. jobs.py models one-shot, self-terminating
children (Popen + communicate); `repowise serve` is a LONG-LIVED child that
must outlive the request, be port-tracked, reused across a repo's PRs, and
explicitly torn down. Different lifetime → its own module.

Structure: functional core (pure decisions, unit-tested with no mocks) up top;
side-effecting edges (subprocess preflight/checkout/serve, frameability probe)
below. All argv is FIXED; client-supplied paths are discrete argv elements and
are NEVER shell-interpolated (mirrors gh.py / jobs.py). No side effects at
import.
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from prview import core


# ===========================================================================
# Errors
# ===========================================================================

@dataclass
class RepowiseError(Exception):
    message: str
    hint: str = ""

    def __str__(self) -> str:
        return f"{self.message} ({self.hint})" if self.hint else self.message


_MISSING_HINT = "install it: uv tool install repowise"
_NODE_HINT = "install Node.js 20+ (https://nodejs.org) — the dashboard is a Next.js app"
_MIN_NODE_MAJOR = 20


# ===========================================================================
# Functional core — pure decisions (no I/O, unit-tested without mocks)
# ===========================================================================

# A clone's origin can be SSH or HTTPS; normalize to owner/repo (drop .git).
_REMOTE_RE = re.compile(
    r"(?:github\.com[:/])([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def _remote_matches(remote_url: str, owner: str, repo: str) -> bool:
    m = _REMOTE_RE.search(remote_url.strip())
    return bool(m) and m.group(1) == owner and m.group(2) == repo


def validate_repo_path_decision(
    *,
    path_exists: bool,
    git_dir_present: bool,
    remote_url: str,
    owner: str,
    repo: str,
) -> dict:
    """Decide if a clone path is valid for owner/repo, given pre-parsed facts.

    PURE: callers do the filesystem stat + `git remote` shell-out and pass the
    results in; this only decides. Returns {"ok": True} or {ok:False,error,hint}.
    """
    if not path_exists:
        return {"ok": False, "error": "Path does not exist",
                "hint": "enter the absolute path to an existing local clone"}
    if not git_dir_present:
        return {"ok": False, "error": "Not a git repository",
                "hint": "expected a directory containing .git"}
    if not _remote_matches(remote_url, owner, repo):
        return {"ok": False, "error": f"Remote does not match {owner}/{repo}",
                "hint": f"this clone's origin is {remote_url.strip() or '(none)'}"}
    return {"ok": True}


def parse_frameability(headers: dict) -> bool:
    """True if the dashboard root may be embedded in an iframe.

    PURE header parse. X-Frame-Options DENY/SAMEORIGIN → not frameable;
    CSP frame-ancestors (any directive present) → not frameable; absent → ok.
    """
    lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    xfo = lower.get("x-frame-options", "").strip().upper()
    if xfo in ("DENY", "SAMEORIGIN"):
        return False
    csp = lower.get("content-security-policy", "").lower()
    if "frame-ancestors" in csp:
        return False
    return True


def is_indexed_decision(*, repowise_dir_files) -> bool:
    """True only when the index DB wiki.db is present in .repowise/.

    PURE: caller lists <path>/.repowise/ and passes the basenames. Bare
    .repowise/ (config.yaml/state.json, NO wiki.db) is NOT indexed — skipping
    on it would embed an empty dashboard.
    """
    return bool(repowise_dir_files) and "wiki.db" in repowise_dir_files


def parse_node_major(version_str) -> int | None:
    """Parse the major version from `node --version` output (e.g. 'v20.11.1')."""
    if not version_str:
        return None
    m = re.match(r"v?(\d+)\.", version_str.strip())
    return int(m.group(1)) if m else None


def node_ok_decision(version_str) -> bool:
    major = parse_node_major(version_str)
    return major is not None and major >= _MIN_NODE_MAJOR


# ---------------------------------------------------------------------------
# Prepare-step state machine (PURE)
# ---------------------------------------------------------------------------

_STEP_KEYS = ("resolve_path", "checkout", "index", "serve", "open")
_PENDING, _RUNNING, _DONE, _SKIPPED, _FAILED = (
    "pending", "running", "done", "skipped", "failed",
)


class PrepareSteps:
    """Ordered prepare steps with pure status transitions.

    Keys: resolve_path|checkout|index|serve|open. Status ∈
    pending|running|done|skipped|failed. A failed step records error_step +
    error + hint. snapshot() yields the shape rendered by the prepare UI.
    """

    def __init__(self):
        self._status = {k: _PENDING for k in _STEP_KEYS}
        self._detail = {k: "" for k in _STEP_KEYS}
        self.error_step: str | None = None
        self.error: str | None = None
        self.error_hint: str | None = None

    def status_of(self, key: str) -> str:
        return self._status[key]

    def start(self, key: str, detail: str = ""):
        self._status[key] = _RUNNING
        self._detail[key] = detail

    def done(self, key: str, detail: str = ""):
        self._status[key] = _DONE
        if detail:
            self._detail[key] = detail

    def skip(self, key: str, detail: str = ""):
        self._status[key] = _SKIPPED
        self._detail[key] = detail

    def fail(self, key: str, *, error: str, hint: str = ""):
        self._status[key] = _FAILED
        self.error_step = key
        self.error = error
        self.error_hint = hint

    def snapshot(self) -> dict:
        return {
            "steps": [
                {"key": k, "status": self._status[k], "detail": self._detail[k]}
                for k in _STEP_KEYS
            ],
            "error_step": self.error_step,
            "error": self.error,
            "error_hint": self.error_hint,
        }


# ===========================================================================
# Imperative shell — subprocess edges
# ===========================================================================

def _run(argv: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a fixed-argv command, mapping a missing binary to a RepowiseError.

    Mirrors gh.py:_run — a missing CLI raises FileNotFoundError → an unhandled
    500 with a leaked stack; we surface an actionable install hint instead.
    """
    try:
        return subprocess.run(argv, capture_output=True, text=True, cwd=cwd)
    except FileNotFoundError:
        binary = argv[0]
        hint = _MISSING_HINT if binary == "repowise" else _NODE_HINT
        raise RepowiseError(f"`{binary}` not found", hint=hint)


# --- 1.4 Preflight --------------------------------------------------------

def cli_present() -> tuple[bool, str | None]:
    """(cli_present, cli_hint) for /repowise/status — `repowise --version`."""
    try:
        result = _run(["repowise", "--version"])
    except RepowiseError as exc:
        return False, exc.hint
    if result.returncode != 0:
        return False, _MISSING_HINT
    return True, None


def node_present() -> tuple[bool, str | None]:
    """(node_ok, node_hint) for /repowise/status — `node --version`, major ≥ 20."""
    try:
        result = _run(["node", "--version"])
    except RepowiseError:
        return False, _NODE_HINT  # missing binary → node hint, not the repowise install hint
    if result.returncode != 0 or not node_ok_decision(result.stdout):
        return False, _NODE_HINT
    return True, None


# --- 1.5 Repo-path resolution ---------------------------------------------

def resolve_repo_path(owner: str, repo: str) -> str | None:
    """Return the persisted local path for owner/repo, or None ('unknown')."""
    return core.get_repo_path(owner, repo)


def validate_and_persist_path(owner: str, repo: str, path: str) -> dict:
    """Validate a candidate clone path (FR-4) and persist on success.

    Edge: stats the path + reads `git remote get-url origin`, then defers to
    the PURE validate_repo_path_decision. Persists via core on ok.
    """
    expanded = Path(path).expanduser()
    path_exists = expanded.is_dir()
    git_dir_present = (expanded / ".git").exists()
    remote_url = ""
    if path_exists and git_dir_present:
        result = _run(
            ["git", "-C", str(expanded), "remote", "get-url", "origin"],
        )
        if result.returncode == 0:
            remote_url = result.stdout

    decision = validate_repo_path_decision(
        path_exists=path_exists,
        git_dir_present=git_dir_present,
        remote_url=remote_url,
        owner=owner,
        repo=repo,
    )
    if decision.get("ok"):
        resolved = str(expanded.resolve())
        core.set_repo_path(owner, repo, resolved)
        return {"ok": True, "path": resolved}
    return decision


# --- 1.6 Checkout edge ----------------------------------------------------

# --- 1.6 PR worktree ------------------------------------------------------
# Materialize the PR head in an ISOLATED git worktree of the user's clone
# instead of checking it out in place. The user's working tree is never
# touched, so a dirty tree no longer blocks a prepare. Worktrees live under
# ~/.prview/worktrees (OUTSIDE the clone) so they never pollute its git status.

_WORKTREE_DIR = Path.home() / ".prview" / "worktrees"

# key "repo_path#number" → (repo_path, worktree_path), for teardown.
_worktrees: dict[str, tuple[str, str]] = {}
_worktrees_lock = threading.Lock()


def _worktree_path(repo_path: str, number: int) -> Path:
    slug = Path(repo_path).name or "repo"
    return _WORKTREE_DIR / f"{slug}-pr-{number}"


def prepare_pr_worktree(repo_path: str, number: int) -> tuple[str, str]:
    """Check the PR head out into an isolated worktree. Returns (path, short_rev).

    No dirty-guard: the worktree is a separate checkout, so uncommitted work in
    repo_path is left alone. Fetch the PR head into a local ref, then add a
    detached worktree at that ref. The stale worktree (if any) is removed first
    so a force-pushed PR head is always picked up fresh.

    NOTE: re-verify fork-head handling against the LIVE repowise/gh CLI — for a
    fork PR, `git fetch origin pull/<n>/head` resolves the head on the upstream
    even when the contributor's branch lives on a fork (spec fact #7).
    """
    ref = f"prview/pr-{number}"
    fetch = _run(["git", "-C", repo_path, "fetch", "origin",
                  f"pull/{number}/head:{ref}"])
    if fetch.returncode != 0:
        raise RepowiseError(
            f"git fetch failed: {fetch.stderr.strip()}",
            hint="confirm origin points at the PR's repo and you have access",
        )
    wt = _worktree_path(repo_path, number)
    wt.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "-C", repo_path, "worktree", "remove", "--force", str(wt)])
    add = _run(["git", "-C", repo_path, "worktree", "add", "--force",
                "--detach", str(wt), ref])
    if add.returncode != 0:
        raise RepowiseError(
            f"git worktree add failed: {add.stderr.strip()}",
            hint="could not materialize the PR head in a worktree",
        )
    with _worktrees_lock:
        _worktrees[f"{repo_path}#{number}"] = (repo_path, str(wt))
    rev = _run(["git", "-C", str(wt), "rev-parse", "--short", "HEAD"])
    return str(wt), rev.stdout.strip() if rev.returncode == 0 else ""


def remove_all_worktrees() -> None:
    """Prune every worktree prview created (shutdown). Best-effort, fixed-argv."""
    with _worktrees_lock:
        items = list(_worktrees.values())
        _worktrees.clear()
    for repo_path, wt_path in items:
        _run(["git", "-C", repo_path, "worktree", "remove", "--force", wt_path])


# --- 1.7 Ensure-index -----------------------------------------------------

def _list_repowise_dir(repo_path: str):
    d = Path(repo_path) / ".repowise"
    if not d.is_dir():
        return None
    return [p.name for p in d.iterdir()]


def is_repo_indexed(repo_path: str) -> bool:
    """Read-only index check for GET /repowise/status (no subprocess)."""
    return is_indexed_decision(repowise_dir_files=_list_repowise_dir(repo_path))


def ensure_indexed(repo_path: str) -> bool:
    """Index the repo unless already indexed. Returns True if skipped.

    Gate on wiki.db (via the PURE is_indexed_decision), NOT bare .repowise/.
    Uses --index-only for a fast, LLM-free first pass — tradeoff: it builds the
    structural index without the (slower, paid) LLM doc-gen, which is enough to
    serve a populated dashboard; a full `repowise init` can run later.
    """
    if is_indexed_decision(repowise_dir_files=_list_repowise_dir(repo_path)):
        return True
    result = _run(["repowise", "init", repo_path, "--yes", "--index-only"])
    if result.returncode != 0:
        raise RepowiseError(
            f"repowise init failed: {result.stderr.strip()}",
            hint="run `repowise init` manually in the clone to see details",
        )
    return False


# ===========================================================================
# 1.8–1.10 Serve registry (long-lived children) + teardown
# ===========================================================================

@dataclass
class ServeEntry:
    pid: int
    api_port: int
    ui_port: int
    url: str
    started_at: float
    repo_path: str
    frameable: bool | None = None
    logfile: str = ""
    _proc: "subprocess.Popen | None" = field(default=None, repr=False)


_serves: dict[str, ServeEntry] = {}
_serves_lock = threading.Lock()

_SERVE_LOG_DIR = Path.home() / ".prview" / "serve-logs"


def _read_tail(path: str, limit: int = 4000) -> str:
    """Last `limit` chars of a serve logfile (for the 'View output' affordance)."""
    try:
        data = Path(path).read_text(errors="replace")
    except OSError:
        return ""
    return data[-limit:]


def _terminate_proc(proc: "subprocess.Popen") -> None:
    """Tear down a long-lived serve child + its grandchildren (the API server,
    the Next.js UI, node workers). The child is spawned in its own session
    (start_new_session) so we signal the whole process GROUP, escalating
    TERM → wait → KILL so no ports are left bound."""
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:
            return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def _serve_env() -> dict:
    # serve prompts interactively to pick a chat/search embedder unless one is
    # configured; under a non-TTY Popen that prompt aborts and serve exits
    # before the dashboard comes up. Pre-seed REPOWISE_EMBEDDER so _setup_embedder
    # returns early. Prefer a REAL embedder matching an available provider key
    # (so chat/search work), falling back to `mock` (no key, dashboard-only).
    # An explicit REPOWISE_EMBEDDER always wins.
    env = dict(os.environ)
    if not env.get("REPOWISE_EMBEDDER"):
        if env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"):
            env["REPOWISE_EMBEDDER"] = "gemini"
        elif env.get("OPENAI_API_KEY"):
            env["REPOWISE_EMBEDDER"] = "openai"
        elif env.get("OPENROUTER_API_KEY"):
            env["REPOWISE_EMBEDDER"] = "openrouter"
        else:
            env["REPOWISE_EMBEDDER"] = "mock"
    return env


# The bundled Next.js UI bakes its API proxy target at BUILD time:
# next.config rewrites send /api/* -> http://localhost:7337 and ignore
# REPOWISE_API_URL. So the embedded dashboard only works when the API server
# listens on 7337 — a random --port leaves every panel proxying to a dead
# 7337 (ECONNREFUSED → 500). The API port is therefore fixed; only the UI port
# (which the UI honours via PORT) is dynamic. One consequence: at most ONE
# repowise serve can run at a time (they would collide on 7337), so ensure_serve
# tears down any other live serve before starting a new one.
_API_PORT = 7337


def _serve_argv(ui_port: int) -> list[str]:
    # `repowise serve` (0.23) takes NO path argument and has no --yes /
    # --no-workspace options — it resolves the repo from the cwd's
    # .repowise/wiki.db, so ensure_serve runs it with cwd=<worktree>. Two
    # servers: API on --port 7337 (see _API_PORT), Next.js Web UI on --ui-port
    # (the dashboard we embed). --host 127.0.0.1 keeps both on loopback.
    # DOCUMENTED EXPOSURE: the embedded iframe targets this different-origin
    # loopback server, which is NOT behind prview's session-token gate
    # (SecurityMiddleware only guards the prview app). It is reachable by any
    # local process; acceptable for a localhost dev tool.
    return [
        "repowise", "serve",
        "--host", "127.0.0.1",
        "--port", str(_API_PORT),
        "--ui-port", str(ui_port),
    ]


def get_serve(owner: str, repo: str) -> ServeEntry | None:
    with _serves_lock:
        return _serves.get(f"{owner}/{repo}")


def ensure_serve(owner: str, repo: str, repo_path: str) -> ServeEntry:
    """Lazy-start one `repowise serve` per repo; reuse on repeat prepares.

    Fire-and-forget Popen (NO communicate — long-lived child, unlike jobs.py
    one-shot). Allocates BOTH ports via pick_free_port (called twice). The
    embedded `url` is the UI-port dashboard.
    """
    # Lazy import breaks the launcher↔server↔repowise import cycle: launcher
    # imports server, server imports repowise; importing launcher at this
    # module's top would close the loop. pick_free_port is not duplicated.
    from prview.launcher import pick_free_port

    key = f"{owner}/{repo}"
    with _serves_lock:
        existing = _serves.get(key)
        if existing is not None and existing._proc is not None \
                and existing._proc.poll() is None \
                and existing.repo_path == repo_path:
            return existing
    # No exact reuse → we need port 7337 free. Only one serve can hold it, so
    # tear down EVERY live serve (this repo's stale one and any other repo's)
    # before starting fresh. Worktrees are left intact (serves only).
    _stop_all_serves()

    ui_port = pick_free_port()
    # Stream the long-lived child's output to a logfile, NOT an undrained PIPE:
    # a chatty server would fill the OS pipe buffer and deadlock. The logfile's
    # tail feeds stderr_tail / the "View output" affordance on serve failure.
    _SERVE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = str(_SERVE_LOG_DIR / f"{owner}-{repo}-{ui_port}.log")
    try:
        log_fh = open(logfile, "w")
        proc = subprocess.Popen(
            _serve_argv(ui_port),
            cwd=repo_path,  # serve resolves .repowise/wiki.db from cwd (the worktree)
            env=_serve_env(),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,  # own process group → teardown reaps grandchildren
        )
    except FileNotFoundError:
        raise RepowiseError("`repowise` not found", hint=_MISSING_HINT)

    entry = ServeEntry(
        pid=proc.pid,
        api_port=_API_PORT,
        ui_port=ui_port,
        url=f"http://127.0.0.1:{ui_port}/",
        started_at=time.time(),
        repo_path=repo_path,
        logfile=logfile,
        _proc=proc,
    )
    with _serves_lock:
        _serves[key] = entry
    return entry


def probe_frameability(entry: ServeEntry, timeout_s: float = 30.0) -> bool | None:
    """Poll the UI-port root until up (or timeout), then parse its headers.

    Uses stdlib urllib only (no new deps). Sets and returns entry.frameable.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(entry.url, timeout=2.0) as resp:
                entry.frameable = parse_frameability(dict(resp.headers.items()))
                return entry.frameable
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return entry.frameable  # still None — caller treats as unknown


# ===========================================================================
# Diff-mode blast radius — query the repowise API (loopback, no key) for the
# associations of the PR's changed files: direct risk, transitively-affected
# (1-hop+) files, co-change partners, and recommended reviewers.
# ===========================================================================

def _api_request(path: str, payload: dict | None = None, timeout_s: float = 30.0) -> dict:
    """GET/POST JSON against the loopback repowise API (verify_api_key is a
    no-op on 127.0.0.1, so no token needed). POST when payload is given."""
    url = f"http://127.0.0.1:{_API_PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RepowiseError(
            f"repowise API {exc.code} for {path}",
            hint="re-prepare the Repowise tab, then retry",
        )
    except (urllib.error.URLError, OSError) as exc:
        raise RepowiseError(
            f"repowise API unreachable: {exc}",
            hint="the repowise serve isn't up — open the Repowise tab to prepare first",
        )


def blast_radius(owner: str, repo: str, changed_files: list[str], max_depth: int = 3) -> dict:
    """Compute the PR's blast radius from the live repowise index.

    Resolves the served repo by its worktree path (the /api/repos list can hold
    other repos from earlier prepares) and POSTs the changed files.
    """
    serve = get_serve(owner, repo)
    if serve is None or serve._proc is None or serve._proc.poll() is not None:
        raise RepowiseError(
            "repowise serve is not running",
            hint="open the Repowise tab to prepare this PR first",
        )
    repos = _api_request("/api/repos")
    repo_id = next(
        (r["id"] for r in repos if r.get("local_path") == serve.repo_path),
        repos[0]["id"] if repos else None,
    )
    if not repo_id:
        raise RepowiseError("repowise has not indexed this repo yet",
                            hint="re-prepare the Repowise tab")
    return _api_request(
        f"/api/repos/{repo_id}/blast-radius",
        {"changed_files": changed_files, "max_depth": max_depth},
    )


# Common coverage-report locations, searched in the user's main clone (where the
# suite runs and deps live) first, then the PR worktree.
_COVERAGE_NAMES = (
    "coverage.lcov", "lcov.info", "coverage/lcov.info", "coverage/coverage.lcov",
    "coverage.xml", "cobertura.xml", "coverage/cobertura.xml",
    "clover.xml", "coverage/clover.xml",
)


def _detect_coverage(*roots: str | None) -> str | None:
    for root in roots:
        if not root:
            continue
        for name in _COVERAGE_NAMES:
            p = Path(root) / name
            if p.is_file():
                return str(p)
    return None


def ingest_coverage(owner: str, repo: str, coverage_path: str | None = None) -> dict:
    """Ingest a coverage report (LCOV/Cobertura/Clover) into the served index so
    the dashboard's coverage / risk×coverage panels populate.

    The PR worktree is a bare checkout (no deps), so the report is generated in
    the user's main clone; we ingest it against the worktree's index. An explicit
    path wins; otherwise we auto-detect common report names in the main clone
    then the worktree.
    """
    serve = get_serve(owner, repo)
    if serve is None or serve._proc is None or serve._proc.poll() is not None:
        raise RepowiseError(
            "repowise serve is not running",
            hint="open the Repowise tab to prepare this PR first",
        )
    worktree = serve.repo_path
    if coverage_path:
        path = str(Path(coverage_path).expanduser())
        if not Path(path).is_file():
            raise RepowiseError(f"coverage report not found: {coverage_path}",
                                hint="check the path, or leave it blank to auto-detect")
    else:
        path = _detect_coverage(resolve_repo_path(owner, repo), worktree)
        if not path:
            raise RepowiseError(
                "no coverage report found",
                hint="generate one (e.g. pytest --cov --cov-report=lcov) or pass a path",
            )
    result = _run(["repowise", "health", "--coverage", path,
                   "--no-workspace", worktree], cwd=worktree)
    if result.returncode != 0:
        raise RepowiseError(
            f"coverage ingest failed: {result.stderr.strip()[:200]}",
            hint="confirm the report format (lcov / cobertura / clover)",
        )
    m = re.search(r"Ingested (\d+) files", (result.stdout or "") + (result.stderr or ""))
    return {"ok": True, "files": int(m.group(1)) if m else 0, "path": path}


def stop_serve(owner: str, repo: str) -> bool:
    """Terminate a repo's serve child and drop its registry entry."""
    key = f"{owner}/{repo}"
    with _serves_lock:
        entry = _serves.pop(key, None)
    if entry is None or entry._proc is None:
        return False
    _terminate_proc(entry._proc)
    return True


def _stop_all_serves():
    """Terminate every serve child and clear the registry (serves only — leaves
    worktrees intact). Used to free port 7337 before a new serve starts."""
    with _serves_lock:
        entries = list(_serves.values())
        _serves.clear()
    for entry in entries:
        if entry._proc is not None:
            _terminate_proc(entry._proc)


def stop_all():
    """Terminate all serve children, prune worktrees, clear registries (shutdown)."""
    _stop_all_serves()
    remove_all_worktrees()


# ===========================================================================
# 2.3 Prepare job orchestrator (job-id + daemon thread; mirrors jobs.py)
# ===========================================================================
# jobs.py runs ONE-SHOT claude children; this orchestrates the MULTI-STEP
# prepare sequence (resolve → checkout → index → serve → probe → open) on a
# background thread, exposing the per-step PrepareSteps snapshot for polling.
# The frameability probe blocks (≤30s), so it runs HERE on the thread — never
# in an async route. All orchestration calls the module-level edge functions
# by name so they can be stubbed in tests without spawning a subprocess.


@dataclass
class PrepareJob:
    id: str
    owner: str
    repo: str
    number: int
    steps: PrepareSteps
    status: str = "running"  # running | done | error | cancelled
    started_at: float = 0.0
    dashboard_url: str | None = None
    serve_port: int | None = None
    frameable: bool | None = None
    stderr_tail: str | None = None
    _cancelled: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


_prepares: dict[str, PrepareJob] = {}


def _prepare_snapshot(job: PrepareJob) -> dict:
    steps = job.steps.snapshot()
    return {
        "status": job.status,
        "steps": steps["steps"],
        "elapsed": time.time() - job.started_at,
        "dashboard_url": job.dashboard_url,
        "serve_port": job.serve_port,
        "frameable": job.frameable,
        "error": steps["error"],
        "error_step": steps["error_step"],
        "error_hint": steps["error_hint"],
        "stderr_tail": job.stderr_tail,
    }


def _cancelled(job: PrepareJob) -> bool:
    with job._lock:
        return job._cancelled


def _run_prepare(job: PrepareJob):
    steps = job.steps
    current = "resolve_path"  # the step in flight, for exception attribution
    try:
        steps.start(current)
        repo_path = resolve_repo_path(job.owner, job.repo)
        if not repo_path:
            steps.fail(current, error="repo path not set",
                       hint="POST /repowise/repo-path first")
            job.status = "error"
            return
        steps.done(current, detail=repo_path)
        if _cancelled(job):
            job.status = "cancelled"
            return

        current = "checkout"  # isolated worktree — never touches the dirty tree
        steps.start(current)
        worktree_path, head = prepare_pr_worktree(repo_path, job.number)
        steps.done(current, detail=f"@ {head}" if head else "")
        if _cancelled(job):
            job.status = "cancelled"
            return

        current = "index"  # skip when wiki.db present (index the worktree)
        steps.start(current)
        skipped = ensure_indexed(worktree_path)
        if skipped:
            steps.skip(current, detail="already indexed")
        else:
            steps.done(current, detail="indexed")
        if _cancelled(job):
            job.status = "cancelled"
            return

        current = "serve"  # lazy, reused; long-lived child (serves the worktree)
        steps.start(current, detail="allocating port…")
        entry = ensure_serve(job.owner, job.repo, worktree_path)
        job.serve_port = entry.ui_port
        job.dashboard_url = entry.url
        steps.done(current, detail=f":{entry.ui_port}")
        if _cancelled(job):
            job.status = "cancelled"
            return

        current = "open"  # probe frameability (blocks ≤30s) then surface URL
        steps.start(current)
        job.frameable = probe_frameability(entry)
        # If the serve child died during/after the probe, the dashboard URL is
        # dead — surface it as a serve failure (with the log tail) rather than a
        # green "done" that embeds a broken iframe.
        if entry._proc is not None and entry._proc.poll() is not None:
            job.stderr_tail = _read_tail(entry.logfile)
            steps.fail("serve", error="repowise serve exited before the dashboard came up",
                       hint="see View output")
            job.status = "error"
            return
        steps.done(current, detail=entry.url)
        job.status = "done"
    except RepowiseError as exc:
        if current == "serve":
            entry = get_serve(job.owner, job.repo)
            if entry is not None:
                job.stderr_tail = _read_tail(entry.logfile)
        steps.fail(current, error=exc.message, hint=exc.hint)
        job.status = "error"
    except Exception as exc:  # never leak a stack to the poller
        steps.fail(current, error=str(exc))
        job.status = "error"


def start_prepare(owner: str, repo: str, number: int) -> str:
    """Mint a prepare job, run the orchestrator on a daemon thread, return id."""
    job = PrepareJob(
        id=str(uuid.uuid4()),
        owner=owner, repo=repo, number=number,
        steps=PrepareSteps(),
        started_at=time.time(),
    )
    _prepares[job.id] = job
    threading.Thread(target=_run_prepare, args=(job,), daemon=True).start()
    return job.id


def get_prepare(job_id: str) -> dict | None:
    """Snapshot a prepare job for GET /repowise/prepare/{id}, or None if unknown."""
    job = _prepares.get(job_id)
    return _prepare_snapshot(job) if job is not None else None


def cancel_prepare(job_id: str) -> bool:
    """Best-effort cancel: flip the flag the orchestrator checks between steps."""
    job = _prepares.get(job_id)
    if job is None:
        return False
    with job._lock:
        if job.status != "running":
            return False
        job._cancelled = True
    return True
