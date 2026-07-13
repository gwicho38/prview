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
