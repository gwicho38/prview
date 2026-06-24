"""Tests for prview.gh — gh CLI wrappers. All subprocess calls are patched;
no real gh process is ever spawned."""
import json
from unittest.mock import patch

import pytest

from prview.core import PRInfo
from prview.gh import (
    GhError,
    fetch_pr_info,
    mark_file_viewed,
    post_pr_comment,
)


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_fetch_pr_info_maps_payload_and_ci_rollup():
    payload = {
        "title": "Add feature",
        "author": {"login": "alice"},
        "body": "hello",
        "baseRefName": "main",
        "headRefName": "feat",
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {"conclusion": "SUCCESS"},
            {"conclusion": "SUCCESS"},
        ],
        "additions": 10,
        "deletions": 2,
        "changedFiles": 3,
    }
    with patch("prview.gh.subprocess.run", return_value=_Result(stdout=json.dumps(payload))):
        pr = fetch_pr_info("o", "r", 42)
    assert isinstance(pr, PRInfo)
    assert pr.owner == "o" and pr.repo == "r" and pr.number == 42
    assert pr.title == "Add feature"
    assert pr.author == "alice"
    assert pr.ci_status == "pass"
    assert pr.additions == 10 and pr.deletions == 2 and pr.changed_files == 3


def test_fetch_pr_info_ci_rollup_states():
    base = {"author": {"login": "a"}}

    def run_with(rollup):
        payload = dict(base, statusCheckRollup=rollup)
        with patch("prview.gh.subprocess.run", return_value=_Result(stdout=json.dumps(payload))):
            return fetch_pr_info("o", "r", 1).ci_status

    assert run_with([]) == "none"
    assert run_with([{"conclusion": "FAILURE"}, {"conclusion": "SUCCESS"}]) == "fail"
    assert run_with([{"conclusion": "PENDING"}]) == "pending"


def test_fetch_pr_info_unauth_raises_structured_gherror():
    err = _Result(returncode=1, stderr="gh: not authenticated")
    with patch("prview.gh.subprocess.run", return_value=err):
        with pytest.raises(GhError) as exc:
            fetch_pr_info("o", "r", 42)
    e = exc.value
    assert isinstance(e.message, str) and e.message
    assert e.hint == "run `gh auth login`"
    # The actionable hint must surface to the user.
    assert "gh auth login" in str(e)


def test_post_pr_comment_preserves_body_prefix_as_argv_element():
    captured = {}

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        return _Result(returncode=0)

    with patch("prview.gh.subprocess.run", side_effect=fake_run):
        ok = post_pr_comment("o", "r", 42, "src/app.py", "Looks good")

    assert ok is True
    cmd = captured["cmd"]
    # text passed as a discrete argv element, never shell-interpolated.
    assert "--body" in cmd
    body = cmd[cmd.index("--body") + 1]
    assert body == "**src/app.py**\n\nLooks good"
    # argv is the fixed gh comment invocation.
    assert cmd[:3] == ["gh", "pr", "comment"]


def test_post_pr_review_comment_anchors_range_via_gh_api():
    from prview.gh import post_pr_review_comment
    captured = {}

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        return _Result(returncode=0)

    with patch("prview.gh.subprocess.run", side_effect=fake_run):
        ok = post_pr_review_comment("o", "r", 42, "src/app.py", "range nit",
                                    "sha123", line=5, side="RIGHT", start_line=3)
    assert ok is True
    cmd = captured["cmd"]
    assert cmd[:5] == ["gh", "api", "--method", "POST", "repos/o/r/pulls/42/comments"]
    # numeric fields use -F (typed); strings use -f; range adds start_line/start_side
    assert "-F" in cmd and f"line=5" in cmd
    assert "body=range nit" in cmd and "commit_id=sha123" in cmd and "path=src/app.py" in cmd
    assert "side=RIGHT" in cmd
    assert "start_line=3" in cmd and "start_side=RIGHT" in cmd


def test_post_pr_review_comment_single_line_omits_range():
    from prview.gh import post_pr_review_comment
    captured = {}
    with patch("prview.gh.subprocess.run",
               side_effect=lambda cmd, *a, **k: (captured.update(cmd=cmd), _Result(0))[1]):
        post_pr_review_comment("o", "r", 42, "src/app.py", "nit", "sha", line=9)
    cmd = captured["cmd"]
    assert "line=9" in cmd
    assert not any(str(x).startswith("start_line=") for x in cmd)


def test_mark_file_viewed_two_step_success():
    results = iter([
        _Result(returncode=0, stdout="PR_nodeid\n"),  # gh pr view --json id
        _Result(returncode=0, stdout="{}"),            # gh api graphql
    ])
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return next(results)

    with patch("prview.gh.subprocess.run", side_effect=fake_run):
        ok = mark_file_viewed("o", "r", 42, "src/app.py")

    assert ok is True
    assert len(calls) == 2
    assert calls[0][:3] == ["gh", "pr", "view"]
    assert calls[1][:3] == ["gh", "api", "graphql"]
    # the pr node id flows into the graphql args; path is an argv element.
    assert any("prId=PR_nodeid" == arg for arg in calls[1])
    assert any("path=src/app.py" == arg for arg in calls[1])


def test_mark_file_viewed_graphql_failure_returns_false_not_exception():
    results = iter([
        _Result(returncode=0, stdout="PR_nodeid\n"),       # id lookup ok
        _Result(returncode=1, stderr="graphql boom"),       # markFileAsViewed fails
    ])

    def fake_run(cmd, *a, **kw):
        return next(results)

    with patch("prview.gh.subprocess.run", side_effect=fake_run):
        ok = mark_file_viewed("o", "r", 42, "src/app.py")

    # No exception: API can report local-only save.
    assert ok is False


def test_mark_file_viewed_id_lookup_failure_returns_false():
    with patch("prview.gh.subprocess.run", return_value=_Result(returncode=1, stderr="x")):
        ok = mark_file_viewed("o", "r", 42, "src/app.py")
    assert ok is False
