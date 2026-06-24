"""Pydantic request/response models mirroring the API surface table.

Owner/repo path+body strings are validated against the same character classes
as core.parse_pr_ref (owner: no `/`; repo: no `/` or `#`); `number` is a
positive int. Validation happens here so route handlers stay thin and never
shell-interpolate unvalidated input.
"""
from pydantic import BaseModel, Field, field_validator

from prview.core import FileDiff, PRInfo


_OWNER_BAD = set("/")
_REPO_BAD = set("/#")

# Canonical review events (lowercase). The CLI source maps exactly these to
# `gh pr review --approve/--request-changes/--comment`. Accepting unknown values
# would silently downgrade to a plain comment, so we normalize + reject here.
_VALID_EVENTS = {"approve", "request_changes", "comment"}


def _check_owner(v: str) -> str:
    v = v.strip()
    # Reject leading '-' so an owner can never be read as a gh option (argv is
    # fixed, so this is defense-in-depth, not the primary guard).
    if not v or v.startswith("-") or any(c in _OWNER_BAD for c in v):
        raise ValueError("invalid owner")
    return v


def _check_repo(v: str) -> str:
    v = v.strip()
    if not v or v.startswith("-") or any(c in _REPO_BAD for c in v):
        raise ValueError("invalid repo")
    return v


def _check_path(v: str) -> str:
    if not v or v.startswith("-") or "\x00" in v:
        raise ValueError("invalid path")
    return v


def _check_event(v: str) -> str:
    v = v.strip().lower().replace("-", "_")
    if v not in _VALID_EVENTS:
        raise ValueError(f"invalid event (expected one of {sorted(_VALID_EVENTS)})")
    return v


class PRRefRequest(BaseModel):
    ref: str


class PRTarget(BaseModel):
    owner: str
    repo: str
    number: int = Field(gt=0)

    @field_validator("owner")
    @classmethod
    def _v_owner(cls, v: str) -> str:
        return _check_owner(v)

    @field_validator("repo")
    @classmethod
    def _v_repo(cls, v: str) -> str:
        return _check_repo(v)


class FileTarget(PRTarget):
    path: str

    @field_validator("path")
    @classmethod
    def _v_path(cls, v: str) -> str:
        return _check_path(v)


class AskRequest(FileTarget):
    question: str


class ExplainSelectionRequest(FileTarget):
    selection: str


class FlagRequest(FileTarget):
    flagged: bool
    note: str = ""


class CommentRequest(FileTarget):
    text: str
    # Optional line anchor (new-side diff lines). When `line` is set the comment
    # is posted as a GitHub review comment on path@line; `start_line` (< line)
    # makes it a multi-line range. Absent → a general PR comment (file-level).
    line: int | None = None
    start_line: int | None = None
    side: str = "RIGHT"


class CommentModel(BaseModel):
    text: str
    line: int | None = None
    start_line: int | None = None

    @classmethod
    def coerce(cls, c) -> "CommentModel":
        # Tolerate legacy entries persisted as bare strings (pre-line-anchor).
        return cls(text=c) if isinstance(c, str) else cls(**c)


class SubmitRequest(PRTarget):
    event: str
    body: str | None = None

    @field_validator("event")
    @classmethod
    def _v_event(cls, v: str) -> str:
        return _check_event(v)


class PRInfoModel(BaseModel):
    owner: str
    repo: str
    number: int
    title: str = ""
    author: str = ""
    body: str = ""
    base: str = ""
    head: str = ""
    state: str = ""
    review_decision: str = ""
    ci_status: str = ""
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0

    @classmethod
    def of(cls, pr: PRInfo) -> "PRInfoModel":
        return cls(**pr.__dict__)


class FileListItem(BaseModel):
    filename: str
    additions: int = 0
    deletions: int = 0
    flagged: bool = False
    flag_note: str = ""
    viewed: bool = False
    comments: list[CommentModel] = []

    @classmethod
    def of(cls, fd: FileDiff) -> "FileListItem":
        return cls(
            filename=fd.filename,
            additions=fd.additions,
            deletions=fd.deletions,
            flagged=fd.flagged,
            flag_note=fd.flag_note,
            viewed=fd.viewed,
            comments=[CommentModel.coerce(c) for c in fd.comments],
        )


class FileDetail(FileListItem):
    diff_text: str


class ReviewStateModel(BaseModel):
    viewed: list[str] = []
    flagged: dict[str, str] = {}
    comments: int = 0
    comment_threads: dict[str, list[CommentModel]] = {}
    submitted: bool = False

    @classmethod
    def of(cls, state: dict) -> "ReviewStateModel":
        return cls(
            viewed=list(state.get("viewed", [])),
            flagged=dict(state.get("flagged", {})),
            comments=int(state.get("comments", 0)),
            comment_threads={
                k: [CommentModel.coerce(c) for c in v]
                for k, v in state.get("comment_threads", {}).items()
            },
            submitted=bool(state.get("submitted", False)),
        )


class PRResponse(BaseModel):
    pr: PRInfoModel
    files: list[FileListItem]
    state: ReviewStateModel


class JobIdResponse(BaseModel):
    job_id: str


class JobStatusResponse(BaseModel):
    status: str
    result: str | None = None
    error: str | None = None
    elapsed: float = 0.0


class OkResponse(BaseModel):
    ok: bool
    error: str | None = None


class ViewedResponse(BaseModel):
    viewed: bool
    remote_ok: bool


class FlagResponse(BaseModel):
    flagged: bool
    note: str


class ResumableRow(BaseModel):
    owner: str
    repo: str
    number: int
    viewed_count: int
    flagged_count: int
    submitted: bool


# ---------------------------------------------------------------------------
# Repowise (G2) — same owner/repo validators; heavy path validation is
# server-side in repowise.validate_and_persist_path (a non-empty string here).
# ---------------------------------------------------------------------------

class RepoRef(BaseModel):
    owner: str
    repo: str

    @field_validator("owner")
    @classmethod
    def _v_owner(cls, v: str) -> str:
        return _check_owner(v)

    @field_validator("repo")
    @classmethod
    def _v_repo(cls, v: str) -> str:
        return _check_repo(v)


class RepoPathRequest(RepoRef):
    path: str

    @field_validator("path")
    @classmethod
    def _v_path(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("path must not be empty")
        return v


class RepoPathResponse(BaseModel):
    ok: bool
    path: str


class PrepareRequest(PRTarget):
    pass


class RepowiseStatusResponse(BaseModel):
    cli_present: bool
    cli_hint: str | None = None
    node_ok: bool = True
    node_hint: str | None = None
    repo_path_known: bool
    repo_path: str | None = None
    indexed: bool = False
    serve_running: bool = False
    serve_url: str | None = None
    serve_port: int | None = None
    frameable: bool | None = None


class PrepareStep(BaseModel):
    key: str
    status: str  # pending | running | done | skipped | failed
    detail: str = ""


class PrepareSnapshot(BaseModel):
    status: str  # running | done | error | cancelled
    steps: list[PrepareStep]
    elapsed: float = 0.0
    dashboard_url: str | None = None
    serve_port: int | None = None
    frameable: bool | None = None
    error: str | None = None
    error_step: str | None = None
    error_hint: str | None = None
    stderr_tail: str | None = None


# --- Diff-mode blast radius (associations among the PR's changed files) ------

class BlastRadiusRequest(PRTarget):
    changed_files: list[str]
    max_depth: int = Field(default=3, ge=1, le=10)


class DirectRisk(BaseModel):
    path: str
    risk_score: float = 0.0
    temporal_hotspot: float = 0.0
    centrality: float = 0.0


class TransitiveAffected(BaseModel):
    path: str
    depth: int


class CochangeWarning(BaseModel):
    changed: str
    missing_partner: str
    score: float = 0.0


class ReviewerSuggestion(BaseModel):
    email: str
    files: int = 0
    ownership_pct: float = 0.0


class BlastRadiusModel(BaseModel):
    direct_risks: list[DirectRisk] = []
    transitive_affected: list[TransitiveAffected] = []
    cochange_warnings: list[CochangeWarning] = []
    recommended_reviewers: list[ReviewerSuggestion] = []
    test_gaps: list[str] = []
    overall_risk_score: float = 0.0


class CoverageIngestRequest(PRTarget):
    path: str | None = None  # explicit report path; blank → auto-detect


class CoverageIngestModel(BaseModel):
    ok: bool
    files: int = 0
    path: str = ""
