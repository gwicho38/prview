import pytest

import prview.core as core
from prview.core import parse_pr_ref, parse_diff, FileDiff


def test_parse_pr_ref_owner_repo_form():
    assert parse_pr_ref("owner/repo#123") == ("owner", "repo", 123)


def test_parse_pr_ref_full_url():
    ref = "https://github.com/owner/repo/pull/123"
    assert parse_pr_ref(ref) == ("owner", "repo", 123)


def test_parse_pr_ref_bare_number_rejected():
    with pytest.raises(ValueError):
        parse_pr_ref("123")


def test_parse_pr_ref_garbage_rejected():
    with pytest.raises(ValueError):
        parse_pr_ref("not a pr ref at all")


def test_parse_diff_multi_file_counts():
    raw = (
        "diff --git a/foo.py b/foo.py\n"
        "index 111..222 100644\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,2 +1,3 @@\n"
        " keep\n"
        "+added one\n"
        "+added two\n"
        "-removed one\n"
        "diff --git a/bar.py b/bar.py\n"
        "index 333..444 100644\n"
        "--- a/bar.py\n"
        "+++ b/bar.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old bar\n"
        "+new bar\n"
    )
    files = parse_diff(raw)
    assert [f.filename for f in files] == ["foo.py", "bar.py"]
    foo, bar = files
    assert foo.additions == 2 and foo.deletions == 1
    assert bar.additions == 1 and bar.deletions == 1


def test_parse_diff_binary_chunk_no_crash():
    raw = (
        "diff --git a/image.png b/image.png\n"
        "index 555..666 100644\n"
        "Binary files a/image.png and b/image.png differ\n"
    )
    files = parse_diff(raw)
    assert len(files) == 1
    fd = files[0]
    assert isinstance(fd, FileDiff)
    assert fd.filename == "image.png"
    assert fd.additions == 0
    assert fd.deletions == 0


def test_first_hunk_range_anchors_added_lines_on_the_right():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -10,2 +10,4 @@\n ctx\n+one\n+two\n ctx\n"
    )
    assert core.first_hunk_range(diff) == (11, 12, "RIGHT")


def test_first_hunk_range_anchors_a_deletions_only_diff_on_the_left():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -5,3 +4,1 @@\n ctx\n-gone\n-also gone\n"
    )
    assert core.first_hunk_range(diff) == (6, 7, "LEFT")


def test_first_hunk_range_prefers_the_first_hunk_with_added_lines():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,1 @@\n ctx\n-removed\n"
        "@@ -20,1 +19,2 @@\n ctx\n+added\n"
    )
    assert core.first_hunk_range(diff) == (20, 20, "RIGHT")


def test_first_hunk_range_returns_none_without_hunks():
    assert core.first_hunk_range("diff --git a/a.png b/a.png\nBinary files differ\n") is None
    assert core.first_hunk_range("") is None


def test_first_hunk_range_ignores_the_diff_header_plus_lines():
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n ctx\n+real\n"
    assert core.first_hunk_range(diff) == (2, 2, "RIGHT")


def test_first_hunk_range_keeps_an_added_line_whose_content_starts_with_plusplusplus():
    diff = (
        "diff --git a/x.patch b/x.patch\n--- a/x.patch\n+++ b/x.patch\n"
        "@@ -0,0 +1,4 @@\n"
        "+--- a/x\n"
        "++++ b/x\n"
        "+@@ -1 +1 @@\n"
        "++real\n"
    )
    assert core.first_hunk_range(diff) == (1, 4, "RIGHT")


def test_first_hunk_range_keeps_a_removed_line_whose_content_starts_with_dashdashdash():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -5,3 +4,1 @@\n ctx\n----gone\n----also gone\n"
    )
    assert core.first_hunk_range(diff) == (6, 7, "LEFT")
