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


class FlagRequest(FileTarget):
    flagged: bool
    note: str = ""


class CommentRequest(FileTarget):
    text: str


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

    @classmethod
    def of(cls, fd: FileDiff) -> "FileListItem":
        return cls(
            filename=fd.filename,
            additions=fd.additions,
            deletions=fd.deletions,
            flagged=fd.flagged,
            flag_note=fd.flag_note,
            viewed=fd.viewed,
        )


class FileDetail(FileListItem):
    diff_text: str


class ReviewStateModel(BaseModel):
    viewed: list[str] = []
    flagged: dict[str, str] = {}
    comments: int = 0
    submitted: bool = False

    @classmethod
    def of(cls, state: dict) -> "ReviewStateModel":
        return cls(
            viewed=list(state.get("viewed", [])),
            flagged=dict(state.get("flagged", {})),
            comments=int(state.get("comments", 0)),
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
