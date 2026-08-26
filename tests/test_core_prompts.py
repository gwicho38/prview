from types import SimpleNamespace

import prview.core as core
from prview.core import (
    FileDiff,
    PRInfo,
    _DIFF_LIMIT,
    build_ask_prompt,
    build_explain_prompt,
    build_explain_selection_prompt,
    build_overview_prompt,
    build_summary_prompt,
)


def _pr():
    return PRInfo(
        owner="o",
        repo="r",
        number=42,
        title="Add feature",
        author="alice",
        body="B" * 2000,
    )


def _fd(diff_text="diff body\n"):
    return FileDiff(
        filename="src/app.py",
        diff_text=diff_text,
        additions=3,
        deletions=1,
    )


def test_summary_prompt_byte_for_byte():
    pr, fd = _pr(), _fd()
    expected = (
        f"PR: {pr.title} by {pr.author}\n"
        f"File: {fd.filename} (+{fd.additions} -{fd.deletions})\n"
        f"Diff:\n```diff\n{fd.diff_text}\n```\n\n"
        "In 1-2 sentences, summarize what changed in this file and why. Be direct."
    )
    assert build_summary_prompt(pr, fd) == expected


def test_explain_prompt_byte_for_byte():
    pr, fd = _pr(), _fd()
    expected = (
        f"You are a code reviewer.\n\n"
        f"PR: {pr.title} (#{pr.number}) by {pr.author}\n\n"
        f"File: {fd.filename}\n"
        f"Diff:\n```diff\n{fd.diff_text}\n```\n\n"
        f"Explain the code in this file. Focus on:\n"
        f"- What does this file do? What is its role in the codebase?\n"
        f"- Walk through the key functions, classes, or data structures line by line\n"
        f"- How do the changed/added parts work mechanically?\n"
        f"- Flag any bugs, logic errors, or edge cases in the implementation\n\n"
        f"Do NOT summarize the PR or describe what changed at a high level. "
        f"Explain the actual code — what it does, how it works, and what could break."
    )
    assert build_explain_prompt(pr, fd) == expected


def test_ask_prompt_includes_full_diff_body_cap_and_anchor_steer():
    pr, fd = _pr(), _fd()
    question = "Why this approach?"
    prompt = build_ask_prompt(pr, fd, question)
    assert f"Description: {pr.body[:1000]}\n\n" in prompt   # body still capped at 1000
    assert f"Diff:\n```diff\n{fd.diff_text}\n```\n\n" in prompt
    assert f"User question: {question}" in prompt
    # The reference-anchor steer the user asked for.
    assert "treat that reference as the anchor" in prompt


def test_prompts_send_full_diff_not_old_cuts():
    """The 4000/8000-char cuts are gone: a large file reaches the model whole."""
    pr = _pr()
    fd = _fd("x" * 30000)  # over both old cuts, under the new limit
    for prompt in (build_summary_prompt(pr, fd), build_explain_prompt(pr, fd),
                   build_ask_prompt(pr, fd, "q"), build_explain_selection_prompt(pr, fd, "s")):
        assert "x" * 30000 in prompt
        assert "truncated" not in prompt


def test_prompts_clip_pathological_diff_with_explicit_marker():
    pr = _pr()
    fd = _fd("z" * (_DIFF_LIMIT + 5000))
    prompt = build_explain_prompt(pr, fd)
    assert "z" * _DIFF_LIMIT in prompt
    assert f"truncated at {_DIFF_LIMIT} characters" in prompt


def test_ask_prompt_truncates_body_to_1000():
    pr = _pr()
    fd = _fd(diff_text="z" * 200)
    prompt = build_ask_prompt(pr, fd, "q")
    assert "B" * 1000 in prompt
    assert "B" * 1001 not in prompt


def test_explain_selection_prompt_includes_snippet_and_context():
    pr, fd = _pr(), _fd("diff body\n")
    prompt = build_explain_selection_prompt(pr, fd, "def handle(self):\n    pass")
    # snippet is embedded, plus file + diff context, and the "only this snippet" steer
    assert "def handle(self):\n    pass" in prompt
    assert fd.filename in prompt
    assert "diff body" in prompt
    assert "only this snippet" in prompt


def test_explain_selection_prompt_caps_selection_only():
    pr = _pr()
    fd = _fd("z" * 9000)
    prompt = build_explain_selection_prompt(pr, fd, "s" * 3000)
    assert "s" * 2000 in prompt and "s" * 2001 not in prompt   # selection still capped at 2000
    assert "z" * 9000 in prompt                                # diff now sent in full


def _ov_pr(**kw):
    base = dict(owner="o", repo="r", number=5, title="Queue consolidation",
                author="gw", body="Fixes silent drops", head_sha="sha-a")
    base.update(kw)
    return core.PRInfo(**base)


def test_overview_prompt_contains_header_and_file_table():
    files = [core.FileDiff("a.py", "diff --git a/a.py b/a.py\n+x", additions=3, deletions=1),
             core.FileDiff("b.py", "diff --git a/b.py b/b.py\n+y", additions=1, deletions=0)]
    p = core.build_overview_prompt(_ov_pr(), files)
    assert "Queue consolidation" in p
    assert "Fixes silent drops" in p
    assert "a.py (+3 -1)" in p
    assert "b.py (+1 -0)" in p


def test_overview_prompt_includes_diffs_and_instructions():
    files = [core.FileDiff("a.py", "diff --git a/a.py b/a.py\n+real-diff-line", additions=1, deletions=0)]
    p = core.build_overview_prompt(_ov_pr(), files)
    assert "+real-diff-line" in p
    assert "three sentences" in p
    assert "ASCII" in p
    assert "No mermaid" in p
    assert "lifecycle" in p.lower()


def test_overview_prompt_budget_omits_oversized_diffs():
    huge = core.FileDiff("huge.py", "x" * (core._DIFF_LIMIT + 1), additions=9000, deletions=0)
    small = core.FileDiff("small.py", "diff --git a/small.py b/small.py\n+tiny", additions=1, deletions=0)
    p = core.build_overview_prompt(_ov_pr(), [huge, small])
    assert "diffs omitted for 1 smaller files: huge.py" in p
    assert "+tiny" in p            # smaller file still included after the skip
    assert "x" * 1000 not in p     # huge diff body absent


def test_overview_prompt_clips_body():
    p = core.build_overview_prompt(_ov_pr(body="B" * 20_000), [])
    assert "B" * core._OVERVIEW_BODY_LIMIT in p
    assert "B" * (core._OVERVIEW_BODY_LIMIT + 1) not in p


def test_overview_prompt_embeds_exemplars():
    p = core.build_overview_prompt(_ov_pr(), [])
    assert core._OVERVIEW_EXEMPLARS.strip() in p


def test_behavior_names_prompt_carries_ids_titles_and_files_but_no_diffs():
    pr = core.PRInfo(owner="o", repo="r", number=7, title="Add orders", body="why")
    derived = [
        SimpleNamespace(id="b1", title="feat: model", source_shas=("aaa111",), filenames=("model.py",)),
        SimpleNamespace(
            id="b2", title="feat: wire", source_shas=("bbb222",),
            filenames=("server.py", "app.js"),
        ),
    ]
    prompt = core.build_behavior_names_prompt(pr, derived)
    assert "b1" in prompt and "feat: model" in prompt and "model.py" in prompt
    assert "server.py, app.js" in prompt
    assert "aaa111" in prompt and "bbb222" in prompt
    assert "Add orders" in prompt
    assert "@@" not in prompt and "diff --git" not in prompt
    assert "->" in prompt
