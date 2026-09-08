from pathlib import Path

import prview.core as core
from prview.api_models import CommentModel, ReviewStateModel
from prview.core import (
    FileDiff,
    _state_path,
    apply_saved_state,
    collect_state,
    load_review_state,
    save_review_state,
)


def test_state_path_under_prview_state():
    p = _state_path("owner", "repo", 7)
    assert p == Path.home() / ".prview" / "state" / "owner-repo-7.json"


def test_load_missing_returns_default(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    assert load_review_state("o", "r", 1) == {
        "viewed": [],
        "flagged": {},
        "comments": 0,
        "comment_threads": {},
        "submitted": False,
    }


def test_apply_saved_state_populates_file_comments(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    files = [FileDiff(filename="a.py", diff_text=""), FileDiff(filename="b.py", diff_text="")]
    state = {"comment_threads": {"a.py": ["first", "second"]}}
    apply_saved_state(files, state)
    assert files[0].comments == ["first", "second"]
    assert files[1].comments == []


def test_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")

    files = [
        FileDiff(filename="a.py", diff_text=""),
        FileDiff(filename="b.py", diff_text=""),
        FileDiff(filename="c.py", diff_text=""),
    ]
    files[0].viewed = True
    files[1].flagged = True
    files[1].flag_note = "needs work"

    state = collect_state(files, comments_posted=4)
    save_review_state("o", "r", 9, state)

    loaded = load_review_state("o", "r", 9)
    assert loaded["viewed"] == ["a.py"]
    assert loaded["flagged"] == {"b.py": "needs work"}
    assert loaded["comments"] == 4

    fresh = [
        FileDiff(filename="a.py", diff_text=""),
        FileDiff(filename="b.py", diff_text=""),
        FileDiff(filename="c.py", diff_text=""),
    ]
    apply_saved_state(fresh, loaded)
    assert fresh[0].viewed is True
    assert fresh[1].flagged is True
    assert fresh[1].flag_note == "needs work"
    assert fresh[2].viewed is False and fresh[2].flagged is False


def test_cli_written_file_without_submitted_still_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True)
    path = _state_path("o", "r", 3)
    path.write_text('{"viewed": ["x.py"], "flagged": {}, "comments": 2}\n')
    loaded = load_review_state("o", "r", 3)
    assert loaded["viewed"] == ["x.py"]
    assert loaded.get("submitted", False) is False


def test_added_line_numbers_from_unified_diff():
    from prview.core import added_line_numbers
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n+++ b/x.py\n"
        "@@ -1,3 +1,4 @@\n"
        " a\n"          # context  new 1
        "+b\n"          # added    new 2
        " c\n"          # context  new 3
        "-old\n"        # removed  (no new)
        "+new1\n"       # added    new 4
        "@@ -20,2 +21,3 @@\n"
        " ctx\n"        # context  new 21
        "+tail\n"       # added    new 22
    )
    assert added_line_numbers(diff) == [2, 4, 22]


def test_added_line_numbers_empty_for_no_additions():
    from prview.core import added_line_numbers
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,1 @@\n a\n-gone\n"
    assert added_line_numbers(diff) == []


def test_added_line_numbers_keeps_an_added_line_that_looks_like_a_diff_header():
    from prview.core import added_line_numbers
    diff = (
        "diff --git a/x.patch b/x.patch\n--- a/x.patch\n+++ b/x.patch\n"
        "@@ -0,0 +1,4 @@\n"
        "+--- a/x\n"
        "++++ b/x\n"
        "+@@ -1 +1 @@\n"
        "++real\n"
    )
    assert added_line_numbers(diff) == [1, 2, 3, 4]


def test_overview_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    core.save_overview("o", "r", 1, "sha-a", "# Overview\n```\nbox\n```")
    data = core.load_overview("o", "r", 1)
    assert data["sha"] == "sha-a"
    assert data["markdown"].startswith("# Overview")
    assert data["generated_at"] > 0


def test_overview_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    assert core.load_overview("o", "r", 1) == {}


def test_overview_corrupt_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "o-r-1-overview.json").write_text("{not json")
    assert core.load_overview("o", "r", 1) == {}


# --- staged comment tier ------------------------------------------------------

def test_a_comment_saved_before_staging_existed_still_loads():
    c = CommentModel.coerce({"text": "hi", "line": 4})
    assert c.staged is False
    assert c.source is None and c.run_id is None


def test_a_legacy_bare_string_comment_still_loads():
    assert CommentModel.coerce("old style").text == "old style"


def test_a_staged_finding_carries_its_provenance():
    c = CommentModel.coerce({
        "text": "IndexError on empty input", "line": 412, "staged": True,
        "source": "pr-review", "severity": "fix", "id": "f1", "run_id": "r1",
    })
    assert (c.staged, c.source, c.severity) == (True, "pr-review", "fix")


def test_state_with_no_review_notes_loads_with_an_empty_bucket():
    st = ReviewStateModel.of({"viewed": [], "flagged": {}, "comments": 0})
    assert st.review_notes == []


def test_review_notes_round_trip():
    st = ReviewStateModel.of({
        "review_notes": [{"text": "no tests cover this", "source": "pr-review",
                          "severity": "consider", "id": "n1", "run_id": "r1"}],
    })
    assert st.review_notes[0].text == "no tests cover this"
