"""Source-level assertions on static/app.js — no JS test infra exists, so the
route-default and hash-routing acceptance criteria are pinned with string
checks. Brittle by design: renaming these symbols must update this test."""
from pathlib import Path

APP_JS = (Path(__file__).parent.parent / "prview" / "static" / "app.js").read_text()


def test_pr_load_defaults_to_overview():
    assert "function initialTab()" in APP_JS
    assert 'return State.standalone ? "review" : "overview";' in APP_JS
    assert "show(initialTab())" in APP_JS


def test_hash_routing_covers_all_tabs():
    assert "hashchange" in APP_JS
    assert 'return State.standalone ? ["review", "repowise"] : ["overview", "review", "repowise"];' in APP_JS


def test_nav_tabs_order_overview_first():
    assert APP_JS.index('mk("overview", "Overview")') < APP_JS.index('mk("review", "Review")')


def test_fenced_blocks_render_preformatted():
    assert "ai-md-pre" in APP_JS


def test_submit_toast_links_to_review():
    assert 'toast("Review submitted", "success", { href: res.url || prUrl() })' in APP_JS
    assert "opts.href" in APP_JS
    assert '"toast-link"' in APP_JS


def test_wrap_lines_toggle_persists_and_applies_data_attribute():
    assert 'const WRAP_KEY = "prview:wrap-lines";' in APP_JS
    assert 'document.documentElement.setAttribute("data-wrap", "on")' in APP_JS
    assert '"fd-view-btn fd-wrap-btn"' in APP_JS


def test_syntax_highlighting_wired_into_diff_and_full_file_views():
    assert "highlight: !!window.hljs" in APP_JS
    assert "}, window.hljs);" in APP_JS
    assert 'window.hljs.getLanguage(ext)' in APP_JS
    assert 'window.hljs.highlight(text, { language: ext, ignoreIllegals: true })' in APP_JS


def test_pr_lifecycle_state_badge_covers_open_merged_closed():
    assert "merged: { g:" in APP_JS
    assert "closed: { g:" in APP_JS
    assert "function prStateGlyph(state)" in APP_JS
    assert '`PR: <span class="${prState.cls}">${prState.g}</span> ${prState.label}`' in APP_JS
    # Submitting a review only makes sense while the PR is still open.
    assert 'submit.disabled = prState.label !== "open";' in APP_JS
