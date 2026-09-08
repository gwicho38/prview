"""Tests for prview.findings — parsing an agent's JSONL and placing findings on
the diff. Pure: no subprocess, no filesystem, no state."""
from prview.findings import Finding, MAX_FINDINGS_PER_RUN, parse_findings, place


# --- parse --------------------------------------------------------------------

def test_one_json_object_per_line_becomes_one_finding():
    out, dropped = parse_findings(
        '{"path": "a.py", "line": 3, "body": "boom", "severity": "fix"}\n'
        '{"path": "b.py", "body": "meh"}\n'
    )
    assert dropped == 0
    assert [f.path for f in out] == ["a.py", "b.py"]
    assert out[0].severity == "fix"


def test_prose_around_the_findings_is_ignored_not_fatal():
    out, dropped = parse_findings(
        "Let me look at the diff.\n"
        '{"path": "a.py", "body": "boom"}\n'
        "Done.\n"
    )
    assert len(out) == 1 and dropped == 0


def test_a_json_line_missing_body_is_dropped_and_counted():
    out, dropped = parse_findings('{"path": "a.py", "line": 3}\n')
    assert out == [] and dropped == 1


def test_a_json_line_with_a_blank_body_is_dropped():
    out, dropped = parse_findings('{"path": "a.py", "body": "   "}\n')
    assert out == [] and dropped == 1


def test_a_json_line_missing_path_is_kept_for_the_pr_tier():
    out, dropped = parse_findings('{"body": "no tests anywhere"}\n')
    assert len(out) == 1 and out[0].path is None and dropped == 0


def test_a_json_array_line_is_prose_not_a_lost_finding():
    # Only `{`-prefixed lines are finding candidates, so `[info] …` log noise
    # does not inflate the drop count.
    out, dropped = parse_findings('["a.py", "boom"]\n')
    assert out == [] and dropped == 0


def test_a_truncated_json_line_is_dropped_and_counted():
    out, dropped = parse_findings('{"path": "a.py", "body": "bo\n')
    assert out == [] and dropped == 1


def test_a_non_integer_line_is_treated_as_absent():
    out, _ = parse_findings('{"path": "a.py", "line": "412", "body": "boom"}\n')
    assert out[0].line is None


def test_a_findings_flood_is_capped_and_the_overflow_counted():
    text = "".join('{"path": "a.py", "body": "x"}\n'
                   for _ in range(MAX_FINDINGS_PER_RUN + 5))
    out, dropped = parse_findings(text)
    assert len(out) == MAX_FINDINGS_PER_RUN
    assert dropped == 5


# --- place --------------------------------------------------------------------

ADDED = {"a.py": [10, 11, 12], "b.py": [5]}


def test_a_line_the_diff_touches_anchors_there():
    p = place(Finding("a.py", 11, None, "fix", "boom", None), ADDED)
    assert (p.tier, p.path, p.line) == ("anchored", "a.py", 11)
    assert p.reason is None


def test_a_valid_range_anchors_as_a_range():
    p = place(Finding("a.py", 12, 10, None, "boom", None), ADDED)
    assert (p.tier, p.start_line, p.line) == ("anchored", 10, 12)


def test_a_line_the_diff_does_not_touch_demotes_to_file_level():
    p = place(Finding("a.py", 99, None, None, "boom", None), ADDED)
    assert (p.tier, p.path, p.line) == ("file", "a.py", None)
    assert "not in the diff" in p.reason


def test_a_range_with_one_bad_end_demotes_to_file_level():
    p = place(Finding("a.py", 12, 99, None, "boom", None), ADDED)
    assert p.tier == "file"


def test_an_inverted_range_demotes_to_file_level():
    p = place(Finding("a.py", 10, 12, None, "boom", None), ADDED)
    assert p.tier == "file"


def test_a_file_with_no_line_is_file_level_without_a_demotion_reason():
    p = place(Finding("b.py", None, None, None, "boom", None), ADDED)
    assert (p.tier, p.reason) == ("file", None)


def test_a_path_not_in_the_pr_becomes_a_pr_level_note_keeping_the_path():
    p = place(Finding("nowhere.py", 3, None, None, "boom", None), ADDED)
    assert p.tier == "pr"
    assert "nowhere.py" in p.reason


def test_no_path_at_all_is_a_pr_level_note():
    p = place(Finding(None, None, None, None, "no tests", None), ADDED)
    assert (p.tier, p.reason) == ("pr", None)


def test_a_file_the_pr_touches_but_adds_nothing_to_is_file_level():
    p = place(Finding("renamed.py", 3, None, None, "boom", None), {"renamed.py": []})
    assert p.tier == "file"
