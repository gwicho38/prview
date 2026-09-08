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
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import prview.behaviors as behaviors
import prview.core as core
import prview.gh as gh
import prview.jobs as jobs
import prview.order as order
import prview.repowise as repowise
import prview.reviews as reviews
import prview.skills as skills
import prview.state_store as state_store
from prview.api_models import (
    ArchiveRequest,
    AskRequest,
    ClearRunRequest,
    DraftEditRequest,
    BehaviorCommentRequest,
    BehaviorCommentResponse,
    BehaviorModel,
    BehaviorsResponse,
    CommentRequest,
    ExplainSelectionRequest,
    FileDetail,
    FullFileModel,
    FileListItem,
    FileTarget,
    FlagRequest,
    FlagResponse,
    JobIdResponse,
    JobStatusResponse,
    OkResponse,
    PrepareRequest,
    PrepareStandaloneRequest,
    BlastRadiusModel,
    BlastRadiusRequest,
    CoverageIngestModel,
    CoverageIngestRequest,
    DocgenRequest,
    DocgenSnapshot,
    OllamaModelsModel,
    OverviewModel,
    PrepareSnapshot,
    PRInfoModel,
    PRRefRequest,
    PRResponse,
    PromptRequest,
    PromptResponse,
    PRTarget,
    RepoPathRequest,
    RepoPathResponse,
    RepoRef,
    RepowiseStatusResponse,
    ResumableRow,
    ReviewStateModel,
    SubmitRequest,
    SubmitResponse,
    ViewedResponse,
    RunIdResponse,
    RunReviewRequest,
    RunSnapshot,
    SkillRow,
    SkillsResponse,
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


@app.exception_handler(repowise.RepowiseError)
async def _repowise_error_handler(request: Request, exc: repowise.RepowiseError):
    # Missing CLI / failed subprocess → structured 400 hint, never a leaked 500.
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
    """Largest-first. core.build_overview_prompt depends on this order to pick
    which whole diffs reach the AI summary, so it is the cache order; display
    order is the client's choice from the `orders` map."""
    return sorted(files, key=lambda f: f.additions + f.deletions, reverse=True)


def _load_pr(owner: str, repo: str, number: int) -> PRResponse:
    pr = gh.fetch_pr_info(owner, repo, number)
    raw = gh.fetch_pr_diff(owner, repo, number, base=pr.base)
    files = _sorted_files(core.parse_diff(raw))
    state = core.load_review_state(owner, repo, number)
    core.apply_saved_state(files, state)
    cache.set(state_store.pr_key(owner, repo, number), pr=pr, files=files)
    return PRResponse(
        pr=PRInfoModel.of(pr),
        files=[FileListItem.of(f) for f in files],
        state=ReviewStateModel.of(state),
        orders=order.orders_map(files),
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


# Grouping costs N+1 gh calls, so it's computed on first use and kept until the head SHA moves.
_BEHAVIOR_CACHE: "OrderedDict[tuple[str, str, int, str], tuple[list, bool]]" = OrderedDict()
_BEHAVIOR_CACHE_CAP = 32
_BEHAVIOR_CACHE_LOCK = threading.Lock()
_BEHAVIOR_DERIVE_LOCKS: dict[tuple[str, str, int, str], threading.Lock] = {}


def _behavior_cache_get(key):
    with _BEHAVIOR_CACHE_LOCK:
        hit = _BEHAVIOR_CACHE.get(key)
        if hit is not None:
            _BEHAVIOR_CACHE.move_to_end(key)
        return hit


def _behavior_cache_put(key, value) -> None:
    with _BEHAVIOR_CACHE_LOCK:
        _BEHAVIOR_CACHE[key] = value
        _BEHAVIOR_CACHE.move_to_end(key)
        while len(_BEHAVIOR_CACHE) > _BEHAVIOR_CACHE_CAP:
            evicted, _ = _BEHAVIOR_CACHE.popitem(last=False)
            _BEHAVIOR_DERIVE_LOCKS.pop(evicted, None)


def _behavior_derive_lock(key) -> threading.Lock:
    with _BEHAVIOR_CACHE_LOCK:
        return _BEHAVIOR_DERIVE_LOCKS.setdefault(key, threading.Lock())


def _behaviors_for(owner: str, repo: str, number: int) -> tuple[list, str, bool]:
    entry = _cached(owner, repo, number)
    pr, files = entry["pr"], entry["files"]
    key = (owner, repo, number, pr.head_sha)
    hit = _behavior_cache_get(key)
    if hit is not None:
        return hit[0], pr.head_sha, hit[1]
    # One derivation per key: FastAPI runs sync endpoints on a thread pool, and
    # concurrent first-requests would otherwise each pay the N+1 gh calls.
    with _behavior_derive_lock(key):
        hit = _behavior_cache_get(key)
        if hit is not None:
            return hit[0], pr.head_sha, hit[1]
        return _derive_behaviors(owner, repo, number, pr, files, key)


def _derive_behaviors(owner, repo, number, pr, files, key) -> tuple[list, str, bool]:
    try:
        commits = gh.fetch_pr_commits(owner, repo, number)
    except gh.GhError as e:
        raise _err(409, str(e), getattr(e, "hint", None))
    groupable = behaviors.is_groupable(commits)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            listed = pool.map(
                lambda sha: (sha, gh.fetch_commit_files(owner, repo, sha)),
                [c["sha"] for c in commits if not c["is_merge"]],
            )
            files_by_sha = dict(listed)
    except gh.GhError as e:
        raise _err(409, str(e), getattr(e, "hint", None))
    derived = behaviors.behaviors_from_commits(
        commits, files_by_sha, {f.filename for f in files})
    _behavior_cache_put(key, (derived, groupable))
    return derived, pr.head_sha, groupable


@app.get("/pr/{owner}/{repo}/{n}/behaviors", response_model=BehaviorsResponse)
def get_behaviors(owner: str, repo: str, n: int) -> BehaviorsResponse:
    derived, head_sha, groupable = _behaviors_for(owner, repo, n)
    return BehaviorsResponse(
        behaviors=[BehaviorModel.of(b) for b in derived],
        head_sha=head_sha,
        groupable=groupable,
    )


@app.post("/ai/behavior-names", response_model=JobIdResponse)
def post_ai_behavior_names(req: PRTarget) -> JobIdResponse:
    derived, head_sha, groupable = _behaviors_for(req.owner, req.repo, req.number)
    entry = _cached(req.owner, req.repo, req.number)
    key = (req.owner, req.repo, req.number, head_sha)

    def apply(result: str):
        named = behaviors.apply_behavior_names(derived, result)
        if named:
            _behavior_cache_put(key, (named, groupable))

    return JobIdResponse(
        job_id=jobs.start_behavior_names(entry["pr"], derived, on_done=apply))


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


@app.get("/pr/{owner}/{repo}/{n}/file/full", response_model=FullFileModel)
def get_file_full(owner: str, repo: str, n: int, path: str) -> FullFileModel:
    # Whole-file view: the file at the PR head + the new-side line numbers this
    # PR added (so the client can highlight them). Diff lines come from the cache.
    _, fd = _cached_file(owner, repo, n, path)
    sha = gh.pr_head_sha(owner, repo, n)
    content = gh.fetch_file_at_ref(owner, repo, path, sha)
    return FullFileModel(content=content, added_lines=core.added_line_numbers(fd.diff_text))


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


@app.post("/ai/explain-selection", response_model=JobIdResponse)
async def ai_explain_selection(req: ExplainSelectionRequest) -> JobIdResponse:
    pr, fd = _cached_file(req.owner, req.repo, req.number, req.path)
    return JobIdResponse(job_id=jobs.start_explain_selection(pr, fd, req.selection))


# The browser-side model engine builds nothing itself: it asks for the very prompt
# the claude path would send, so both engines stay one implementation. It may ask for a
# smaller diff budget, because it runs in a context window a fraction of claude's; the
# prompt then states what was cut.
# Reached only from post_ai_prompt, so req is always a PromptRequest; the
# /ai/* job endpoints build their prompts through jobs.py instead.
_FILE_PROMPTS = {
    "summary": lambda pr, fd, req: core.build_summary_prompt(pr, fd, req.diff_limit),
    "explain": lambda pr, fd, req: core.build_explain_prompt(pr, fd, req.diff_limit),
    "ask": lambda pr, fd, req: core.build_ask_prompt(pr, fd, req.question, req.diff_limit),
    "explain-selection": lambda pr, fd, req: core.build_explain_selection_prompt(
        pr, fd, req.selection, req.diff_limit),
}
_PROMPT_REQUIRED = {"ask": "question", "explain-selection": "selection"}


@app.post("/ai/prompt", response_model=PromptResponse)
def post_ai_prompt(req: PromptRequest) -> PromptResponse:
    if req.kind == "overview":
        entry = _cached(req.owner, req.repo, req.number)
        return PromptResponse(prompt=core.build_overview_prompt(
            entry["pr"], entry["files"], req.diff_limit))
    build = _FILE_PROMPTS.get(req.kind)
    if build is None:
        raise _err(400, f"Unknown prompt kind: {req.kind}",
                   f"expected one of: overview, {', '.join(sorted(_FILE_PROMPTS))}")
    if not req.path:
        raise _err(400, f"Prompt kind {req.kind} needs a file path")
    missing = _PROMPT_REQUIRED.get(req.kind)
    if missing and not getattr(req, missing):
        raise _err(400, f"Prompt kind {req.kind} needs a {missing}")
    pr, fd = _cached_file(req.owner, req.repo, req.number, req.path)
    return PromptResponse(prompt=build(pr, fd, req))


@app.get("/job/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str) -> JobStatusResponse:
    snap = jobs.get_job(job_id)
    if snap is None:
        raise _err(404, f"No such job: {job_id}")
    return JobStatusResponse(**snap)


@app.post("/job/{job_id}/cancel", response_model=OkResponse)
async def cancel_job(job_id: str) -> OkResponse:
    return OkResponse(ok=jobs.cancel_job(job_id))


# --- Overview — whole-PR AI orientation, cached per head SHA -------------------

@app.get("/overview/{owner}/{repo}/{n}", response_model=OverviewModel)
def get_overview(owner: str, repo: str, n: int) -> OverviewModel:
    entry = _cached(owner, repo, n)
    stored = core.load_overview(owner, repo, n)
    if not stored:
        return OverviewModel()
    if stored.get("sha") != entry["pr"].head_sha:
        return OverviewModel(sha=stored.get("sha"), stale=True)
    return OverviewModel(markdown=stored.get("markdown"), sha=stored.get("sha"))


@app.post("/ai/overview", response_model=JobIdResponse)
async def ai_overview(req: PRTarget) -> JobIdResponse:
    entry = _cached(req.owner, req.repo, req.number)
    return JobIdResponse(
        job_id=jobs.start_overview(entry["pr"], entry["files"], entry["pr"].head_sha))


@app.post("/overview/comment", response_model=OkResponse)
def overview_comment(req: PRTarget) -> OkResponse:
    stored = core.load_overview(req.owner, req.repo, req.number)
    if not stored.get("markdown"):
        raise _err(404, "no overview generated for this PR",
                   "generate the overview first")
    return OkResponse(ok=gh.post_pr_comment_file(
        req.owner, req.repo, req.number, stored["markdown"]))


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
    # Line-anchored → a GitHub review comment on path@line (range when
    # start_line is set); otherwise a general, file-level PR comment.
    if req.line is not None:
        commit_id = gh.pr_head_sha(req.owner, req.repo, req.number)
        ok = gh.post_pr_review_comment(
            req.owner, req.repo, req.number, req.path, req.text, commit_id,
            line=req.line, side=req.side, start_line=req.start_line,
        )
    else:
        ok = gh.post_pr_comment(req.owner, req.repo, req.number, req.path, req.text)
    if ok:
        entry = {"text": req.text, "line": req.line, "start_line": req.start_line}

        def mutate(state: dict) -> dict:
            state["comments"] = int(state.get("comments", 0)) + 1
            threads = dict(state.get("comment_threads", {}))
            threads[req.path] = [*threads.get(req.path, []), entry]
            state["comment_threads"] = threads
            return state

        state_store.mutate_state(req.owner, req.repo, req.number, mutate)
    return OkResponse(ok=ok)


def _behavior_target(fd_by_name: dict, filenames) -> tuple[object, tuple[int, int, str]] | None:
    """Highest-story-tier file with an anchorable hunk, churn-descending on ties."""
    ranked = sorted(
        (fd_by_name[n] for n in filenames if n in fd_by_name),
        key=lambda fd: (order.story_tier(fd.filename), -(fd.additions + fd.deletions)),
    )
    for fd in ranked:
        span = core.first_hunk_range(fd.diff_text)
        if span:
            return fd, span
    return None


@app.post("/behaviors/comment", response_model=BehaviorCommentResponse)
def post_behavior_comment(req: BehaviorCommentRequest) -> BehaviorCommentResponse:
    derived, _, _ = _behaviors_for(req.owner, req.repo, req.number)
    match = next((b for b in derived if b.id == req.behavior_id), None)
    if match is None:
        raise _err(404, f"Behavior not in PR: {req.behavior_id}")

    entry = _cached(req.owner, req.repo, req.number)
    fd_by_name = {fd.filename: fd for fd in entry["files"]}
    body = (
        f"**On behavior: {match.title}**\n"
        f"({', '.join(match.filenames)})\n\n"
        f"{req.text}"
    )

    target = _behavior_target(fd_by_name, match.filenames)
    if target is None:
        ok = gh.post_pr_comment_file(req.owner, req.repo, req.number, body)
        anchored, path, line, start_line = False, None, None, None
    else:
        fd, (start, end, side) = target
        commit_id = gh.pr_head_sha(req.owner, req.repo, req.number)
        ok = gh.post_pr_review_comment(
            req.owner, req.repo, req.number, fd.filename, body, commit_id,
            line=end, side=side, start_line=start if start < end else None,
            start_side=side,
        )
        anchored, path, line = ok, (fd.filename if ok else None), (end if ok else None)
        start_line = start if start < end else None

    if ok:
        def mutate(state: dict) -> dict:
            state["comments"] = int(state.get("comments", 0)) + 1
            if anchored and path:
                thread_entry = {"text": req.text, "line": line, "start_line": start_line}
                threads = dict(state.get("comment_threads", {}))
                threads[path] = [*threads.get(path, []), thread_entry]
                state["comment_threads"] = threads
            return state

        state_store.mutate_state(req.owner, req.repo, req.number, mutate)
    return BehaviorCommentResponse(ok=ok, anchored=anchored, path=path, line=line)


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


def _staged_payload(state: dict) -> tuple[list[dict], list[str]]:
    """Anchored staged drafts as gh review comments, plus the unanchorable ones
    as body paragraphs. A file-level draft has no legal anchor, so it joins the
    body rather than being dropped."""
    comments, notes = [], []
    for path, entries in state.get("comment_threads", {}).items():
        for e in entries:
            if not (isinstance(e, dict) and e.get("staged")):
                continue
            if e.get("line") is None:
                notes.append(f"**{path}** — {e['text']}")
                continue
            comments.append({
                "path": path, "body": e["text"], "line": e["line"],
                "side": "RIGHT", "start_line": e.get("start_line"),
            })
    notes.extend(n["text"] for n in state.get("review_notes", []))
    return comments, notes


@app.post("/review/submit", response_model=SubmitResponse)
def submit_review(req: SubmitRequest) -> SubmitResponse:
    state = core.load_review_state(req.owner, req.repo, req.number)
    body = req.body if req.body is not None else _flagged_body(state)
    comments, notes = _staged_payload(state)
    if notes:
        body = "\n\n".join([body or "", *notes]).strip()

    if comments:
        ok, err = gh.submit_review_with_comments(
            req.owner, req.repo, req.number, req.event, body, comments)
    else:
        # No inline comments to batch, so keep the proven `gh pr review` path —
        # the reviews API rejects a COMMENT review carrying neither.
        ok, err = gh.submit_review(req.owner, req.repo, req.number, req.event, body)
    if not ok:
        return SubmitResponse(ok=False, error=err or "review submission failed")

    def mutate(s: dict) -> dict:
        threads, posted = {}, 0
        for path, entries in s.get("comment_threads", {}).items():
            out = []
            for e in entries:
                if isinstance(e, dict) and e.get("staged"):
                    posted += 1
                    out.append({**e, "staged": False})
                else:
                    out.append(e)
            threads[path] = out
        s["comment_threads"] = threads
        s["comments"] = int(s.get("comments", 0)) + posted
        s["review_notes"] = []
        s["submitted"] = True
        return s

    state_store.mutate_state(req.owner, req.repo, req.number, mutate)
    return SubmitResponse(ok=True,
                          url=gh.latest_review_url(req.owner, req.repo, req.number))


@app.post("/review/archive", response_model=OkResponse)
def archive_review(req: ArchiveRequest) -> OkResponse:
    def mutate(state: dict) -> dict:
        state["archived"] = req.archived
        return state

    state_store.mutate_state(req.owner, req.repo, req.number, mutate)
    return OkResponse(ok=True)


# --- Read routes (sync: read state from disk) ---------------------------------

@app.get("/state/{owner}/{repo}/{n}", response_model=ReviewStateModel)
def get_state(owner: str, repo: str, n: int) -> ReviewStateModel:
    return ReviewStateModel.of(core.load_review_state(owner, repo, n))


@app.get("/reviews", response_model=list[ResumableRow])
def list_reviews(include_archived: bool = False) -> list[ResumableRow]:
    return [ResumableRow(**row) for row in state_store.list_resumable(include_archived)]


# --- Repowise (G2) -------------------------------------------------------------
# Concurrency contract: routes that shell out to git/gh/repowise are sync `def`
# (threadpool). The prepare submit/poll/cancel routes are `async` — they only
# touch the in-memory prepare registry; the multi-step work runs on a daemon
# thread inside prview.repowise (same model as the AI /job routes).

@app.get("/repowise/status", response_model=RepowiseStatusResponse)
def repowise_status(owner: str, repo: str, number: int) -> RepowiseStatusResponse:
    cli_present, cli_hint = repowise.cli_present()
    node_ok, node_hint = repowise.node_present()
    repo_path = repowise.resolve_repo_path(owner, repo)
    indexed = bool(repo_path) and repowise.is_repo_indexed(repo_path)
    entry = repowise.get_serve(owner, repo)
    return RepowiseStatusResponse(
        cli_present=cli_present,
        cli_hint=cli_hint,
        node_ok=node_ok,
        node_hint=node_hint,
        repo_path_known=repo_path is not None,
        repo_path=repo_path,
        indexed=indexed,
        serve_running=entry is not None,
        serve_url=entry.url if entry else None,
        serve_port=entry.ui_port if entry else None,
        frameable=entry.frameable if entry else None,
    )


@app.post("/repowise/repo-path", response_model=RepoPathResponse)
def repowise_repo_path(req: RepoPathRequest) -> RepoPathResponse:
    result = repowise.validate_and_persist_path(req.owner, req.repo, req.path)
    if result.get("ok"):
        return RepoPathResponse(ok=True, path=result["path"])
    raise _err(400, result.get("error", "invalid path"), result.get("hint"))


@app.post("/repowise/prepare", response_model=JobIdResponse)
async def repowise_prepare(req: PrepareRequest) -> JobIdResponse:
    if repowise.resolve_repo_path(req.owner, req.repo) is None:
        raise _err(409, "repo path not set", "POST /repowise/repo-path first")
    return JobIdResponse(job_id=repowise.start_prepare(req.owner, req.repo, req.number))


@app.post("/repowise/prepare-standalone", response_model=JobIdResponse)
def repowise_prepare_standalone(req: PrepareStandaloneRequest) -> JobIdResponse:
    return JobIdResponse(job_id=repowise.start_prepare_standalone(req.path))


@app.get("/repowise/prepare/{job_id}", response_model=PrepareSnapshot)
async def repowise_prepare_status(job_id: str) -> PrepareSnapshot:
    snap = repowise.get_prepare(job_id)
    if snap is None:
        raise _err(404, f"No such prepare job: {job_id}")
    return PrepareSnapshot(**snap)


@app.post("/repowise/prepare/{job_id}/cancel", response_model=OkResponse)
async def repowise_prepare_cancel(job_id: str) -> OkResponse:
    return OkResponse(ok=repowise.cancel_prepare(job_id))


@app.post("/repowise/stop", response_model=OkResponse)
def repowise_stop(req: RepoRef) -> OkResponse:
    return OkResponse(ok=repowise.stop_serve(req.owner, req.repo))


@app.post("/repowise/blast-radius", response_model=BlastRadiusModel)
def repowise_blast_radius(req: BlastRadiusRequest) -> BlastRadiusModel:
    # Diff mode: associations among the PR's changed files from the live index.
    data = repowise.blast_radius(req.owner, req.repo, req.changed_files, req.max_depth)
    return BlastRadiusModel(**data)


@app.post("/repowise/coverage", response_model=CoverageIngestModel)
def repowise_coverage(req: CoverageIngestRequest) -> CoverageIngestModel:
    # Ingest a coverage report so the dashboard's coverage panels populate.
    data = repowise.ingest_coverage(req.owner, req.repo, req.path)
    return CoverageIngestModel(**data)


@app.get("/repowise/ollama-models", response_model=OllamaModelsModel)
def repowise_ollama_models() -> OllamaModelsModel:
    return OllamaModelsModel(models=repowise.list_ollama_models())


@app.post("/repowise/docs/generate", response_model=JobIdResponse)
def repowise_docs_generate(req: DocgenRequest) -> JobIdResponse:
    # Generate the docs/wiki panel with a local ollama model (background job).
    return JobIdResponse(job_id=repowise.start_docgen(req.owner, req.repo, req.model))


@app.get("/repowise/docs/generate/{job_id}", response_model=DocgenSnapshot)
def repowise_docs_generate_status(job_id: str) -> DocgenSnapshot:
    snap = repowise.get_docgen(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail={"error": "unknown docgen job"})
    return DocgenSnapshot(**snap)


@app.post("/repowise/docs/generate/{job_id}/cancel", response_model=OkResponse)
def repowise_docs_generate_cancel(job_id: str) -> OkResponse:
    return OkResponse(ok=repowise.cancel_docgen(job_id))


# --- Local AI review ----------------------------------------------------------

@app.get("/reviews/skills/{owner}/{repo}/{n}", response_model=SkillsResponse)
def review_skills(owner: str, repo: str, n: int) -> SkillsResponse:
    files = [fd.filename for fd in _cached(owner, repo, n)["files"]]
    rows = []
    for skill in skills.discover_skills(skills.default_roots()):
        ok, label = skills.fits(skill, files, repo)
        rows.append(SkillRow(name=skill.name, description=skill.description,
                             fits=ok, label=label))
    rows.sort(key=lambda r: (not r.fits, r.name))
    return SkillsResponse(skills=rows)


@app.post("/reviews/run", response_model=RunIdResponse)
def run_review(req: RunReviewRequest) -> RunIdResponse:
    if reviews.active_run_for(req.owner, req.repo, req.number):
        raise _err(409, "a review is already running for this PR",
                   "cancel it before starting another")
    files = _cached(req.owner, req.repo, req.number)["files"]
    added = {fd.filename: core.added_line_numbers(fd.diff_text) for fd in files}
    scope_paths = req.paths or [fd.filename for fd in files]
    try:
        run_id = reviews.start_review(
            req.owner, req.repo, req.number, req.skill, req.scope,
            scope_paths, added,
        )
    except RuntimeError as exc:
        raise _err(400, str(exc), "set a local repo path for this repository")
    return RunIdResponse(run_id=run_id)


@app.get("/reviews/run/{run_id}", response_model=RunSnapshot)
def review_run(run_id: str) -> RunSnapshot:
    snap = reviews.get_run(run_id)
    if snap is None:
        raise _err(404, "unknown review run")
    return RunSnapshot(**snap)


@app.post("/reviews/run/{run_id}/cancel", response_model=OkResponse)
def cancel_review_run(run_id: str) -> OkResponse:
    return OkResponse(ok=reviews.cancel_run(run_id))


@app.post("/reviews/run/clear", response_model=OkResponse)
def clear_review_run(req: ClearRunRequest) -> OkResponse:
    reviews.clear_run(req.owner, req.repo, req.number, req.run_id)
    return OkResponse(ok=True)


def _edit_draft(req: DraftEditRequest, new_text: str | None) -> bool:
    """Rewrite (new_text) or remove (None) one staged draft.

    Only records with staged: true are reachable, so a posted comment can never
    be rewritten through this path.
    """
    hit = False

    def mutate(state: dict) -> dict:
        nonlocal hit
        threads = {}
        for path, entries in state.get("comment_threads", {}).items():
            kept = []
            for e in entries:
                if not (isinstance(e, dict) and e.get("staged")
                        and e.get("id") == req.id):
                    kept.append(e)
                    continue
                hit = True
                if new_text is not None:
                    kept.append({**e, "text": new_text})
            if kept:
                threads[path] = kept
        notes = []
        for note in state.get("review_notes", []):
            if note.get("id") == req.id:
                hit = True
                if new_text is not None:
                    notes.append({**note, "text": new_text})
                continue
            notes.append(note)
        state["comment_threads"] = threads
        state["review_notes"] = notes
        return state

    state_store.mutate_state(req.owner, req.repo, req.number, mutate)
    return hit


@app.put("/reviews/draft", response_model=OkResponse)
def edit_draft(req: DraftEditRequest) -> OkResponse:
    if not _edit_draft(req, req.text or ""):
        raise _err(404, "no staged draft with that id")
    return OkResponse(ok=True)


@app.delete("/reviews/draft", response_model=OkResponse)
def dismiss_draft(req: DraftEditRequest) -> OkResponse:
    if not _edit_draft(req, None):
        raise _err(404, "no staged draft with that id")
    return OkResponse(ok=True)


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
