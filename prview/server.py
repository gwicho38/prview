"""FastAPI app: the HTTP layer over the G1-G3 functional core + CLI wrappers.

Concurrency contract (audit fix 4.5): every route that shells out to `gh` is a
**sync `def`** handler. FastAPI runs sync handlers in a threadpool, so a
blocking subprocess.run never stalls the event loop. The AI submit/poll/cancel
routes are `async` because they only touch the in-memory job registry (the
300s claude work already runs on its own daemon thread inside prview.jobs).
NEVER call a blocking subprocess from an `async def` here.

Caching: POST /pr fetches the diff once and caches PRInfo + parsed chunks keyed
by pr_key. GET …/file and the AI endpoints read from that cache; a miss means
the server restarted mid-session, surfaced as a structured 409 so the client
re-issues POST /pr.

Persistence: every mutating route funnels through state_store.mutate_state,
holding the per-PR lock for the whole read-modify-write before returning.

Errors: GhError / parse errors / cache misses map to structured {error, hint?}
JSON via HTTPException(detail=...) — never a leaked stack trace.
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import prview.core as core
import prview.gh as gh
import prview.jobs as jobs
import prview.state_store as state_store
from prview.api_models import (
    AskRequest,
    CommentRequest,
    FileDetail,
    FileListItem,
    FileTarget,
    FlagRequest,
    FlagResponse,
    JobIdResponse,
    JobStatusResponse,
    OkResponse,
    PRInfoModel,
    PRRefRequest,
    PRResponse,
    PRTarget,
    ResumableRow,
    ReviewStateModel,
    SubmitRequest,
    ViewedResponse,
)
from prview.cache import CACHE_MISS, PRCache
from prview.security import SecurityMiddleware

app = FastAPI(title="prview")
cache = PRCache()

app.add_middleware(SecurityMiddleware)


def set_session_token(token: str) -> None:
    """Inject the per-session token the launcher (G6) minted at startup."""
    app.state.session_token = token


@app.exception_handler(gh.GhError)
async def _gh_error_handler(request: Request, exc: gh.GhError):
    return JSONResponse({"error": exc.message, "hint": exc.hint or None}, status_code=400)


@app.exception_handler(HTTPException)
async def _http_error_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    body = detail if isinstance(detail, dict) and "error" in detail else {"error": detail}
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        {"error": "Invalid request", "hint": str(exc.errors()[:1])},
        status_code=422,
    )


def _err(status: int, error: str, hint: str | None = None) -> HTTPException:
    detail = {"error": error}
    if hint:
        detail["hint"] = hint
    return HTTPException(status_code=status, detail=detail)


def _sorted_files(files: list[core.FileDiff]) -> list[core.FileDiff]:
    return sorted(files, key=lambda f: f.additions + f.deletions, reverse=True)


def _load_pr(owner: str, repo: str, number: int) -> PRResponse:
    pr = gh.fetch_pr_info(owner, repo, number)
    raw = gh.fetch_pr_diff(owner, repo, number)
    files = _sorted_files(core.parse_diff(raw))
    state = core.load_review_state(owner, repo, number)
    core.apply_saved_state(files, state)
    cache.set(state_store.pr_key(owner, repo, number), pr=pr, files=files)
    return PRResponse(
        pr=PRInfoModel.of(pr),
        files=[FileListItem.of(f) for f in files],
        state=ReviewStateModel.of(state),
    )


def _cached(owner: str, repo: str, number: int) -> dict:
    entry = cache.get(state_store.pr_key(owner, repo, number))
    if entry is CACHE_MISS:
        raise _err(409, "PR not loaded (cache miss) — reload the PR",
                   "re-issue POST /pr for this reference")
    return entry


def _cached_file(owner: str, repo: str, number: int, path: str) -> tuple[core.PRInfo, core.FileDiff]:
    entry = _cached(owner, repo, number)
    for fd in entry["files"]:
        if fd.filename == path:
            return entry["pr"], fd
    raise _err(404, f"File not in PR: {path}")


# --- PR load (sync: shells gh) ------------------------------------------------

@app.post("/pr", response_model=PRResponse)
def post_pr(req: PRRefRequest) -> PRResponse:
    try:
        owner, repo, number = core.parse_pr_ref(req.ref)
    except ValueError as exc:
        raise _err(400, str(exc), "use owner/repo#123 or a GitHub PR URL")
    return _load_pr(owner, repo, number)


@app.get("/pr/{owner}/{repo}/{n}", response_model=PRResponse)
def get_pr(owner: str, repo: str, n: int) -> PRResponse:
    return _load_pr(owner, repo, n)


@app.get("/pr/{owner}/{repo}/{n}/file", response_model=FileDetail)
def get_file(owner: str, repo: str, n: int, path: str) -> FileDetail:
    _, fd = _cached_file(owner, repo, n, path)
    return FileDetail(
        filename=fd.filename,
        additions=fd.additions,
        deletions=fd.deletions,
        flagged=fd.flagged,
        flag_note=fd.flag_note,
        viewed=fd.viewed,
        diff_text=fd.diff_text,
    )


# --- AI jobs (async: only touches in-memory registry) -------------------------

@app.post("/ai/summary", response_model=JobIdResponse)
async def ai_summary(req: FileTarget) -> JobIdResponse:
    pr, fd = _cached_file(req.owner, req.repo, req.number, req.path)
    return JobIdResponse(job_id=jobs.start_summary(pr, fd))


@app.post("/ai/explain", response_model=JobIdResponse)
async def ai_explain(req: FileTarget) -> JobIdResponse:
    pr, fd = _cached_file(req.owner, req.repo, req.number, req.path)
    return JobIdResponse(job_id=jobs.start_explain(pr, fd))


@app.post("/ai/ask", response_model=JobIdResponse)
async def ai_ask(req: AskRequest) -> JobIdResponse:
    pr, fd = _cached_file(req.owner, req.repo, req.number, req.path)
    return JobIdResponse(job_id=jobs.start_ask(pr, fd, req.question))


@app.get("/job/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str) -> JobStatusResponse:
    snap = jobs.get_job(job_id)
    if snap is None:
        raise _err(404, f"No such job: {job_id}")
    return JobStatusResponse(**snap)


@app.post("/job/{job_id}/cancel", response_model=OkResponse)
async def cancel_job(job_id: str) -> OkResponse:
    return OkResponse(ok=jobs.cancel_job(job_id))


# --- Mutating routes (sync: shell gh + persist under per-PR lock) -------------

@app.post("/file/viewed", response_model=ViewedResponse)
def file_viewed(req: FileTarget) -> ViewedResponse:
    remote_ok = gh.mark_file_viewed(req.owner, req.repo, req.number, req.path)

    def mutate(state: dict) -> dict:
        viewed = set(state.get("viewed", []))
        viewed.add(req.path)
        state["viewed"] = sorted(viewed)
        return state

    state_store.mutate_state(req.owner, req.repo, req.number, mutate)
    return ViewedResponse(viewed=True, remote_ok=remote_ok)


@app.post("/file/flag", response_model=FlagResponse)
def file_flag(req: FlagRequest) -> FlagResponse:
    def mutate(state: dict) -> dict:
        flagged = dict(state.get("flagged", {}))
        if req.flagged:
            flagged[req.path] = req.note
        else:
            flagged.pop(req.path, None)
        state["flagged"] = flagged
        return state

    state_store.mutate_state(req.owner, req.repo, req.number, mutate)
    return FlagResponse(flagged=req.flagged, note=req.note if req.flagged else "")


@app.post("/comment", response_model=OkResponse)
def post_comment(req: CommentRequest) -> OkResponse:
    ok = gh.post_pr_comment(req.owner, req.repo, req.number, req.path, req.text)
    if ok:
        def mutate(state: dict) -> dict:
            state["comments"] = int(state.get("comments", 0)) + 1
            return state

        state_store.mutate_state(req.owner, req.repo, req.number, mutate)
    return OkResponse(ok=ok)


def _flagged_body(state: dict) -> str:
    """Flagged-files review body (source lines 594-600), reused verbatim."""
    flagged = state.get("flagged", {})
    if not flagged:
        return ""
    body = "**Flagged files:**\n"
    for filename in flagged:
        body += f"- `{filename}`"
        note = flagged[filename]
        if note:
            body += f" — {note}"
        body += "\n"
    return body


@app.post("/review/submit", response_model=OkResponse)
def submit_review(req: SubmitRequest) -> OkResponse:
    state = core.load_review_state(req.owner, req.repo, req.number)
    body = req.body if req.body is not None else _flagged_body(state)
    ok, err = gh.submit_review(req.owner, req.repo, req.number, req.event, body)
    if ok:
        def mutate(s: dict) -> dict:
            s["submitted"] = True
            return s

        state_store.mutate_state(req.owner, req.repo, req.number, mutate)
        return OkResponse(ok=True)
    return OkResponse(ok=False, error=err or "review submission failed")


# --- Read routes (sync: read state from disk) ---------------------------------

@app.get("/state/{owner}/{repo}/{n}", response_model=ReviewStateModel)
def get_state(owner: str, repo: str, n: int) -> ReviewStateModel:
    return ReviewStateModel.of(core.load_review_state(owner, repo, n))


@app.get("/reviews", response_model=list[ResumableRow])
def list_reviews() -> list[ResumableRow]:
    return [ResumableRow(**row) for row in state_store.list_resumable()]


# --- Static assets ------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
_INDEX_HTML = _STATIC_DIR / "index.html"


@app.get("/")
@app.get("/index.html")
def index() -> FileResponse:
    """Serve the SPA shell at the launch URL (G6 opens /?token=…)."""
    return FileResponse(str(_INDEX_HTML))


app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
