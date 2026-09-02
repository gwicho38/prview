"""Source-level assertions on static/app.js — no JS test infra exists, so the
route-default and hash-routing acceptance criteria are pinned with string
checks. Brittle by design: renaming these symbols must update this test."""
from pathlib import Path

APP_JS = (Path(__file__).parent.parent / "prview" / "static" / "app.js").read_text()
STYLES = (Path(__file__).parent.parent / "prview" / "static" / "styles.css").read_text()


def test_pr_load_defaults_to_overview():
    assert "function initialTab()" in APP_JS
    assert 'return State.standalone ? "review" : "overview";' in APP_JS
    assert "show(initialTab())" in APP_JS


def test_hash_routing_covers_all_tabs():
    assert "hashchange" in APP_JS
    assert 'const tabs = State.standalone ? ["review"] : ["overview", "review"];' in APP_JS
    assert 'if (repowiseAvailable()) tabs.push("repowise");' in APP_JS


def test_a_build_without_a_local_process_offers_no_repowise_tab():
    assert "function repowiseAvailable() { return !window.__prviewNoRepowise; }" in APP_JS
    assert 'if (repowiseAvailable()) tabs.append(mk("repowise", "Repowise"));' in APP_JS


def test_the_overview_runs_on_the_browser_engine_when_that_is_the_engine():
    assert 'if (State.engine === "browser") { await runOverviewInBrowser(); return; }' in APP_JS
    assert 'api("POST", "/ai/prompt", { ...prKey(), kind: "overview" })' in APP_JS
    assert "runBrowserModel(prompt, {" in APP_JS


def test_the_browser_overview_waits_for_a_click_instead_of_downloading_weights():
    assert 'ov.status = "ready";' in APP_JS
    assert '"The overview runs a model in this browser. The first run downloads its weights."' in APP_JS


def test_cancel_reaches_a_locally_running_overview():
    assert "if (localRunId) cancelBrowserModel(localRunId);" in APP_JS


def test_posting_an_overview_carries_the_markdown_for_serverless_builds():
    assert 'api("POST", "/overview/comment", { ...prKey(), markdown: ovState().markdown })' in APP_JS


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
    assert 'case "G": e.preventDefault(); if (!State.standalone) toggleGrouped(); break;' in APP_JS


def test_toggle_grouped_derives_intent_from_active_grouping_not_the_flag():
    fn = APP_JS[APP_JS.index("async function toggleGrouped("):]
    fn = fn[:fn.index("\n}\n")]
    assert "groupingActive()" in fn
    assert "!State.grouped" not in fn


def test_behavior_rows_keep_their_flat_file_index():
    # Headers are inserted BETWEEN rows; rows keep data-idx so j/k still work.
    assert "function renderGroupedRows(" in APP_JS
    assert 'row.dataset.idx' in APP_JS


def test_a_file_touched_by_later_behaviors_is_badged():
    assert '"fl-also-in"' in APP_JS
    assert "also in ${" in APP_JS


def test_behavior_comment_posts_to_the_behavior_endpoint():
    assert 'api("POST", "/behaviors/comment"' in APP_JS
    assert "Comment on behavior" in APP_JS


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


def test_behavior_naming_control_is_a_play_stop_toggle_that_clears_the_job_id():
    start = APP_JS[APP_JS.index("async function startBehaviorNaming("):]
    start = start[:start.index("\n}\n")]
    assert start.count("_behaviorNameJobId = null;") == 2  # onDone and onError

    stop = APP_JS[APP_JS.index("async function stopBehaviorNaming("):]
    stop = stop[:stop.index("\n}\n")]
    assert "/job/${jobId}/cancel" in stop

    name_btn = APP_JS[APP_JS.index('name.className = "btn btn-ghost fl-behavior-name";'):]
    name_btn = name_btn[:name_btn.index("controls.appendChild(name);")]
    assert '"■ Stop"' in name_btn
    assert '"▶ Name"' in name_btn
    assert "stopBehaviorNaming" in name_btn


def test_nav_and_nearest_visible_read_the_rendered_row_order():
    nav = APP_JS[APP_JS.index("function navTo("):]
    nav = nav[:nav.index("\n}\n")]
    assert "State.rowOrder" in nav

    nearest = APP_JS[APP_JS.index("function nearestVisible("):]
    nearest = nearest[:nearest.index("\n}\n")]
    assert "State.rowOrder" in nearest


def test_collapse_and_hide_tests_share_the_visible_row_helper():
    assert "function keepCurrentVisible()" in APP_JS

    caret = APP_JS[APP_JS.index('caret.addEventListener("click"'):]
    caret = caret[:caret.index("\n    });\n")]
    assert "keepCurrentVisible();" in caret

    fn = APP_JS[APP_JS.index("function toggleHideTests("):]
    fn = fn[:fn.index("\n}\n")]
    assert "keepCurrentVisible();" in fn


def test_a_transient_load_failure_lets_the_next_toggle_retry():
    fn = APP_JS[APP_JS.index("async function loadBehaviors("):]
    fn = fn[:fn.index("\n}\n")]
    catch_block = fn[fn.index("catch (e) {"):]
    assert "State.behaviors = null;" in catch_block


def test_ungroupable_disables_the_group_toggle():
    toggle = APP_JS[APP_JS.index('const grp = document.createElement("button");'):]
    toggle = toggle[:toggle.index("controls.appendChild(grp);")]
    assert "grp.disabled = ungroupable;" in toggle
    assert "Single commit" in toggle


def test_group_toggle_label_reflects_active_grouping_not_the_preference_alone():
    assert "function groupingActive()" in APP_JS
    toggle = APP_JS[APP_JS.index('const grp = document.createElement("button");'):]
    toggle = toggle[:toggle.index("controls.appendChild(grp);")]
    assert "groupingActive()" in toggle
    assert "State.grouped ?" not in toggle
    assert "String(State.grouped)" not in toggle


def test_both_comment_scopes_share_one_composer():
    # Two near-identical modals drifted apart once; one composer is the fix.
    assert "function openCommentComposer(" in APP_JS
    for fn in ("function openCommentModal()", "function openBehaviorCommentModal(b)"):
        body = APP_JS[APP_JS.index(fn):]
        body = body[:body.index("\n}\n")]
        assert "openCommentComposer({" in body, fn
        assert "createElement(\"textarea\")" not in body, fn


def test_browser_engine_is_opt_in_and_persisted():
    assert 'const ENGINE_KEY = "prview:ai-engine";' in APP_JS
    assert "function browserEngineAvailable() { return !!navigator.gpu; }" in APP_JS
    # Locally the default stays claude; the hosted build overrides it.
    assert 'return window.__prviewDefaultEngine || "claude";' in APP_JS


def test_an_engine_that_cannot_answer_here_is_not_selectable():
    # The hosted page has no local process, so offering claude as the default sent
    # a first-time visitor's first click straight into an error.
    assert "function claudeAvailable() { return !window.__prviewNoClaude; }" in APP_JS
    assert 'opt.disabled = value === "claude" && !claudeAvailable();' in APP_JS
    saved = APP_JS[APP_JS.index("function savedEngine()"):]
    saved = saved[:saved.index("\n}\n")]
    assert "claudeAvailable()" in saved, "a stored claude choice must not survive where claude cannot run"


def test_browser_engine_reuses_the_servers_prompt_builders():
    # Both engines must ask the model the same thing; a second prompt copy in JS
    # is the drift this endpoint exists to prevent.
    body = APP_JS[APP_JS.index("async function launchBrowserJob("):]
    body = body[:body.index("\n}\n")]
    assert 'api("POST", "/ai/prompt"' in body
    assert "build_summary_prompt" not in APP_JS


def test_browser_generation_runs_in_a_worker_not_the_main_thread():
    assert "new Worker(" in APP_JS and "llm-worker.js" in APP_JS
    assert "import(WEBLLM)" not in APP_JS, "the model must be imported in the worker, not app.js"


def test_cancel_reaches_the_browser_engine_too():
    body = APP_JS[APP_JS.index("async function cancelJob(path)"):]
    body = body[:body.index("\n}\n")]
    assert "cancelBrowserModel(ai.localRunId)" in body


def test_the_default_browser_model_is_not_the_rough_one():
    # The 1.5B described a bottom-following fix as keeping content "at the top" and
    # claimed added logic where the diff removed it. Default to one that can be trusted.
    first = APP_JS[APP_JS.index("const BROWSER_MODELS = ["):]
    first = first[:first.index("];")]
    assert first.index("Qwen2.5-Coder-7B") < first.index("Qwen2.5-Coder-1.5B")


def test_the_ui_can_run_without_a_server_behind_it():
    # The hosted Pages build swaps the transport, not the UI.
    assert "if (window.__prviewTransport) return window.__prviewTransport(method, path, body);" in APP_JS


def test_the_model_worker_url_is_overridable_and_load_failures_surface():
    assert 'new Worker(window.__prviewWorkerUrl || "/static/llm-worker.js")' in APP_JS
    assert "_llm.onerror" in APP_JS, "a worker that 404s must reject its waiters, not hang"


def test_a_hidden_button_is_actually_hidden():
    # The stylesheet opts each class into the hidden attribute one at a time, and
    # .btn sets display: inline-flex, which outranks the browser default.
    assert ".btn[hidden] { display: none; }" in STYLES
