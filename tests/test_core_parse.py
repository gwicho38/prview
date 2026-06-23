import pytest

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
