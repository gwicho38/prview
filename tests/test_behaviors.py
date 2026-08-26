"""Behavior derivation is pure: commit metadata in, ordered Behavior list out."""
import prview.behaviors as behaviors


def _c(sha, subject, is_merge=False):
    return {"sha": sha, "subject": subject, "is_merge": is_merge}


def test_each_file_lands_in_the_earliest_behavior_that_touches_it():
    got = behaviors.behaviors_from_commits(
        [_c("a", "feat: model"), _c("b", "feat: wire it up")],
        {"a": ["model.py"], "b": ["model.py", "server.py"]},
        {"model.py", "server.py"},
    )
    assert [b.filenames for b in got] == [("model.py",), ("server.py",)]
    assert got[0].also_in == {"model.py": 1}
    assert got[1].also_in == {}


def test_ids_are_contiguous_and_titles_come_from_commit_subjects():
    got = behaviors.behaviors_from_commits(
        [_c("a", "feat: one"), _c("b", "fix: two")],
        {"a": ["x.py"], "b": ["y.py"]},
        {"x.py", "y.py"},
    )
    assert [(b.id, b.title) for b in got] == [("b1", "feat: one"), ("b2", "fix: two")]
    assert [b.source_shas for b in got] == [("a",), ("b",)]


def test_merge_commits_are_skipped():
    got = behaviors.behaviors_from_commits(
        [_c("m", "Merge branch 'main'", is_merge=True), _c("a", "feat: real")],
        {"m": ["merged.py"], "a": ["real.py"]},
        {"merged.py", "real.py"},
    )
    assert [b.title for b in got] == ["feat: real"]
    assert got[0].filenames == ("real.py",)


def test_all_merge_input_yields_no_behaviors():
    got = behaviors.behaviors_from_commits(
        [_c("m", "Merge", is_merge=True)], {"m": ["a.py"]}, {"a.py"},
    )
    assert got == []


def test_files_absent_from_the_pr_diff_are_dropped():
    got = behaviors.behaviors_from_commits(
        [_c("a", "feat: one")], {"a": ["kept.py", "force-pushed-away.py"]}, {"kept.py"},
    )
    assert got[0].filenames == ("kept.py",)


def test_a_behavior_left_with_no_files_is_dropped_and_ids_renumber():
    got = behaviors.behaviors_from_commits(
        [_c("a", "chore: only untracked"), _c("b", "feat: real")],
        {"a": ["gone.py"], "b": ["real.py"]},
        {"real.py"},
    )
    assert [(b.id, b.title) for b in got] == [("b1", "feat: real")]


def test_a_commit_with_no_file_list_is_dropped():
    got = behaviors.behaviors_from_commits(
        [_c("a", "feat: one")], {}, {"real.py"},
    )
    assert got == []


def test_groupable_requires_two_non_merge_commits():
    assert not behaviors.is_groupable([])
    assert not behaviors.is_groupable([_c("a", "feat: only")])
    assert not behaviors.is_groupable([_c("a", "feat: one"), _c("m", "Merge", is_merge=True)])
    assert behaviors.is_groupable([_c("a", "feat: one"), _c("b", "feat: two")])


def test_every_pr_file_appears_exactly_once_across_behaviors():
    got = behaviors.behaviors_from_commits(
        [_c("a", "one"), _c("b", "two"), _c("c", "three")],
        {"a": ["x.py", "y.py"], "b": ["y.py", "z.py"], "c": ["x.py", "z.py"]},
        {"x.py", "y.py", "z.py"},
    )
    seen = [f for b in got for f in b.filenames]
    assert sorted(seen) == ["x.py", "y.py", "z.py"]
