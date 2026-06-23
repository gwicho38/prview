from prview.core import (
    FileDiff,
    PRInfo,
    build_ask_prompt,
    build_explain_prompt,
    build_explain_selection_prompt,
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
    diff_preview = fd.diff_text[:4000]
    expected = (
        f"PR: {pr.title} by {pr.author}\n"
        f"File: {fd.filename} (+{fd.additions} -{fd.deletions})\n"
        f"Diff:\n```diff\n{diff_preview}\n```\n\n"
        "In 1-2 sentences, summarize what changed in this file and why. Be direct."
    )
    assert build_summary_prompt(pr, fd) == expected


def test_summary_prompt_truncates_diff_to_4000():
    pr = _pr()
    fd = _fd(diff_text="x" * 9000)
    prompt = build_summary_prompt(pr, fd)
    assert "x" * 4000 in prompt
    assert "x" * 4001 not in prompt


def test_explain_prompt_byte_for_byte():
    pr, fd = _pr(), _fd()
    expected = (
        f"You are a code reviewer.\n\n"
        f"PR: {pr.title} (#{pr.number}) by {pr.author}\n\n"
        f"File: {fd.filename}\n"
        f"Diff:\n```diff\n{fd.diff_text[:8000]}\n```\n\n"
        f"Explain the code in this file. Focus on:\n"
        f"- What does this file do? What is its role in the codebase?\n"
        f"- Walk through the key functions, classes, or data structures line by line\n"
        f"- How do the changed/added parts work mechanically?\n"
        f"- Flag any bugs, logic errors, or edge cases in the implementation\n\n"
        f"Do NOT summarize the PR or describe what changed at a high level. "
        f"Explain the actual code — what it does, how it works, and what could break."
    )
    assert build_explain_prompt(pr, fd) == expected


def test_explain_prompt_truncates_diff_to_8000():
    pr = _pr()
    fd = _fd(diff_text="y" * 12000)
    prompt = build_explain_prompt(pr, fd)
    assert "y" * 8000 in prompt
    assert "y" * 8001 not in prompt


def test_ask_prompt_byte_for_byte():
    pr, fd = _pr(), _fd()
    question = "Why this approach?"
    expected = (
        f"You are reviewing a pull request.\n\n"
        f"PR: {pr.title} (#{pr.number}) by {pr.author}\n"
        f"Description: {pr.body[:1000]}\n\n"
        f"File: {fd.filename}\n"
        f"Diff:\n```diff\n{fd.diff_text[:8000]}\n```\n\n"
        f"User question: {question}\n\n"
        f"Answer concisely based on the diff and PR context."
    )
    assert build_ask_prompt(pr, fd, question) == expected


def test_ask_prompt_truncates_body_to_1000_and_diff_to_8000():
    pr = _pr()
    fd = _fd(diff_text="z" * 12000)
    prompt = build_ask_prompt(pr, fd, "q")
    assert "B" * 1000 in prompt
    assert "B" * 1001 not in prompt
    assert "z" * 8000 in prompt
    assert "z" * 8001 not in prompt


def test_explain_selection_prompt_includes_snippet_and_context():
    pr, fd = _pr(), _fd("diff body\n")
    prompt = build_explain_selection_prompt(pr, fd, "def handle(self):\n    pass")
    # snippet is embedded, plus file + diff context, and the "only this snippet" steer
    assert "def handle(self):\n    pass" in prompt
    assert fd.filename in prompt
    assert "diff body" in prompt
    assert "only this snippet" in prompt


def test_explain_selection_prompt_caps_selection_and_diff():
    pr = _pr()
    fd = _fd("z" * 9000)
    prompt = build_explain_selection_prompt(pr, fd, "s" * 3000)
    assert "s" * 2000 in prompt and "s" * 2001 not in prompt   # selection capped at 2000
    assert "z" * 8000 in prompt and "z" * 8001 not in prompt   # diff capped at 8000
