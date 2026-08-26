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
    assert 'const prIsOpen = (pr.state || "").toUpperCase() === "OPEN";' in APP_JS
    assert "submit.disabled = !prIsOpen;" in APP_JS


def test_review_order_defaults_to_story_and_persists():
    assert 'const ORDER_KEY = "prview:review-order";' in APP_JS
    assert 'const DEFAULT_ORDER = "story";' in APP_JS
    assert "localStorage.setItem(ORDER_KEY, mode)" in APP_JS
    # Declared before State, which reads DEFAULT_ORDER in its initializer.
    assert APP_JS.index("const DEFAULT_ORDER") < APP_JS.index("const State = {")


def test_reorder_keeps_the_open_file_and_never_drops_one():
    assert "function applyOrder()" in APP_JS
    assert "if (next.length !== State.files.length) return;" in APP_JS
    assert 'State.files.findIndex((f) => f.filename === open)' in APP_JS
    assert "applyOrder();" in APP_JS


def test_order_control_and_shortcut_are_wired():
    assert '"fl-order-select"' in APP_JS
    assert 'case "o": e.preventDefault(); cycleOrder(); break;' in APP_JS


def test_behavior_grouping_persists_and_is_keyed_separately():
    assert 'const GROUP_KEY = "prview:group-behaviors";' in APP_JS
    assert "localStorage.setItem(GROUP_KEY" in APP_JS


def test_behavior_grouping_is_toggled_by_shift_g():
    assert 'case "G": e.preventDefault(); toggleGrouped(); break;' in APP_JS


def test_behavior_rows_keep_their_flat_file_index():
    # Headers are inserted BETWEEN rows; rows keep data-idx so j/k still work.
    assert "function renderGroupedRows(" in APP_JS
    assert 'row.dataset.idx' in APP_JS


def test_a_file_touched_by_later_behaviors_is_badged():
    assert '"fl-also-in"' in APP_JS
    assert "also in ${" in APP_JS


def test_behavior_comment_posts_to_the_behavior_endpoint():
    assert 'api("POST", "/behaviors/comment"' in APP_JS
    assert "On behavior" in APP_JS or "openBehaviorCommentModal" in APP_JS


def test_behavior_names_is_by_election_not_automatic():
    assert 'api("POST", "/ai/behavior-names"' in APP_JS
    # No call site may fire naming from the grouping load path.
    load = APP_JS[APP_JS.index("async function loadBehaviors("):]
    assert "/ai/behavior-names" not in load[:load.index("\n}\n")]


def test_behavior_naming_uses_the_shared_job_poller_not_a_hand_rolled_interval():
    fn = APP_JS[APP_JS.index("async function startBehaviorNaming("):]
    fn = fn[:fn.index("\n}\n")]
    assert "pollJobId(" in fn
    assert "setInterval" not in fn
    assert "setInterval" not in APP_JS


def test_nav_and_nearest_visible_read_the_rendered_row_order():
    nav = APP_JS[APP_JS.index("function navTo("):]
    nav = nav[:nav.index("\n}\n")]
    assert "State.rowOrder" in nav

    nearest = APP_JS[APP_JS.index("function nearestVisible("):]
    nearest = nearest[:nearest.index("\n}\n")]
    assert "State.rowOrder" in nearest


def test_group_toggle_label_reflects_active_grouping_not_the_preference_alone():
    assert "function groupingActive()" in APP_JS
    toggle = APP_JS[APP_JS.index('const grp = document.createElement("button");'):]
    toggle = toggle[:toggle.index("controls.appendChild(grp);")]
    assert "groupingActive()" in toggle
    assert "State.grouped ?" not in toggle
    assert "String(State.grouped)" not in toggle
