from pathlib import Path

import prview.core as core
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
