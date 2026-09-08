"""Tests for prview.gh — gh CLI wrappers. All subprocess calls are patched;
no real gh process is ever spawned."""
import json
import os
from unittest.mock import patch

import pytest

import prview.gh as gh
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


def test_fetch_pr_info_parses_head_ref_oid(monkeypatch):
    import json as _json
    import subprocess as _sp

    captured = {}

    def fake_run(cmd):
        captured["cmd"] = cmd
        payload = {
            "title": "t", "author": {"login": "a"}, "body": "b",
            "baseRefName": "main", "headRefName": "feat", "state": "OPEN",
            "reviewDecision": "", "statusCheckRollup": [],
            "additions": 1, "deletions": 1, "changedFiles": 1,
            "headRefOid": "abc123def456",
        }
        return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr="")

    monkeypatch.setattr(gh, "_run", fake_run)
    pr = gh.fetch_pr_info("o", "r", 1)
    assert pr.head_sha == "abc123def456"
    assert "headRefOid" in captured["cmd"][captured["cmd"].index("--json") + 1]


def test_post_pr_comment_file_uses_body_file(monkeypatch):
    import subprocess as _sp
    from pathlib import Path as _P

    captured = {}

    def fake_run(cmd):
        captured["cmd"] = cmd
        idx = cmd.index("--body-file")
        captured["content"] = _P(cmd[idx + 1]).read_text()
        return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(gh, "_run", fake_run)
    body = "## Overview\n```\n┌box┐\n└───┘\n```"
    assert gh.post_pr_comment_file("o", "r", 9, body) is True
    assert captured["cmd"][:4] == ["gh", "pr", "comment", "9"]
    assert "--repo" in captured["cmd"] and "o/r" in captured["cmd"]
    assert captured["content"] == body


def test_latest_review_url_returns_last_html_url(monkeypatch):
    import json as _json
    import subprocess as _sp

    captured = {}

    def fake_run(cmd):
        captured["cmd"] = cmd
        reviews = [
            {"id": 1, "html_url": "https://github.com/o/r/pull/9#pullrequestreview-1"},
            {"id": 2, "html_url": "https://github.com/o/r/pull/9#pullrequestreview-2"},
        ]
        return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(reviews), stderr="")

    monkeypatch.setattr(gh, "_run", fake_run)
    url = gh.latest_review_url("o", "r", 9)
    assert url == "https://github.com/o/r/pull/9#pullrequestreview-2"
    assert captured["cmd"][:2] == ["gh", "api"]
    assert "repos/o/r/pulls/9/reviews" in captured["cmd"]


def test_latest_review_url_none_on_failure_or_empty(monkeypatch):
    import subprocess as _sp

    monkeypatch.setattr(gh, "_run", lambda cmd: _sp.CompletedProcess(cmd, 1, stdout="", stderr="boom"))
    assert gh.latest_review_url("o", "r", 9) is None

    monkeypatch.setattr(gh, "_run", lambda cmd: _sp.CompletedProcess(cmd, 0, stdout="[]", stderr=""))
    assert gh.latest_review_url("o", "r", 9) is None

    monkeypatch.setattr(gh, "_run", lambda cmd: _sp.CompletedProcess(cmd, 0, stdout="{not json", stderr=""))
    assert gh.latest_review_url("o", "r", 9) is None


def test_fetch_pr_commits_parses_subject_and_merge_flag(monkeypatch):
    payload = json.dumps([
        {"sha": "aaa", "commit": {"message": "feat: first\n\nbody"}, "parents": [{"sha": "p1"}]},
        {"sha": "bbb", "commit": {"message": "Merge branch 'main'"}, "parents": [{"sha": "p1"}, {"sha": "p2"}]},
    ])
    monkeypatch.setattr(gh, "_run", lambda cmd: _Result(0, payload))
    assert gh.fetch_pr_commits("o", "r", 7) == [
        {"sha": "aaa", "subject": "feat: first", "is_merge": False},
        {"sha": "bbb", "subject": "Merge branch 'main'", "is_merge": True},
    ]


def test_fetch_pr_commits_raises_structured_error(monkeypatch):
    monkeypatch.setattr(gh, "_run", lambda cmd: _Result(1, "", "not logged in"))
    with pytest.raises(gh.GhError) as ei:
        gh.fetch_pr_commits("o", "r", 7)
    assert ei.value.hint


def test_fetch_commit_files_returns_filenames(monkeypatch):
    payload = json.dumps({"files": [{"filename": "a.py"}, {"filename": "b/c.ts"}]})
    monkeypatch.setattr(gh, "_run", lambda cmd: _Result(0, payload))
    assert gh.fetch_commit_files("o", "r", "aaa") == ["a.py", "b/c.ts"]


def test_fetch_commit_files_tolerates_a_commit_with_no_files(monkeypatch):
    monkeypatch.setattr(gh, "_run", lambda cmd: _Result(0, json.dumps({})))
    assert gh.fetch_commit_files("o", "r", "aaa") == []


# --- fetch_pr_diff: GitHub's 300-file diff cap --------------------------------

_TOO_LARGE = ("HTTP 406: Sorry, the diff exceeded the maximum number of files "
              "(300). Consider using 'List pull requests files' API")


def test_a_normal_pr_diff_never_touches_git(monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return _Result(stdout="diff --git a/x b/x\n")

    monkeypatch.setattr(gh, "_run", fake_run)
    assert gh.fetch_pr_diff("o", "r", 1, base="main") == "diff --git a/x b/x\n"
    assert all(c[0] == "gh" for c in calls)


def test_an_over_cap_pr_falls_back_to_the_local_clone(monkeypatch):
    monkeypatch.setattr(gh.core, "get_repo_path", lambda o, r: "/repos/audio")

    def fake_run(cmd):
        if cmd[0] == "gh":
            return _Result(returncode=1, stderr=_TOO_LARGE)
        if "fetch" in cmd:
            return _Result()
        if "merge-base" in cmd:
            return _Result(stdout="19f9b90\n")
        if "diff" in cmd:
            return _Result(stdout="diff --git a/big b/big\n")
        raise AssertionError(f"unexpected argv: {cmd}")

    monkeypatch.setattr(gh, "_run", fake_run)
    assert gh.fetch_pr_diff("o", "r", 40, base="main") == "diff --git a/big b/big\n"


def test_the_fallback_diffs_the_pr_head_against_its_merge_base(monkeypatch):
    monkeypatch.setattr(gh.core, "get_repo_path", lambda o, r: "/repos/audio")
    seen = []

    def fake_run(cmd):
        seen.append(cmd)
        if cmd[0] == "gh":
            return _Result(returncode=1, stderr=_TOO_LARGE)
        if "merge-base" in cmd:
            return _Result(stdout="19f9b90\n")
        return _Result(stdout="")

    monkeypatch.setattr(gh, "_run", fake_run)
    gh.fetch_pr_diff("o", "r", 40, base="main")
    fetch = next(c for c in seen if "fetch" in c)
    assert fetch[-1] == "+pull/40/head:refs/prview/pr-40"
    merge_base = next(c for c in seen if "merge-base" in c)
    assert merge_base[-2:] == ["origin/main", "refs/prview/pr-40"]
    diff = next(c for c in seen if c[3:4] == ["diff"])
    assert diff[-2:] == ["19f9b90", "refs/prview/pr-40"]


def test_an_over_cap_pr_with_no_local_clone_says_what_to_do(monkeypatch):
    monkeypatch.setattr(gh.core, "get_repo_path", lambda o, r: None)
    monkeypatch.setattr(gh, "_run",
                        lambda cmd: _Result(returncode=1, stderr=_TOO_LARGE))
    with pytest.raises(GhError) as exc:
        gh.fetch_pr_diff("o", "r", 40, base="main")
    assert "300-file" in str(exc.value)
    assert "local repo path" in exc.value.hint


def test_an_ordinary_gh_failure_is_not_retried_against_git(monkeypatch):
    monkeypatch.setattr(gh, "_run",
                        lambda cmd: _Result(returncode=1, stderr="not found"))
    with pytest.raises(GhError) as exc:
        gh.fetch_pr_diff("o", "r", 9, base="main")
    assert "Failed to fetch diff" in str(exc.value)


# --- batched review submission ------------------------------------------------

def test_staged_comments_go_out_as_one_review(monkeypatch):
    seen = {}

    def fake_run(cmd):
        seen["cmd"] = cmd
        with open(cmd[cmd.index("--input") + 1]) as fh:
            seen["payload"] = json.load(fh)
        return _Result(stdout="{}")

    monkeypatch.setattr(gh, "_run", fake_run)
    ok, err = gh.submit_review_with_comments(
        "o", "r", 1, "comment", "body text",
        [{"path": "a.py", "body": "boom", "line": 11, "side": "RIGHT",
          "start_line": None}],
    )
    assert ok and err is None
    assert seen["cmd"][0] == "gh"
    assert seen["payload"]["event"] == "COMMENT"
    assert seen["payload"]["body"] == "body text"
    assert len(seen["payload"]["comments"]) == 1
    # A null start_line is dropped, not sent: the API rejects it on a single-line
    # comment.
    assert "start_line" not in seen["payload"]["comments"][0]


def test_a_multi_line_comment_keeps_its_start_line(monkeypatch):
    seen = {}

    def fake_run(cmd):
        with open(cmd[cmd.index("--input") + 1]) as fh:
            seen["payload"] = json.load(fh)
        return _Result(stdout="{}")

    monkeypatch.setattr(gh, "_run", fake_run)
    gh.submit_review_with_comments("o", "r", 1, "comment", "", [
        {"path": "a.py", "body": "b", "line": 12, "side": "RIGHT", "start_line": 10},
    ])
    assert seen["payload"]["comments"][0]["start_line"] == 10


def test_the_payload_file_is_removed_even_when_gh_fails(monkeypatch):
    seen = {}

    def fake_run(cmd):
        seen["path"] = cmd[cmd.index("--input") + 1]
        return _Result(returncode=1, stderr="422 bad line")

    monkeypatch.setattr(gh, "_run", fake_run)
    ok, err = gh.submit_review_with_comments("o", "r", 1, "comment", "b", [])
    assert ok is False and "422" in err
    assert not os.path.exists(seen["path"])
