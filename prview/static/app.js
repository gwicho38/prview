"use strict";
/* prview frontend — vanilla JS, no build step, zero external network requests.
 * diff2html is vendored under /static/vendor and exposed as window.Diff2HtmlUI. */

// ----------------------------------------------------------------------------
// Session token: captured once from ?token= on load, sent on every API call.
// ----------------------------------------------------------------------------
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const TOKEN_HEADER = "X-Prview-Token";
// Strip the token from the visible URL so it doesn't linger in browser history
// or leak via Referer. It's already captured above and sent as a header.
if (TOKEN && window.history && history.replaceState) {
  const _u = new URL(location.href);
  _u.searchParams.delete("token");
  history.replaceState(null, "", _u.pathname + _u.search + _u.hash);
}

// ----------------------------------------------------------------------------
// In-memory app state (single working copy; rehydrated from server on load).
// ----------------------------------------------------------------------------
const State = {
  pr: null,            // PRInfoModel
  files: [],           // [FileListItem] (server-sorted by change size desc)
  review: null,        // ReviewStateModel {viewed[], flagged{}, comments, submitted}
  idx: 0,              // current file index
  detailCache: {},     // path -> FileDetail
  ai: {},              // path -> {mode, status, jobId, result, qa, timer, t0, prevMode}
  rw: null,            // repowise prepare state for the current PR (see Repowise)
};

function prKey() {
  const { owner, repo, number } = State.pr;
  return { owner, repo, number };
}

// ----------------------------------------------------------------------------
// API client. Attaches token, parses {error,hint}, retries once on 409 by
// re-issuing POST /pr (server cache went stale after a restart).
// ----------------------------------------------------------------------------
class ApiError extends Error {
  constructor(status, error, hint) {
    super(error || `HTTP ${status}`);
    this.status = status;
    this.hint = hint || "";
  }
}

async function rawFetch(method, path, body) {
  const headers = { [TOKEN_HEADER]: TOKEN };
  const opts = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch { data = { error: text }; } }
  if (!res.ok) {
    const err = data || {};
    throw new ApiError(res.status, err.error || res.statusText, err.hint);
  }
  return data;
}

let reloading = null;
async function reloadCurrentPr() {
  if (!State.pr) throw new ApiError(409, "No PR loaded");
  if (!reloading) {
    reloading = rawFetch("POST", "/pr", { ref: refString() }).finally(() => { reloading = null; });
  }
  return reloading;
}

async function api(method, path, body, { retryOn409 = true } = {}) {
  try {
    return await rawFetch(method, path, body);
  } catch (e) {
    if (retryOn409 && e instanceof ApiError && e.status === 409 && State.pr) {
      await reloadCurrentPr();
      return rawFetch(method, path, body);
    }
    throw e;
  }
}

function refString() {
  const { owner, repo, number } = State.pr;
  return `${owner}/${repo}#${number}`;
}

// ----------------------------------------------------------------------------
// Router — toggles the three screens.
// ----------------------------------------------------------------------------
const Screens = {
  landing: document.getElementById("screen-landing"),
  review: document.getElementById("screen-review"),
  repowise: document.getElementById("screen-repowise"),
  submit: document.getElementById("screen-submit"),
};
let activeScreen = "landing";

function show(screen) {
  activeScreen = screen;
  for (const [name, el] of Object.entries(Screens)) el.hidden = name !== screen;
  if (screen === "landing") loadResumeList();
  if (screen === "submit") renderSubmit();
  if (screen === "review") renderSummary();
  if (screen === "repowise") { renderSummary(); renderRepowise(); }
}

// ----------------------------------------------------------------------------
// Toast + ARIA live announcements.
// ----------------------------------------------------------------------------
function toast(message, kind = "success") {
  const stack = document.getElementById("toast-stack");
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " toast-error" : "");
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 3200);
  announce(message);
}
function announce(message) {
  const live = document.getElementById("aria-live");
  live.textContent = "";
  requestAnimationFrame(() => { live.textContent = message; });
}

// ----------------------------------------------------------------------------
// PR-reference validation (mirror the two accepted shapes; reject bare number).
// ----------------------------------------------------------------------------
const SHORT_RE = /^[^/\s]+\/[^/#\s]+#\d+$/;             // owner/repo#123
const URL_RE = /github\.com\/[^/\s]+\/[^/\s]+\/pull\/\d+/i; // full URL
function validRef(raw) {
  const v = raw.trim();
  if (!v) return false;
  return SHORT_RE.test(v) || URL_RE.test(v);
}

// ============================================================================
// screen:landing
// ============================================================================
const refInput = document.getElementById("pr-ref-input");
const refError = document.getElementById("pr-ref-error");
const loadBtn = document.getElementById("load-pr-btn");

function setRefError(msg) {
  if (msg) { refError.textContent = msg; refError.hidden = false; refInput.classList.add("invalid"); }
  else { refError.hidden = true; refInput.classList.remove("invalid"); }
}

async function submitRef() {
  const raw = refInput.value;
  if (!validRef(raw)) {
    setRefError("Enter owner/repo#123 or a full GitHub PR URL (a bare number is not enough).");
    return;
  }
  setRefError("");
  loadBtn.disabled = true;
  loadBtn.textContent = "Loading…";
  try {
    const data = await api("POST", "/pr", { ref: raw.trim() }, { retryOn409: false });
    enterReview(data);
  } catch (e) {
    setRefError(e.message + (e.hint ? ` — ${e.hint}` : ""));
  } finally {
    loadBtn.disabled = false;
    loadBtn.innerHTML = 'Load PR <span aria-hidden="true">↵</span>';
  }
}

loadBtn.addEventListener("click", submitRef);
refInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submitRef(); });
refInput.addEventListener("input", () => setRefError(""));
document.getElementById("brand").addEventListener("click", () => show("landing"));

async function loadResumeList() {
  const list = document.getElementById("resume-list");
  list.innerHTML = "";
  let rows = [];
  try { rows = await api("GET", "/reviews"); } catch { rows = []; }
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "resume-empty";
    empty.textContent = "No saved reviews yet. Load a PR to begin.";
    list.appendChild(empty);
    return;
  }
  for (const r of rows) {
    const ref = `${r.owner}/${r.repo}#${r.number}`;
    const btn = document.createElement("button");
    btn.className = "resume-row";
    btn.setAttribute("role", "listitem");

    const refEl = document.createElement("span");
    refEl.className = "resume-ref";
    refEl.textContent = ref;
    btn.appendChild(refEl);

    const meta = document.createElement("span");
    meta.className = "resume-meta";
    if (r.submitted) {
      const badge = document.createElement("span");
      badge.className = "badge-done";
      badge.textContent = "DONE · submitted ✓";
      meta.appendChild(badge);
    } else {
      const total = r.total != null ? `/${r.total}` : "";
      meta.textContent = `${r.viewed_count}${total} viewed · ${r.flagged_count} flagged`;
    }
    btn.appendChild(meta);

    const arrow = document.createElement("span");
    arrow.className = "resume-arrow";
    arrow.textContent = "→";
    arrow.setAttribute("aria-hidden", "true");
    btn.appendChild(arrow);

    btn.addEventListener("click", () => resumeReview(r.owner, r.repo, r.number));
    list.appendChild(btn);
  }
}

async function resumeReview(owner, repo, number) {
  try {
    const data = await api("GET", `/pr/${owner}/${repo}/${number}`);
    enterReview(data);
  } catch (e) {
    toast(e.message, "error");
  }
}

// ============================================================================
// screen:review — load data into state, render regions, pick starting file.
// ============================================================================
function enterReview(data) {
  State.pr = data.pr;
  State.files = data.files;
  State.review = data.review || data.state; // server key is `state`
  State.detailCache = {};
  State.ai = {};
  State.rw = null;
  applyReviewToFiles();
  State.idx = firstUnviewedIndex();
  show("review");
  renderSummary();
  renderFileList();
  selectFile(State.idx);
}

function applyReviewToFiles() {
  const viewed = new Set(State.review.viewed || []);
  const flagged = State.review.flagged || {};
  const threads = State.review.comment_threads || {};
  for (const f of State.files) {
    f.viewed = viewed.has(f.filename);
    f.flagged = Object.prototype.hasOwnProperty.call(flagged, f.filename);
    f.flag_note = flagged[f.filename] || "";
    f.comments = (threads[f.filename] || []).map((c) =>
      typeof c === "string" ? { text: c, line: null, start_line: null } : c);
  }
}

function firstUnviewedIndex() {
  const i = State.files.findIndex((f) => !f.viewed);
  return i === -1 ? 0 : i;
}

function viewedCount() { return State.files.filter((f) => f.viewed).length; }
function flaggedCount() { return State.files.filter((f) => f.flagged).length; }

// ---- component:pr-summary --------------------------------------------------
const CI_GLYPH = {
  passing: { g: "●", cls: "glyph-pass", label: "passing" },
  success: { g: "●", cls: "glyph-pass", label: "passing" },
  failing: { g: "●", cls: "glyph-fail", label: "failing" },
  failure: { g: "●", cls: "glyph-fail", label: "failing" },
  pending: { g: "◷", cls: "glyph-pending", label: "pending" },
};
const CI_NONE = { g: "○", cls: "glyph-none", label: "none" };
function ciGlyph(status) { return CI_GLYPH[(status || "").toLowerCase()] || CI_NONE; }

const DECISION_GLYPH = {
  approved: { g: "✓", cls: "glyph-approved", label: "approved" },
  changes_requested: { g: "✗", cls: "glyph-changes", label: "changes requested" },
};
const DECISION_NONE = { g: "◷", cls: "glyph-none", label: "none yet" };
function decisionGlyph(d) { return DECISION_GLYPH[(d || "").toLowerCase()] || DECISION_NONE; }

// nav-tabs (Review / Repowise): a tablist that drives the router. Active tab is
// glyph (▸) + accent underline + accent color — never color alone. The `g`
// keycap mirrors the existing single-key shortcut affordances.
function buildNavTabs() {
  const tabs = document.createElement("div");
  tabs.className = "nav-tabs";
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "Review or Repowise");
  const active = activeScreen === "repowise" ? "repowise" : "review";
  const mk = (key, label) => {
    const b = document.createElement("button");
    b.className = "nav-tab";
    b.setAttribute("role", "tab");
    const selected = key === active;
    b.setAttribute("aria-selected", selected ? "true" : "false");
    const mark = document.createElement("span");
    mark.className = "nav-tab-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = selected ? "▸" : "";
    const txt = document.createElement("span");
    txt.textContent = label;
    b.append(mark, txt);
    b.addEventListener("click", () => { if (key !== active) show(key); });
    return b;
  };
  tabs.append(mk("review", "Review"), mk("repowise", "Repowise"));
  const hint = document.createElement("span");
  hint.className = "kbd nav-tabs-hint";
  hint.textContent = "g";
  const wrap = document.createElement("span");
  wrap.style.display = "inline-flex";
  wrap.style.alignItems = "center";
  wrap.style.gap = "6px";
  wrap.append(tabs, hint);
  return wrap;
}

function renderSummary() {
  if (!State.pr) return;
  const pr = State.pr;
  const ci = ciGlyph(pr.ci_status);
  const dec = decisionGlyph(pr.review_decision);
  const el = document.getElementById(
    activeScreen === "repowise" ? "pr-summary-repowise" : "pr-summary");
  if (!el) return;
  el.innerHTML = "";

  const drawerBtn = document.createElement("button");
  drawerBtn.className = "btn btn-ghost drawer-toggle";
  drawerBtn.textContent = "☰ Files";
  drawerBtn.addEventListener("click", () => document.body.classList.toggle("drawer-open"));

  const title = document.createElement("div");
  title.className = "ps-title";
  title.append(drawerBtn);
  const ref = document.createElement("span");
  ref.className = "ps-ref";
  ref.textContent = ` ${refString()}  ·  `;
  title.append(ref, document.createTextNode(pr.title || "(untitled)"));

  const meta = document.createElement("div");
  meta.className = "ps-meta";
  const branch = document.createElement("span");
  branch.className = "ps-branch";
  branch.textContent = `${pr.base || "?"} ← ${pr.head || "?"}`;
  meta.append(`by @${pr.author || "unknown"}   `, branch);
  meta.append(`   ·  ${State.files.length} files   `);
  const adds = document.createElement("span"); adds.className = "ps-adds"; adds.textContent = `+${pr.additions}`;
  const dels = document.createElement("span"); dels.className = "ps-dels"; dels.textContent = `−${pr.deletions}`;
  meta.append(adds, " / ", dels);

  const statusRow = document.createElement("div");
  statusRow.className = "ps-status-row";
  const ciEl = document.createElement("span");
  ciEl.innerHTML = `CI: <span class="${ci.cls}">${ci.g}</span> ${ci.label}`;
  const decEl = document.createElement("span");
  decEl.innerHTML = `Review: <span class="${dec.cls}">${dec.g}</span> ${dec.label}`;
  const submit = document.createElement("button");
  submit.className = "btn btn-primary submit-entry";
  submit.innerHTML = 'Submit <span class="kbd">s</span>';
  submit.addEventListener("click", () => show("submit"));
  // nav-tabs sit before .submit-entry so .submit-entry's margin-left:auto still
  // pins Submit to the far right.
  statusRow.append(ciEl, decEl, buildNavTabs(), submit);

  el.append(title, meta, statusRow);
}

// ---- component:file-list ---------------------------------------------------
function renderFileList() {
  const el = document.getElementById("file-list");
  el.innerHTML = "";

  const total = State.files.length;
  const done = viewedCount();
  const pct = total ? Math.round((done / total) * 100) : 0;

  const prog = document.createElement("div");
  prog.className = "fl-progress";
  const count = document.createElement("div");
  count.className = "fl-count";
  count.textContent = `${done}/${total} viewed`;
  const track = document.createElement("div");
  track.className = "progress-track";
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", String(total));
  track.setAttribute("aria-valuenow", String(done));
  const fill = document.createElement("div");
  fill.className = "progress-fill";
  fill.style.width = pct + "%";
  track.appendChild(fill);
  prog.append(count, track);

  const rows = document.createElement("div");
  rows.className = "fl-rows";
  State.files.forEach((f, i) => {
    const row = document.createElement("button");
    row.className = "fl-row" + (i === State.idx ? " current" : "");
    row.dataset.idx = i;

    const marker = document.createElement("span");
    marker.className = "fl-marker";
    marker.textContent = i === State.idx ? "▸" : "";
    marker.setAttribute("aria-hidden", "true");

    // Lead with the basename (always fully visible); the directory is dimmed and
    // truncates first, so files with a shared prefix stay distinguishable.
    const name = document.createElement("span");
    name.className = "fl-name";
    name.title = f.filename;
    const slash = f.filename.lastIndexOf("/");
    const base = document.createElement("span");
    base.className = "fl-base";
    base.textContent = slash >= 0 ? f.filename.slice(slash + 1) : f.filename;
    name.appendChild(base);
    if (slash >= 0) {
      const dir = document.createElement("span");
      dir.className = "fl-dir";
      dir.textContent = f.filename.slice(0, slash);
      name.appendChild(dir);
    }

    const counts = document.createElement("span");
    counts.className = "fl-counts";
    counts.innerHTML = `<span class="ps-adds">+${f.additions}</span> <span class="ps-dels">−${f.deletions}</span>`;

    const badges = document.createElement("span");
    badges.className = "fl-badges";
    if (f.viewed) badges.insertAdjacentHTML("beforeend", '<span class="badge badge-viewed" title="viewed">✓</span>');
    if (f.flagged) badges.insertAdjacentHTML("beforeend", '<span class="badge badge-flagged" title="flagged">⚑</span>');

    row.append(marker, name, counts, badges);
    row.addEventListener("click", () => selectFile(i));
    rows.appendChild(row);
  });

  const legend = document.createElement("div");
  legend.className = "fl-legend";
  legend.innerHTML =
    '<span class="badge-viewed">✓ viewed</span>' +
    '<span class="badge-flagged">⚑ flagged</span>' +
    '<span class="fl-marker">▸ current</span>';

  el.append(prog, rows, legend);
}

function currentFile() { return State.files[State.idx]; }

function navTo(delta) {
  if (!State.files.length) return;
  let i = State.idx + delta;
  i = Math.max(0, Math.min(State.files.length - 1, i));
  selectFile(i);
}

function selectFile(i) {
  if (i < 0 || i >= State.files.length) return;
  State.idx = i;
  renderFileList();
  renderFileDetail();
  const row = document.querySelector(`.fl-row[data-idx="${i}"]`);
  if (row) row.scrollIntoView({ block: "nearest" });
  document.body.classList.remove("drawer-open");
}

// ============================================================================
// component:file-detail — header, action bar, ai-panel, diff (lazy per file)
// ============================================================================
// A comment is {text, line, start_line}; line-anchored ones (line != null) are
// rendered inline at their diff line (see injectInlineComments), so the
// file-level area shows only the unanchored (file-scoped) comments.
function commentLabel(c) {
  if (c.line == null) return "";
  return c.start_line != null && c.start_line !== c.line
    ? `lines ${c.start_line}–${c.line}` : `line ${c.line}`;
}

function makeBubble(c) {
  // Comment text is user-authored → textContent only (never innerHTML), XSS-safe.
  const bubble = document.createElement("div");
  bubble.className = "comment-bubble";
  bubble.textContent = c.text;
  return bubble;
}

// Fills the file-level area with bubbles for file-scoped comments (no line).
function renderCommentBubbles(container, f) {
  container.innerHTML = "";
  const list = (f.comments || []).filter((c) => c.line == null);
  container.hidden = list.length === 0;
  if (!list.length) return;
  const title = document.createElement("div");
  title.className = "fd-comments-title";
  title.textContent = list.length === 1 ? "Your comment" : `Your comments (${list.length})`;
  container.appendChild(title);
  for (const c of list) container.appendChild(makeBubble(c));
}

// After the diff renders, drop each line-anchored comment into a row directly
// beneath its target new-side line — mirroring GitHub's inline review threads.
function injectInlineComments(region, f) {
  const anchored = (f && f.comments || []).filter((c) => c.line != null);
  if (!anchored.length) return;
  // Map each new-side line number → its diff row.
  const rowByLine = new Map();
  for (const row of region.querySelectorAll("tr")) {
    const num = row.querySelector(".line-num2");
    const n = num && parseInt(num.textContent.trim(), 10);
    if (Number.isInteger(n)) rowByLine.set(n, row);
  }
  for (const c of anchored) {
    const row = rowByLine.get(c.line);
    if (!row) continue;
    const tr = document.createElement("tr");
    tr.className = "comment-row";
    const td = document.createElement("td");
    td.colSpan = 2;
    const bubble = makeBubble(c);
    const lbl = commentLabel(c);
    if (lbl) {
      const tag = document.createElement("div");
      tag.className = "fd-comments-title";
      tag.textContent = lbl;
      td.append(tag, bubble);
    } else {
      td.appendChild(bubble);
    }
    tr.appendChild(td);
    row.after(tr);
  }
}

// New-side line range covered by the current diff text selection, or null.
// Captured when the comment modal opens (focusing it would clear the selection).
function selectedNewLineRange() {
  const sel = window.getSelection();
  const region = document.getElementById("diff-region");
  if (!sel || sel.isCollapsed || !sel.rangeCount || !region) return null;
  if (!region.contains(sel.getRangeAt(0).commonAncestorContainer)) return null;
  const nums = [];
  for (const row of region.querySelectorAll("tr")) {
    const code = row.querySelector(".d2h-code-line, .d2h-code-side-line");
    if (!code || !sel.containsNode(code, true)) continue;
    const num = row.querySelector(".line-num2");
    const n = num && parseInt(num.textContent.trim(), 10);
    if (Number.isInteger(n)) nums.push(n);
  }
  if (!nums.length) return null;
  return { start: Math.min(...nums), end: Math.max(...nums) };
}

function renderFileDetail() {
  const el = document.getElementById("file-detail");
  el.innerHTML = "";
  const f = currentFile();
  if (!f) {
    const empty = document.createElement("div");
    empty.className = "fd-empty";
    empty.textContent = "No files in this PR.";
    el.appendChild(empty);
    return;
  }

  const header = document.createElement("div");
  header.className = "fd-header";
  const name = document.createElement("span"); name.className = "fd-name"; name.textContent = f.filename;
  const counts = document.createElement("span");
  counts.innerHTML = `<span class="ps-adds">+${f.additions}</span> <span class="ps-dels">−${f.deletions}</span>`;
  header.append(name, counts);

  const aiPanel = document.createElement("div");
  aiPanel.className = "ai-panel";
  aiPanel.id = "ai-panel";

  const comments = document.createElement("div");
  comments.className = "fd-comments";
  comments.id = "fd-comments";
  renderCommentBubbles(comments, f);

  const diffRegion = document.createElement("div");
  diffRegion.className = "diff-region";
  diffRegion.id = "diff-region";
  diffRegion.innerHTML = '<div class="diff-loading"><span class="spinner"></span> Loading diff…</div>';

  const actions = buildActionBar(f);

  // Scrolling content (header + AI panel + comments + diff) lives in its own
  // region; the action bar is a fixed-height row beneath it, so it never
  // overlaps the diff.
  const scroll = document.createElement("div");
  scroll.className = "fd-scroll";
  scroll.append(header, aiPanel, comments, diffRegion);
  el.append(scroll, actions);

  loadDiff(f.filename, diffRegion);
  startSummary(f.filename, false); // auto-submit summary on select
}

function buildActionBar(f) {
  const bar = document.createElement("div");
  bar.className = "action-bar";
  const label = document.createElement("span");
  label.className = "ab-label";
  label.textContent = "Actions:";
  bar.appendChild(label);

  const mk = (text, kbd, handler) => {
    const b = document.createElement("button");
    b.className = "btn btn-ghost";
    b.innerHTML = `${text} <span class="kbd">${kbd}</span>`;
    b.addEventListener("click", handler);
    return b;
  };
  bar.append(
    mk(f.viewed ? "Viewed ✓" : "Viewed", "v", markViewed),
    mk("Explain", "e", () => startExplain(currentFile().filename)),
    mk("Ask", "a", () => focusAsk()),
    mk("Comment", "c", openCommentModal),
    mk("Flag", "f", openFlagModal),
  );
  return bar;
}

// ---- diff (lazy, vendored diff2html) --------------------------------------
async function loadDiff(path, region) {
  let detail = State.detailCache[path];
  try {
    if (!detail) {
      const { owner, repo, number } = prKey();
      detail = await api("GET", `/pr/${owner}/${repo}/${number}/file?path=${encodeURIComponent(path)}`);
      State.detailCache[path] = detail;
    }
  } catch (e) {
    region.innerHTML = "";
    const err = document.createElement("div");
    err.className = "diff-placeholder";
    err.textContent = `Could not load diff: ${e.message}`;
    region.appendChild(err);
    return;
  }
  if (path !== currentFile().filename) return; // user navigated away mid-load
  renderDiff(detail, region);
}

function renderDiff(detail, region) {
  region.innerHTML = "";
  const text = (detail.diff_text || "").trim();
  if (!text) {
    const ph = document.createElement("div");
    ph.className = "diff-placeholder";
    ph.textContent = "binary file changed";
    region.appendChild(ph);
    return;
  }
  try {
    const ui = new window.Diff2HtmlUI(region, text, {
      drawFileList: false,
      matching: "words",        // word-level inline highlights, not whole-line blocks
      // Unified, not side-by-side: a mostly-additive diff (e.g. +263/-2) renders
      // side-by-side with one empty placeholder row per added line, producing a
      // huge blank column. Line-by-line keeps it compact with no wasted space.
      outputFormat: "line-by-line",
      colorScheme: "dark",      // GitHub-dark palette (matches app theme) — readable contrast
      highlight: false,         // vendored ui-base bundle has no highlight.js; would throw
    });
    ui.draw();
    injectInlineComments(region, currentFile());
  } catch {
    const ph = document.createElement("div");
    ph.className = "diff-placeholder";
    ph.textContent = "binary file changed";
    region.appendChild(ph);
  }
}

// ============================================================================
// Inline "explain selection": highlight text in the diff → floating button →
// AI explanation in a popover anchored to the selection (independent of the
// per-file AI panel).
// ============================================================================
const SelExplain = { btn: null, pop: null, jobId: null, alive: false };

function selPopoverOpen() { return !!SelExplain.pop; }

// Returns {text, rect} if there's a non-trivial selection inside #diff-region.
function diffSelection() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
  const region = document.getElementById("diff-region");
  if (!region || !region.contains(sel.anchorNode)) return null;
  const text = sel.toString().trim();
  if (text.length < 3) return null;
  return { text, rect: sel.getRangeAt(0).getBoundingClientRect() };
}

function hideSelExplainBtn() {
  if (SelExplain.btn) { SelExplain.btn.remove(); SelExplain.btn = null; }
}

function refreshSelExplainBtn() {
  if (SelExplain.pop) return; // popover open — don't re-show the button
  const info = diffSelection();
  if (!info) { hideSelExplainBtn(); return; }
  hideSelExplainBtn();
  const b = document.createElement("button");
  b.className = "sel-explain-btn";
  b.textContent = "✨ Explain selection";
  b.style.top = `${Math.max(8, info.rect.top - 38)}px`;
  b.style.left = `${Math.max(8, info.rect.left)}px`;
  // mousedown (not click): preventDefault keeps the selection alive so we can
  // read it, and stopPropagation avoids the outside-click dismiss handler.
  b.addEventListener("mousedown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    openSelPopover(info.text, info.rect);
  });
  document.body.appendChild(b);
  SelExplain.btn = b;
}

function closeSelPopover() {
  SelExplain.alive = false;
  SelExplain.jobId = null;
  if (SelExplain.pop) { SelExplain.pop.remove(); SelExplain.pop = null; }
}

function openSelPopover(selection, rect) {
  hideSelExplainBtn();
  closeSelPopover(); // a new selection-explain replaces any prior popover
  const pop = document.createElement("div");
  pop.className = "sel-popover";
  pop.style.top = `${Math.min(window.innerHeight - 240, rect.bottom + 8)}px`;
  pop.style.left = `${Math.min(window.innerWidth - 436, Math.max(8, rect.left))}px`;

  const head = document.createElement("div");
  head.className = "sel-pop-head";
  const ttl = document.createElement("span");
  ttl.textContent = "AI · Explain selection";
  const close = document.createElement("button");
  close.className = "sel-pop-close";
  close.setAttribute("aria-label", "Close");
  close.textContent = "✕";
  close.addEventListener("click", closeSelPopover);
  head.append(ttl, close);

  const body = document.createElement("div");
  body.className = "sel-pop-body";
  body.innerHTML = '<div class="ai-loading"><span class="spinner"></span> Explaining…</div>';

  pop.append(head, body);
  document.body.appendChild(pop);
  SelExplain.pop = pop;
  SelExplain.alive = true;
  runSelExplain(selection, body);
}

async function runSelExplain(selection, body) {
  const f = currentFile();
  if (!f) { closeSelPopover(); return; }
  const setError = (msg) => {
    body.innerHTML = "";
    const e = document.createElement("div");
    e.className = "ai-error";
    e.textContent = `⚠ ${msg}`;
    body.appendChild(e);
  };
  try {
    const { job_id } = await api("POST", "/ai/explain-selection",
      { ...prKey(), path: f.filename, selection });
    SelExplain.jobId = job_id;
    pollJobId(job_id, {
      alive: () => SelExplain.alive && SelExplain.jobId === job_id,
      onDone: (snap) => {
        body.innerHTML = "";
        const d = document.createElement("div");
        d.className = "ai-body";
        renderMarkdown(d, snap.result || "");
        body.appendChild(d);
      },
      onError: (snap) => setError(snap.error || "claude call failed"),
    });
  } catch (e) {
    setError(e.message + (e.hint ? ` — ${e.hint}` : ""));
  }
}

let _selExplainInit = false;
function initSelExplain() {
  if (_selExplainInit) return;
  _selExplainInit = true;
  document.addEventListener("selectionchange", () => requestAnimationFrame(refreshSelExplainBtn));
  // Dismiss the popover when clicking outside it.
  document.addEventListener("mousedown", (e) => {
    if (SelExplain.pop && !SelExplain.pop.contains(e.target)) closeSelPopover();
  });
}
initSelExplain();

// ============================================================================
// component:ai-panel — submit→poll→result/error/cancel; live-region announce
// ============================================================================
const POLL_MS = 2000;
const MAX_NOTE = "may take up to 5 min";

function aiFor(path) {
  if (!State.ai[path]) State.ai[path] = { mode: "summary", status: "idle", jobId: null, result: "", qa: null };
  return State.ai[path];
}

function isCurrent(path) { return currentFile() && currentFile().filename === path; }

function startSummary(path, force) {
  const ai = aiFor(path);
  if (!force && ai.status === "done" && ai.mode === "summary" && ai.result) {
    renderAiPanel(path);
    return;
  }
  if (!force && ai.status === "running" && ai.mode === "summary") {
    renderAiPanel(path);
    return;
  }
  ai.mode = "summary";
  ai.qa = null;
  launchJob(path, "/ai/summary", { ...prKey(), path });
}

function startExplain(path) {
  const ai = aiFor(path);
  ai.prevMode = ai.mode;
  ai.mode = "explain";
  launchJob(path, "/ai/explain", { ...prKey(), path });
}

function startAsk(path, question) {
  const ai = aiFor(path);
  ai.prevMode = ai.mode;
  ai.mode = "ask";
  ai.question = question;
  launchJob(path, "/ai/ask", { ...prKey(), path, question });
}

async function launchJob(path, endpoint, body) {
  const ai = aiFor(path);
  ai.status = "running";
  ai.error = "";
  ai.t0 = Date.now();
  if (isCurrent(path)) renderAiPanel(path);
  try {
    const { job_id } = await api("POST", endpoint, body);
    ai.jobId = job_id;
    pollJob(path);
  } catch (e) {
    ai.status = "error";
    ai.error = e.message + (e.hint ? ` — ${e.hint}` : "");
    if (isCurrent(path)) renderAiPanel(path);
  }
}

// Generic poller over the /job/{id} contract. `alive()` returning false stops it
// (cancel / dismiss). One implementation, reused by the per-file AI panel and the
// selection-explain popover — no second polling loop.
function pollJobId(jobId, { onTick, onDone, onError, alive }) {
  const tick = async () => {
    if (alive && !alive()) return;
    let snap;
    try {
      snap = await api("GET", `/job/${jobId}`);
    } catch {
      setTimeout(tick, POLL_MS); // transient; keep polling
      return;
    }
    if (alive && !alive()) return;
    if (snap.status === "done") onDone(snap);
    else if (snap.status === "error") onError(snap);
    else { if (onTick) onTick(snap); setTimeout(tick, POLL_MS); }
  };
  setTimeout(tick, POLL_MS);
}

function pollJob(path) {
  const ai = aiFor(path);
  if (ai.status !== "running" || !ai.jobId) return;
  pollJobId(ai.jobId, {
    alive: () => ai.status === "running",
    onTick: (snap) => { ai.elapsed = snap.elapsed; if (isCurrent(path)) renderAiPanel(path); },
    onDone: (snap) => {
      ai.elapsed = snap.elapsed;
      ai.status = "done";
      if (ai.mode === "ask") ai.qa = { q: ai.question, a: snap.result || "" };
      else ai.result = snap.result || "";
      if (isCurrent(path)) renderAiPanel(path);
      announce("AI response ready");
    },
    onError: (snap) => {
      ai.elapsed = snap.elapsed;
      ai.status = "error";
      ai.error = snap.error || "claude call failed";
      if (isCurrent(path)) renderAiPanel(path);
      announce("AI request failed");
    },
  });
}

async function cancelJob(path) {
  const ai = aiFor(path);
  if (ai.timer) clearTimeout(ai.timer);
  const jobId = ai.jobId;
  ai.status = ai.result || ai.qa ? "done" : "idle"; // return to prior resting state
  ai.mode = ai.prevMode || "summary";
  if (isCurrent(path)) renderAiPanel(path);
  if (jobId) { try { await api("POST", `/job/${jobId}/cancel`); } catch { /* best-effort */ } }
}

function retryAi(path) {
  const ai = aiFor(path);
  if (ai.mode === "explain") startExplain(path);
  else if (ai.mode === "ask") startAsk(path, ai.question || "");
  else startSummary(path, true);
}

function elapsedStr(path) {
  const ai = aiFor(path);
  const secs = ai.elapsed != null ? Math.floor(ai.elapsed) : Math.floor((Date.now() - (ai.t0 || Date.now())) / 1000);
  const m = Math.floor(secs / 60), s = secs % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function renderAiPanel(path) {
  const el = document.getElementById("ai-panel");
  if (!el || !isCurrent(path)) return;
  const ai = aiFor(path);
  el.innerHTML = "";

  const head = document.createElement("div");
  head.className = "ai-head";
  const titleText =
    ai.mode === "explain" ? "AI · Explain" : ai.mode === "ask" ? "AI · Ask" : "AI summary";
  const title = document.createElement("span");
  title.className = "ai-title";
  title.textContent = titleText;
  head.appendChild(title);

  // Toggle button: Explain from summary; back to Summary from explain/ask.
  const toggle = document.createElement("button");
  toggle.className = "btn btn-ghost";
  if (ai.mode === "summary") {
    toggle.innerHTML = 'Explain <span class="kbd">e</span>';
    toggle.addEventListener("click", () => startExplain(path));
  } else {
    toggle.innerHTML = "Summary ◂";
    toggle.addEventListener("click", () => { ai.mode = "summary"; renderAiPanel(path); });
  }
  head.appendChild(toggle);
  el.appendChild(head);

  // State A — loading
  if (ai.status === "running") {
    const loading = document.createElement("div");
    loading.className = "ai-loading";
    loading.innerHTML = `<span class="spinner"></span> Analyzing diff… (job submitted, polling · ${elapsedStr(path)} elapsed)`;
    const progRow = document.createElement("div");
    progRow.className = "ai-loading";
    progRow.style.marginTop = "8px";
    progRow.innerHTML = `<span class="ai-progress-track"><span class="ai-progress-indef"></span></span> ${MAX_NOTE}`;
    const cancel = document.createElement("button");
    cancel.className = "btn";
    cancel.style.marginTop = "8px";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => cancelJob(path));
    el.append(loading, progRow, cancel);
    return;
  }

  // State D — error
  if (ai.status === "error") {
    const err = document.createElement("div");
    err.className = "ai-error";
    const msg = document.createElement("span");
    msg.textContent = `⚠ ${ai.error || "claude call failed"}`;
    const retry = document.createElement("button");
    retry.className = "btn";
    retry.innerHTML = "Retry ↻";
    retry.addEventListener("click", () => retryAi(path));
    err.append(msg, retry);
    el.appendChild(err);
    return;
  }

  // State B / C — result
  const body = document.createElement("div");
  body.className = "ai-body";
  const resultText = ai.result || (ai.mode === "summary" && ai.status === "idle" ? "Loading summary…" : "");
  renderMarkdown(body, resultText);
  el.appendChild(body);

  // Ask Q/A block (State C) shown beneath whatever body we have.
  if (ai.qa) {
    el.appendChild(divider());
    const qa = document.createElement("div");
    qa.className = "ai-qa";
    const q = document.createElement("div");
    q.className = "ai-q";
    q.textContent = `Q: ${ai.qa.q}`;
    const a = document.createElement("div");
    renderMarkdown(a, ai.qa.a || "");
    qa.append(q, a);
    el.appendChild(qa);
  }

  // Ask input (State B): always available once we have a resting panel.
  if (ai.status === "done" || ai.status === "idle") {
    el.appendChild(divider());
    const askWrap = document.createElement("div");
    const lbl = document.createElement("div");
    lbl.className = "ai-title";
    lbl.style.marginBottom = "6px";
    lbl.textContent = "Ask about this file:";
    const row = document.createElement("div");
    row.className = "ai-ask-row";
    const input = document.createElement("input");
    input.className = "text-input";
    input.id = "ai-ask-input";
    input.placeholder = "Why no jitter on the backoff?";
    const askBtn = document.createElement("button");
    askBtn.className = "btn";
    askBtn.innerHTML = 'Ask <span class="kbd">a</span> <span aria-hidden="true">↵</span>';
    const fire = () => { const q = input.value.trim(); if (q) startAsk(path, q); };
    askBtn.addEventListener("click", fire);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") fire(); e.stopPropagation(); });
    row.append(input, askBtn);
    askWrap.append(lbl, row);
    el.appendChild(askWrap);
  }
}

function focusAsk() {
  const input = document.getElementById("ai-ask-input");
  if (input) input.focus();
}

function divider() { const hr = document.createElement("hr"); hr.className = "ai-divider"; return hr; }
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// Minimal, XSS-safe markdown → DOM renderer for AI output. Builds nodes via
// textContent only (never innerHTML with model text). Handles paragraphs,
// bullet lists, headings, `code`/**bold**/*italic*, and decorative separators.
function renderInline(parent, text) {
  const re = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\s][^*]*\*)/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    let node;
    if (tok[0] === "`") { node = document.createElement("code"); node.textContent = tok.slice(1, -1); }
    else if (tok.startsWith("**")) { node = document.createElement("strong"); node.textContent = tok.slice(2, -2); }
    else { node = document.createElement("em"); node.textContent = tok.slice(1, -1); }
    parent.appendChild(node);
    last = m.index + tok.length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

function isSeparatorLine(line) {
  const t = line.trim();
  return t.length >= 3 && /^[\s─—–=_·•*-]+$/.test(t) && /[─—–=_]/.test(t);
}

function renderMarkdown(el, text) {
  el.textContent = "";
  if (!text) return;
  const lines = String(text).split("\n");
  let i = 0, para = [];
  const flush = () => {
    if (!para.length) return;
    const p = document.createElement("p");
    renderInline(p, para.join(" "));
    el.appendChild(p);
    para = [];
  };
  while (i < lines.length) {
    const t = lines[i].trim();
    if (t === "") { flush(); i++; continue; }
    if (isSeparatorLine(lines[i])) { flush(); el.appendChild(document.createElement("hr")); i++; continue; }
    if (/^#{1,6}\s+/.test(t)) {
      flush();
      const h = document.createElement("div");
      h.className = "ai-md-h";
      renderInline(h, t.replace(/^#{1,6}\s+/, ""));
      el.appendChild(h);
      i++; continue;
    }
    if (/^[-*]\s+/.test(t)) {
      flush();
      const ul = document.createElement("ul");
      while (i < lines.length && /^[-*]\s+/.test(lines[i].trim())) {
        const li = document.createElement("li");
        renderInline(li, lines[i].trim().replace(/^[-*]\s+/, ""));
        ul.appendChild(li);
        i++;
      }
      el.appendChild(ul);
      continue;
    }
    para.push(t);
    i++;
  }
  flush();
}

// ============================================================================
// Actions: viewed / flag / comment
// ============================================================================
async function markViewed() {
  const f = currentFile();
  if (!f) return;
  try {
    const res = await api("POST", "/file/viewed", { ...prKey(), path: f.filename });
    f.viewed = true;
    if (!State.review.viewed.includes(f.filename)) State.review.viewed.push(f.filename);
    renderFileList();
    renderFileDetail();
    if (res && res.remote_ok === false) toast("Viewed saved locally, GitHub sync failed", "error");
    else toast("Marked viewed");
  } catch (e) { toast(e.message, "error"); }
}

// ============================================================================
// Shared focus-trapped Modal
// ============================================================================
let modalState = null; // { closeOnBackdrop, lastFocus }

function openModal({ title, render, onClose }) {
  const root = document.getElementById("modal-root");
  modalState = { lastFocus: document.activeElement, onClose };
  root.innerHTML = "";
  const modal = document.createElement("div");
  modal.className = "modal";
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");

  const head = document.createElement("div");
  head.className = "modal-head";
  const t = document.createElement("span"); t.textContent = title;
  const close = document.createElement("button");
  close.className = "modal-close";
  close.setAttribute("aria-label", "Close");
  close.innerHTML = '✕ <span class="kbd">q</span>';
  close.addEventListener("click", closeModal);
  head.append(t, close);

  const body = document.createElement("div");
  render(modal, body);

  modal.prepend(head);
  if (![...modal.children].includes(body)) modal.insertBefore(body, modal.children[1] || null);
  root.appendChild(modal);
  root.hidden = false;

  root.onclick = (e) => { if (e.target === root) closeModal(); };
  trapFocus(modal);
  const first = modal.querySelector("textarea, input, button:not(.modal-close)");
  if (first) first.focus();
}

function closeModal() {
  const root = document.getElementById("modal-root");
  if (root.hidden) return;
  root.hidden = true;
  root.innerHTML = "";
  root.onclick = null;
  const st = modalState; modalState = null;
  if (st && st.onClose) st.onClose();
  if (st && st.lastFocus && st.lastFocus.focus) st.lastFocus.focus();
}

function trapFocus(modal) {
  modal.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const f = modal.querySelectorAll('button, input, textarea, [tabindex]:not([tabindex="-1"])');
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
}

function modalIsOpen() { return !document.getElementById("modal-root").hidden; }

// ---- component:comment-modal ----------------------------------------------
function openCommentModal() {
  const f = currentFile();
  if (!f) return;
  // Capture the diff selection NOW — focusing the modal textarea clears it.
  const range = selectedNewLineRange();
  const anchor = range
    ? (range.start !== range.end ? `lines ${range.start}–${range.end}` : `line ${range.end}`)
    : null;
  openModal({
    title: anchor ? `Comment on  ${f.filename} · ${anchor}` : `Comment on  ${f.filename}`,
    render: (modal, body) => {
      body.className = "modal-body";
      const lbl = document.createElement("label");
      lbl.textContent = anchor ? `Comment (anchored to ${anchor})` : "Comment";
      const ta = document.createElement("textarea");
      ta.className = "textarea";
      ta.id = "comment-text";
      ta.placeholder = anchor
        ? `Add a review comment on ${anchor}…`
        : "Add a comment scoped to this file (select code first to anchor it)…";
      body.append(lbl, ta);

      const foot = document.createElement("div");
      foot.className = "modal-foot";
      const cancel = document.createElement("button");
      cancel.className = "btn"; cancel.textContent = "Cancel";
      cancel.addEventListener("click", closeModal);
      const post = document.createElement("button");
      post.className = "btn btn-primary"; post.textContent = "Post comment";
      post.addEventListener("click", async () => {
        const text = ta.value.trim();
        if (!text) { ta.focus(); return; }
        post.disabled = true; cancel.disabled = true;
        post.innerHTML = '<span class="spinner spinner-sm"></span> Posting…';
        try {
          const payload = { ...prKey(), path: f.filename, text };
          if (range) {
            payload.line = range.end;
            payload.side = "RIGHT";
            if (range.start !== range.end) payload.start_line = range.start;
          }
          const res = await api("POST", "/comment", payload);
          if (res && res.ok) {
            State.review.comments = (State.review.comments || 0) + 1;
            const entry = {
              text,
              line: range ? range.end : null,
              start_line: range && range.start !== range.end ? range.start : null,
            };
            const threads = State.review.comment_threads || (State.review.comment_threads = {});
            threads[f.filename] = [...(threads[f.filename] || []), entry];
            f.comments = [...(f.comments || []), entry];
            closeModal();
            renderFileDetail();
            toast(range ? "Review comment posted" : "Comment posted");
          } else {
            throw new Error((res && res.error) || "comment failed");
          }
        } catch (e) {
          post.disabled = false; cancel.disabled = false;
          post.textContent = "Post comment";
          toast(e.message, "error");
        }
      });
      foot.append(cancel, post);
      modal.appendChild(foot);
    },
  });
}

// ---- component:flag-modal --------------------------------------------------
function openFlagModal() {
  const f = currentFile();
  if (!f) return;
  const alreadyFlagged = f.flagged;
  openModal({
    title: `⚑ Flag  ${f.filename}`,
    render: (modal, body) => {
      body.className = "modal-body";

      if (alreadyFlagged && f.flag_note) {
        const existing = document.createElement("div");
        existing.className = "modal-existing-note";
        existing.textContent = `Current note: ${f.flag_note}`;
        body.appendChild(existing);
      }

      const lbl = document.createElement("label");
      lbl.textContent = "Note (optional):";
      const ta = document.createElement("textarea");
      ta.className = "textarea";
      ta.value = f.flag_note || "";
      ta.placeholder = "Optional note for this flag…";
      body.append(lbl, ta);

      const foot = document.createElement("div");
      foot.className = "modal-foot";
      const cancel = document.createElement("button");
      cancel.className = "btn"; cancel.textContent = "Cancel";
      cancel.addEventListener("click", closeModal);

      const primary = document.createElement("button");
      primary.className = "btn btn-primary";
      primary.innerHTML = alreadyFlagged ? "Unflag" : "Flag ⚑";
      primary.addEventListener("click", async () => {
        const flagged = !alreadyFlagged;
        const note = flagged ? ta.value.trim() : "";
        primary.disabled = true; cancel.disabled = true;
        primary.innerHTML = '<span class="spinner spinner-sm"></span>';
        try {
          const res = await api("POST", "/file/flag", { ...prKey(), path: f.filename, flagged, note });
          f.flagged = res.flagged;
          f.flag_note = res.note || "";
          if (res.flagged) State.review.flagged[f.filename] = res.note || "";
          else delete State.review.flagged[f.filename];
          closeModal();
          renderFileList();
          renderFileDetail();
          toast(res.flagged ? "File flagged ⚑" : "Flag removed");
        } catch (e) {
          primary.disabled = false; cancel.disabled = false;
          primary.innerHTML = alreadyFlagged ? "Unflag" : "Flag ⚑";
          toast(e.message, "error");
        }
      });
      foot.append(cancel, primary);
      modal.appendChild(foot);
    },
  });
}

// ============================================================================
// screen:submit-review
// ============================================================================
async function renderSubmit() {
  // Rehydrate working state from server before computing counts.
  try {
    const { owner, repo, number } = prKey();
    const fresh = await api("GET", `/state/${owner}/${repo}/${number}`);
    State.review = fresh;
    applyReviewToFiles();
  } catch { /* fall back to in-memory copy */ }

  const wrap = document.getElementById("submit-body");
  wrap.innerHTML = "";
  const pr = State.pr;
  const total = State.files.length;
  const viewed = viewedCount();
  const flagged = flaggedCount();
  const skipped = total - viewed;
  const comments = State.review.comments || 0;

  const head = document.createElement("div");
  head.className = "submit-head";
  const h = document.createElement("h2");
  h.textContent = `Submit review · ${refString()}  ${pr.title || ""}`;
  const close = document.createElement("button");
  close.className = "modal-close";
  close.innerHTML = '✕ <span class="kbd">q</span>';
  close.addEventListener("click", () => show("review"));
  head.append(h, close);
  wrap.appendChild(head);

  // counts
  const grid = document.createElement("div");
  grid.className = "counts-grid";
  for (const [label, value] of [["Files", total], ["Viewed", viewed], ["Flagged", flagged], ["Skipped", skipped]]) {
    const cell = document.createElement("div");
    cell.className = "count-cell";
    cell.innerHTML = `<div class="cc-label">${label}</div><div class="cc-value">${value}</div>`;
    grid.appendChild(cell);
  }
  wrap.appendChild(grid);

  const cline = document.createElement("div");
  cline.className = "comments-line";
  cline.textContent = `Comments posted   ${comments}`;
  wrap.appendChild(cline);

  // flagged table
  const flaggedFiles = State.files.filter((f) => f.flagged);
  const ft = document.createElement("div");
  ft.innerHTML = '<div class="section-title">Flagged files</div>';
  if (flaggedFiles.length) {
    const table = document.createElement("table");
    table.className = "flagged-table";
    table.innerHTML = "<thead><tr><th>File</th><th>Note</th></tr></thead>";
    const tb = document.createElement("tbody");
    for (const f of flaggedFiles) {
      const tr = document.createElement("tr");
      const td1 = document.createElement("td"); td1.textContent = f.filename;
      const td2 = document.createElement("td"); td2.textContent = f.flag_note || "—";
      tr.append(td1, td2);
      tb.appendChild(tr);
    }
    table.appendChild(tb);
    ft.appendChild(table);
  } else {
    const e = document.createElement("div");
    e.className = "flagged-empty";
    e.textContent = "No files flagged.";
    ft.appendChild(e);
  }
  wrap.appendChild(ft);

  // review body
  const bodyLbl = document.createElement("div");
  bodyLbl.className = "section-title";
  bodyLbl.textContent = "Review body (optional)";
  const bodyTa = document.createElement("textarea");
  bodyTa.className = "textarea";
  bodyTa.id = "submit-body-text";
  bodyTa.placeholder = "Optional summary comment for the review…";
  wrap.append(bodyLbl, bodyTa);

  // decision radios
  const decTitle = document.createElement("div");
  decTitle.className = "section-title";
  decTitle.textContent = "Decision";
  const dec = document.createElement("div");
  dec.className = "decision-group";
  dec.setAttribute("role", "radiogroup");
  const options = [
    { event: "approve", label: "✓ Approve" },
    { event: "request_changes", label: "✗ Request changes" },
    { event: "comment", label: "◷ Comment only" },
  ];
  options.forEach((o, i) => {
    const lab = document.createElement("label");
    lab.className = "decision-opt";
    const r = document.createElement("input");
    r.type = "radio"; r.name = "decision"; r.value = o.event;
    if (i === 0) r.checked = true;
    lab.append(r, document.createTextNode(" " + o.label));
    dec.appendChild(lab);
  });
  wrap.append(decTitle, dec);

  // unviewed warning (non-blocking)
  const warn = document.createElement("div");
  warn.className = "unviewed-warn";
  warn.hidden = skipped === 0;
  warn.textContent = `${skipped} files not yet viewed — submit anyway?`;
  wrap.appendChild(warn);

  // actions
  const actions = document.createElement("div");
  actions.className = "submit-actions";
  const cancel = document.createElement("button");
  cancel.className = "btn"; cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => show("review"));
  const submit = document.createElement("button");
  submit.className = "btn btn-primary";
  submit.innerHTML = 'Submit review <span aria-hidden="true">↵</span>';
  submit.addEventListener("click", () => doSubmit(submit, cancel, dec, bodyTa));
  actions.append(cancel, submit);
  wrap.appendChild(actions);

  bodyTa.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") doSubmit(submit, cancel, dec, bodyTa);
  });
}

async function doSubmit(submit, cancel, dec, bodyTa) {
  const event = dec.querySelector('input[name="decision"]:checked').value;
  const body = bodyTa.value.trim();
  submit.disabled = true; cancel.disabled = true;
  submit.innerHTML = '<span class="spinner spinner-sm"></span> Submitting…';
  try {
    const payload = { ...prKey(), event };
    if (body) payload.body = body;
    const res = await api("POST", "/review/submit", payload);
    if (res && res.ok) {
      State.review.submitted = true;
      toast("Review submitted");
      show("landing");
    } else {
      throw new Error((res && res.error) || "review submission failed");
    }
  } catch (e) {
    submit.disabled = false; cancel.disabled = false;
    submit.innerHTML = 'Submit review <span aria-hidden="true">↵</span>';
    toast(e.message, "error");
  }
}

// ============================================================================
// screen:repowise — status → branch (modal | prepare+poll → embed | fallback)
// component:nav-tabs · repowise-prepare · repo-path-prompt · embed · fallback · error
// ============================================================================
const RW_STEPS = [
  { key: "resolve_path", label: "Resolve local repo path" },
  { key: "checkout", label: "Checkout PR head" },
  { key: "index", label: "Index repo (repowise init)" },
  { key: "serve", label: "Start repowise serve" },
  { key: "open", label: "Open dashboard" },
];
const REPOWISE_DOCS = "https://github.com/repowise/repowise";

// Per-PR cache key so switching tabs does not re-prepare an already-prepared PR.
function rwPrKey() { const { owner, repo, number } = prKey(); return `${owner}/${repo}#${number}`; }

function rwState() {
  if (!State.rw || State.rw.key !== rwPrKey()) {
    State.rw = { key: rwPrKey(), phase: "idle", jobId: null, snap: null, t0: 0, alive: false };
  }
  return State.rw;
}

function rwRegion() { return document.getElementById("repowise-region"); }
function rwIsActive() { return activeScreen === "repowise"; }

// renderRepowise: entry point when the Repowise tab is shown. If already
// prepared/embedded for this PR, re-render the cached result without re-preparing.
async function renderRepowise() {
  const rw = rwState();
  if (rw.phase === "embed") { renderRwEmbed(rw); return; }
  if (rw.phase === "fallback") { renderRwFallback(rw); return; }
  if (rw.phase === "error") { renderRwError(rw); return; }
  if (rw.phase === "preparing" && rw.snap) { renderRwPrepare(rw); return; }
  if (rw.phase === "preparing") return; // submit in flight; tick will render
  await rwStartFlow();
}

async function rwStartFlow() {
  const rw = rwState();
  rwShowLoading("Checking repowise…");
  let status;
  try {
    const { owner, repo, number } = prKey();
    status = await api("GET", `/repowise/status?owner=${encodeURIComponent(owner)}&repo=${encodeURIComponent(repo)}&number=${number}`);
  } catch (e) {
    rw.phase = "error";
    rw.snap = { error: e.message, error_hint: e.hint || "", error_step: null, steps: [] };
    renderRwError(rw);
    return;
  }
  if (!rwIsActive()) return;
  if (!status.cli_present) {
    rw.phase = "error";
    rw.snap = { error: "repowise CLI not found on PATH",
                error_hint: status.cli_hint || "install it: uv tool install repowise",
                error_step: "serve", steps: [], variant: "cli" };
    renderRwError(rw);
    return;
  }
  if (status.node_ok === false) {
    rw.phase = "error";
    rw.snap = { error: "Node.js not available for repowise",
                error_hint: status.node_hint || "install Node.js",
                error_step: "serve", steps: [], variant: "cli" };
    renderRwError(rw);
    return;
  }
  if (!status.repo_path_known) {
    openRepoPathModal();
    return;
  }
  rwPrepare();
}

async function rwPrepare() {
  const rw = rwState();
  rw.phase = "preparing";
  rw.snap = null;
  rw.t0 = Date.now();
  rw.alive = true;
  rwShowLoading("Preparing repowise analysis…");
  try {
    const { owner, repo, number } = prKey();
    const { job_id } = await api("POST", "/repowise/prepare", { owner, repo, number });
    rw.jobId = job_id;
    pollPrepare(job_id);
  } catch (e) {
    // 409 = path unknown → reopen modal; anything else → error panel.
    if (e instanceof ApiError && e.status === 409) { openRepoPathModal(); return; }
    rw.phase = "error";
    rw.snap = { error: e.message, error_hint: e.hint || "", error_step: null, steps: [] };
    renderRwError(rw);
  }
}

// Poller over /repowise/prepare/{job_id} (distinct from /job/{id}, so this is a
// sibling of pollJobId rather than a reuse of it).
function pollPrepare(jobId) {
  const rw = rwState();
  const tick = async () => {
    if (!rw.alive || rw.jobId !== jobId) return;
    let snap;
    try { snap = await api("GET", `/repowise/prepare/${jobId}`); }
    catch { setTimeout(tick, POLL_MS); return; }
    if (!rw.alive || rw.jobId !== jobId) return;
    rw.snap = snap;
    if (snap.status === "done") {
      rw.alive = false;
      rw.phase = snap.frameable ? "embed" : "fallback";
      announce("repowise analysis ready");
      if (rwIsActive()) renderRepowise();
    } else if (snap.status === "error") {
      rw.alive = false;
      rw.phase = "error";
      announce("repowise prepare failed");
      if (rwIsActive()) renderRwError(rw);
    } else if (snap.status === "cancelled") {
      rw.alive = false;
      rw.phase = "idle";
    } else {
      if (rwIsActive()) renderRwPrepare(rw);
      setTimeout(tick, POLL_MS);
    }
  };
  setTimeout(tick, POLL_MS);
}

function rwShowLoading(msg) {
  const el = rwRegion();
  if (!el) return;
  el.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "rw-prepare";
  const ld = document.createElement("div");
  ld.className = "ai-loading";
  ld.innerHTML = `<span class="spinner"></span> ${escapeHtml(msg)}`;
  wrap.appendChild(ld);
  el.appendChild(wrap);
}

function rwElapsedStr(t0) {
  const secs = Math.max(0, Math.floor((Date.now() - (t0 || Date.now())) / 1000));
  const m = Math.floor(secs / 60), s = secs % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

// ---- component:repowise-prepare -------------------------------------------
function renderRwPrepare(rw) {
  const el = rwRegion();
  if (!el || !rwIsActive()) return;
  const snap = rw.snap || { steps: [] };
  const byKey = {};
  for (const s of (snap.steps || [])) byKey[s.key] = s;
  el.innerHTML = "";

  const wrap = document.createElement("div");
  wrap.className = "rw-prepare";

  const title = document.createElement("div");
  title.className = "rw-prepare-title";
  title.textContent = `Preparing repowise analysis · ${refString()}`;
  wrap.appendChild(title);

  const list = document.createElement("ol");
  list.className = "rw-steps";
  for (const def of RW_STEPS) {
    const st = byKey[def.key] || { status: "pending", detail: "" };
    const li = document.createElement("li");
    li.className = "rw-step is-" + st.status;

    const g = document.createElement("span");
    g.className = "rw-step-glyph";
    if (st.status === "running") {
      g.innerHTML = '<span class="spinner"></span>';
    } else {
      const glyph = st.status === "done" ? { t: "✓", c: "glyph-pass" }
        : st.status === "failed" ? { t: "✗", c: "glyph-fail" }
        : st.status === "skipped" ? { t: "◷", c: "glyph-none" }
        : { t: "○", c: "glyph-none" };
      g.innerHTML = `<span class="${glyph.c}">${glyph.t}</span>`;
    }

    const main = document.createElement("div");
    const label = document.createElement("div");
    label.className = "rw-step-label";
    label.textContent = def.label;
    main.appendChild(label);

    const subParts = [];
    if (st.detail) subParts.push(st.detail);
    if (st.status === "running") subParts.push(`${rwElapsedStr(rw.t0)} elapsed`);
    if (subParts.length) {
      const sub = document.createElement("div");
      sub.className = "rw-step-sub";
      sub.textContent = subParts.join("   ·   ");
      main.appendChild(sub);
    }
    li.append(g, main);
    list.appendChild(li);
  }
  wrap.appendChild(list);

  const prog = document.createElement("div");
  prog.className = "rw-prepare-progress";
  prog.innerHTML = '<span class="ai-progress-track"><span class="ai-progress-indef"></span></span> may take up to 30s';
  wrap.appendChild(prog);

  const foot = document.createElement("div");
  foot.className = "rw-prepare-foot";
  const cancel = document.createElement("button");
  cancel.className = "btn";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", rwCancel);
  foot.appendChild(cancel);
  wrap.appendChild(foot);

  el.appendChild(wrap);
}

async function rwCancel() {
  const rw = rwState();
  const jobId = rw.jobId;
  rw.alive = false;
  rw.phase = "idle";
  rw.jobId = null;
  show("review");
  if (jobId) { try { await api("POST", `/repowise/prepare/${jobId}/cancel`); } catch { /* best-effort */ } }
}

// ---- component:repowise-embed ---------------------------------------------
function renderRwEmbed(rw) {
  const el = rwRegion();
  if (!el || !rwIsActive()) return;
  const snap = rw.snap;
  const url = snap.dashboard_url || "";
  const pr = State.pr;
  el.innerHTML = "";

  const wrap = document.createElement("div");
  wrap.className = "rw-embed";

  const bar = document.createElement("div");
  bar.className = "rw-embed-bar";
  const ctx = document.createElement("span");
  const mark = document.createElement("span");
  mark.className = "rw-bar-mark"; mark.textContent = "◆";
  const ctxText = document.createElement("span");
  ctxText.className = "rw-bar-ctx";
  ctxText.textContent = ` repowise · ${refString()} @ ${pr.head || "?"}`;
  const port = document.createElement("span");
  port.textContent = snap.serve_port ? `  ·  :${snap.serve_port}` : "";
  ctx.append(mark, ctxText, port);

  const actions = document.createElement("span");
  actions.className = "rw-bar-actions";
  // Complete (embedded dashboard) vs Diff associations (prview-native blast radius).
  if (!rw.mode) rw.mode = "complete";
  const seg = document.createElement("span");
  seg.className = "rw-mode-seg";
  for (const [mode, label] of [["complete", "Complete"], ["diff", "Diff associations"]]) {
    const b = document.createElement("button");
    b.className = "rw-mode-btn" + (rw.mode === mode ? " is-active" : "");
    b.textContent = label;
    b.addEventListener("click", () => { rw.mode = mode; renderRwEmbed(rw); });
    seg.appendChild(b);
  }
  const cov = document.createElement("button");
  cov.className = "btn btn-ghost";
  cov.textContent = "Ingest coverage";
  cov.addEventListener("click", openCoverageModal);
  const docs = document.createElement("button");
  docs.className = "btn btn-ghost";
  docs.textContent = "Generate docs";
  docs.addEventListener("click", openDocgenModal);
  const restart = document.createElement("button");
  restart.className = "btn btn-ghost";
  restart.textContent = "↻ Restart";
  restart.addEventListener("click", rwRestart);
  const open = document.createElement("button");
  open.className = "btn btn-ghost";
  open.textContent = "Open ↗";
  open.addEventListener("click", () => window.open(url, "_blank", "noopener"));
  actions.append(seg, cov, docs, restart, open);
  bar.append(ctx, actions);

  const frame = document.createElement("div");
  frame.className = "rw-embed-frame";
  if (rw.mode === "diff") {
    renderRwDiffPanel(frame, rw);
  } else {
    const iframe = document.createElement("iframe");
    iframe.src = url;
    iframe.title = `repowise dashboard for ${refString()}`;
    // The embedded dashboard is a DIFFERENT-origin localhost server NOT behind prview's
    // token gate. Sandbox it so it can't navigate the top frame or reach prview's origin;
    // allow-scripts+allow-same-origin so repowise's own SPA + same-origin XHR still work.
    iframe.sandbox = "allow-scripts allow-same-origin allow-forms allow-popups";
    iframe.referrerPolicy = "no-referrer";
    frame.appendChild(iframe);
  }

  wrap.append(bar, frame);
  el.appendChild(wrap);
}

// Diff mode: prview-native panel of the changed files' associations — direct
// risk, transitively-affected (1-hop+) files, co-change partners, reviewers.
async function renderRwDiffPanel(frame, rw) {
  frame.classList.add("rw-diff-panel");
  if (rw.blast) { paintRwDiff(frame, rw.blast); return; }
  frame.innerHTML = '<div class="rw-diff-loading"><span class="spinner"></span> Analyzing diff associations…</div>';
  const { owner, repo, number } = prKey();
  const changed_files = State.files.map((f) => f.filename);
  try {
    rw.blast = await api("POST", "/repowise/blast-radius", { owner, repo, number, changed_files });
    paintRwDiff(frame, rw.blast);
  } catch (e) {
    frame.innerHTML = "";
    const err = document.createElement("div");
    err.className = "rw-diff-error";
    err.textContent = (e && e.message) || "Could not load diff associations.";
    frame.appendChild(err);
  }
}

function paintRwDiff(frame, b) {
  frame.innerHTML = "";
  const risk = Math.round((b.overall_risk_score || 0) * 10) / 10;
  const head = document.createElement("div");
  head.className = "rw-diff-head";
  head.textContent = `Diff blast radius · risk ${risk}/10 · ${b.direct_risks.length} changed files`;
  frame.appendChild(head);

  const section = (title, rows, render) => {
    const s = document.createElement("div");
    s.className = "rw-diff-section";
    const h = document.createElement("div");
    h.className = "rw-diff-title";
    h.textContent = `${title}  (${rows.length})`;
    s.appendChild(h);
    if (!rows.length) {
      const e = document.createElement("div");
      e.className = "rw-diff-empty"; e.textContent = "none";
      s.appendChild(e);
    } else {
      for (const r of rows) s.appendChild(render(r));
    }
    frame.appendChild(s);
  };

  const row = (text, tag) => {
    const d = document.createElement("div");
    d.className = "rw-diff-row";
    const t = document.createElement("span"); t.className = "rw-diff-path"; t.textContent = text;
    d.appendChild(t);
    if (tag) { const g = document.createElement("span"); g.className = "rw-diff-tag"; g.textContent = tag; d.appendChild(g); }
    return d;
  };

  section("Changed files by risk",
    [...b.direct_risks].sort((a, c) => c.risk_score - a.risk_score),
    (r) => row(r.path, `risk ${Math.round(r.risk_score * 100) / 100}`));
  section("Transitively affected (not in diff)", b.transitive_affected,
    (r) => row(r.path, `depth ${r.depth}`));
  section("Co-change partners missing from this PR", b.cochange_warnings,
    (r) => row(`${r.changed} → ${r.missing_partner}`, `${Math.round(r.score * 100)}%`));
  section("Suggested reviewers", b.recommended_reviewers,
    (r) => row(r.email, `${r.files} files · ${Math.round(r.ownership_pct)}%`));
  section("Test gaps", b.test_gaps, (p) => row(p));
}

// Ingest a coverage report so the dashboard's coverage / risk×coverage panels
// populate. The report is generated in the main clone (deps live there); blank
// path → server auto-detects common report names.
function openCoverageModal() {
  openModal({
    title: "Ingest coverage report",
    render: (modal, body) => {
      body.className = "modal-body";
      const lbl = document.createElement("label");
      lbl.textContent = "Coverage report path (blank = auto-detect)";
      const inp = document.createElement("input");
      inp.className = "text-input";
      inp.placeholder = "~/repos/<repo>/coverage.lcov  ·  lcov / cobertura / clover";
      const note = document.createElement("div");
      note.className = "modal-existing-note";
      note.textContent = "Generate in your main clone first, e.g. pytest --cov --cov-report=lcov";
      body.append(lbl, inp, note);

      const foot = document.createElement("div");
      foot.className = "modal-foot";
      const cancel = document.createElement("button");
      cancel.className = "btn"; cancel.textContent = "Cancel";
      cancel.addEventListener("click", closeModal);
      const go = document.createElement("button");
      go.className = "btn btn-primary"; go.textContent = "Ingest";
      go.addEventListener("click", async () => {
        go.disabled = true; cancel.disabled = true;
        go.innerHTML = '<span class="spinner spinner-sm"></span> Ingesting…';
        try {
          const { owner, repo, number } = prKey();
          const res = await api("POST", "/repowise/coverage",
            { owner, repo, number, path: inp.value.trim() || null });
          closeModal();
          toast(`Ingested coverage for ${res.files} files`);
          const rw = rwState();
          if (rw) { rw.blast = null; renderRepowise(); } // refresh panels with new data
        } catch (e) {
          go.disabled = false; cancel.disabled = false;
          go.textContent = "Ingest";
          toast(e.message, "error");
        }
      });
      foot.append(cancel, go);
      modal.appendChild(foot);
    },
  });
}

// Generate the docs/wiki panel with a LOCAL ollama model (no cloud cost).
// Long-running → kicks off a background job and polls until done.
function openDocgenModal() {
  openModal({
    title: "Generate docs (local ollama)",
    render: async (modal, body) => {
      body.className = "modal-body";
      const lbl = document.createElement("label");
      lbl.textContent = "Ollama model";
      const sel = document.createElement("select");
      sel.className = "text-input";
      sel.innerHTML = '<option value="">loading models…</option>';
      const note = document.createElement("div");
      note.className = "modal-existing-note";
      note.textContent = "Runs repowise init via your local ollama — free, but can take several minutes.";
      const status = document.createElement("div");
      status.className = "rw-docgen-status";
      body.append(lbl, sel, note, status);

      try {
        const { models } = await api("GET", "/repowise/ollama-models");
        sel.innerHTML = "";
        if (!models.length) {
          sel.innerHTML = '<option value="">no models — run: ollama pull qwen2.5:3b</option>';
        } else {
          for (const m of models) {
            const o = document.createElement("option"); o.value = m; o.textContent = m; sel.appendChild(o);
          }
        }
      } catch { sel.innerHTML = '<option value="">could not list models</option>'; }

      const foot = document.createElement("div");
      foot.className = "modal-foot";
      const cancel = document.createElement("button");
      cancel.className = "btn"; cancel.textContent = "Close";
      cancel.addEventListener("click", closeModal);
      const go = document.createElement("button");
      go.className = "btn btn-primary"; go.textContent = "Generate";
      go.addEventListener("click", async () => {
        const model = sel.value;
        if (!model) { toast("pull an ollama model first", "error"); return; }
        go.disabled = true;
        go.innerHTML = '<span class="spinner spinner-sm"></span> Generating…';
        try {
          const { owner, repo, number } = prKey();
          const { job_id } = await api("POST", "/repowise/docs/generate", { owner, repo, number, model });
          await pollDocgen(job_id, status);
          go.disabled = false; go.textContent = "Generate";
        } catch (e) {
          go.disabled = false; go.textContent = "Generate";
          status.textContent = (e && e.message) || "generation failed";
        }
      });
      foot.append(cancel, go);
      modal.appendChild(foot);
    },
  });
}

async function pollDocgen(jobId, status) {
  const t0 = Date.now();
  for (;;) {
    let snap;
    try { snap = await api("GET", `/repowise/docs/generate/${jobId}`); }
    catch (e) { status.textContent = e.message; return; }
    const secs = Math.round((Date.now() - t0) / 1000);
    if (snap.status === "running") {
      status.textContent = `Generating with ${snap.model}… ${secs}s (local, please wait)`;
      await new Promise((r) => setTimeout(r, 2000));
      continue;
    }
    if (snap.status === "done") {
      status.textContent = "Docs generated ✓ — reloading dashboard";
      toast("Docs generated");
      const rw = rwState();
      if (rw) { rw.blast = null; renderRepowise(); }
      return;
    }
    status.textContent = (snap.error || "generation failed") + (snap.log_tail ? `\n${snap.log_tail.slice(-300)}` : "");
    return;
  }
}

async function rwRestart() {
  const rw = rwState();
  const { owner, repo } = prKey();
  try { await api("POST", "/repowise/stop", { owner, repo }); } catch { /* best-effort */ }
  rw.phase = "idle";
  rw.jobId = null;
  rw.snap = null;
  rwPrepare();
}

// ---- component:repowise-link-fallback -------------------------------------
function renderRwFallback(rw) {
  const el = rwRegion();
  if (!el || !rwIsActive()) return;
  const url = (rw.snap && rw.snap.dashboard_url) || "";
  el.innerHTML = "";

  const wrap = document.createElement("div");
  wrap.className = "rw-fallback";

  const head = document.createElement("div");
  head.className = "rw-fallback-mark";
  head.innerHTML = '<span class="rw-bar-mark">◆</span> repowise can’t be embedded here';

  const text = document.createElement("div");
  text.className = "rw-fallback-text";
  text.textContent = `repowise's dashboard blocks being shown inside another page (frame-ancestors). Open it in a new browser tab — it's already running for ${refString()}.`;

  const open = document.createElement("button");
  open.className = "btn btn-primary";
  open.textContent = "Open repowise analysis ↗";
  open.addEventListener("click", () => window.open(url, "_blank", "noopener"));

  const urlRow = document.createElement("div");
  urlRow.className = "rw-url-row";
  const urlEl = document.createElement("span");
  urlEl.className = "rw-url";
  urlEl.textContent = url;
  const copy = document.createElement("button");
  copy.className = "btn";
  copy.textContent = "Copy";
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(url); } catch { /* ignore */ }
    toast("Copied");
  });
  urlRow.append(urlEl, copy);

  wrap.append(head, text, open, urlRow);
  el.appendChild(wrap);
}

// ---- component:repowise-error ---------------------------------------------
function renderRwError(rw) {
  const el = rwRegion();
  if (!el || !rwIsActive()) return;
  const snap = rw.snap || {};
  const variant = snap.variant
    || (snap.error_step === "checkout" ? "checkout"
      : snap.error_step === "serve" ? "serve" : "generic");
  el.innerHTML = "";

  const wrap = document.createElement("div");
  wrap.className = "rw-error";

  // Failing step header (glyph ✗) when we know which step failed.
  if (snap.error_step) {
    const def = RW_STEPS.find((s) => s.key === snap.error_step);
    const head = document.createElement("div");
    head.className = "rw-step";
    head.innerHTML = `<span class="rw-step-glyph"><span class="glyph-fail">✗</span></span>`;
    const lbl = document.createElement("div");
    lbl.className = "rw-step-label";
    lbl.textContent = def ? def.label : snap.error_step;
    head.appendChild(lbl);
    wrap.appendChild(head);
  }

  const err = document.createElement("div");
  err.className = "ai-error";
  const msg = document.createElement("span");
  msg.textContent = `⚠ ${snap.error || "repowise prepare failed"}`;
  err.appendChild(msg);
  wrap.appendChild(err);

  if (snap.error_hint) {
    const hint = document.createElement("div");
    hint.className = "rw-error-hint";
    if (variant === "cli") {
      // Install hint shown as inline code (e.g. `uv tool install repowise`).
      hint.append(document.createTextNode("— "));
      const code = document.createElement("code");
      code.textContent = snap.error_hint;
      hint.appendChild(code);
    } else {
      hint.textContent = `— ${snap.error_hint}`;
    }
    wrap.appendChild(hint);
  }

  const actions = document.createElement("div");
  actions.className = "rw-error-actions";
  const retry = document.createElement("button");
  retry.className = "btn";
  retry.textContent = "Retry ↻";
  retry.addEventListener("click", rwRetry);
  actions.appendChild(retry);

  if (variant === "cli") {
    const docs = document.createElement("button");
    docs.className = "btn";
    docs.textContent = "Open docs ↗";
    docs.addEventListener("click", () => window.open(REPOWISE_DOCS, "_blank", "noopener"));
    actions.appendChild(docs);
  } else if (variant === "serve" && snap.stderr_tail) {
    const view = document.createElement("button");
    view.className = "btn";
    view.textContent = "View output";
    view.addEventListener("click", () => {
      if (wrap.querySelector(".rw-stderr")) return;
      const out = document.createElement("pre");
      out.className = "rw-stderr";
      out.textContent = snap.stderr_tail;
      wrap.appendChild(out);
    });
    actions.appendChild(view);
  } else if (variant === "checkout") {
    const change = document.createElement("button");
    change.className = "btn";
    change.textContent = "Change path";
    change.addEventListener("click", () => openRepoPathModal());
    const cancel = document.createElement("button");
    cancel.className = "btn";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => show("review"));
    actions.append(change, cancel);
  }
  wrap.appendChild(actions);

  el.appendChild(wrap);
  announce("repowise prepare failed");
}

function rwRetry() {
  const rw = rwState();
  rw.phase = "idle";
  rw.jobId = null;
  rw.snap = null;
  rwPrepare();
}

// ---- component:repo-path-prompt -------------------------------------------
function openRepoPathModal() {
  const { owner, repo } = prKey();
  openModal({
    title: `Local clone of  ${owner}/${repo}`,
    onClose: () => {
      // q/Esc/Cancel/backdrop: if we never resolved a path, fall back to Review
      // (prepare cannot proceed without a path).
      const rw = rwState();
      if (rw.phase !== "preparing" && rw.phase !== "embed" && rw.phase !== "fallback") {
        if (activeScreen === "repowise") show("review");
      }
    },
    render: (modal, body) => {
      body.className = "modal-body";

      const desc = document.createElement("div");
      desc.style.marginBottom = "12px";
      desc.style.color = "var(--fg-muted)";
      desc.textContent = `repowise runs against a local checkout. Enter the path to your clone of ${owner}/${repo}. prview remembers it per repo.`;
      body.appendChild(desc);

      const lbl = document.createElement("label");
      lbl.textContent = "Local path";
      const input = document.createElement("input");
      input.className = "text-input";
      input.type = "text";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.placeholder = "~/code/repo";
      input.style.width = "100%";
      body.append(lbl, input);

      const hint = document.createElement("div");
      hint.className = "field-hint";
      hint.textContent = "Tip: the directory containing the repo's .git";
      body.appendChild(hint);

      const errEl = document.createElement("div");
      errEl.className = "field-error";
      errEl.setAttribute("role", "alert");
      errEl.hidden = true;
      body.appendChild(errEl);

      const foot = document.createElement("div");
      foot.className = "modal-foot";
      const cancel = document.createElement("button");
      cancel.className = "btn"; cancel.textContent = "Cancel";
      cancel.addEventListener("click", closeModal);
      const save = document.createElement("button");
      save.className = "btn btn-primary"; save.textContent = "Save";

      const setErr = (m) => {
        if (m) { errEl.textContent = m; errEl.hidden = false; input.classList.add("invalid"); }
        else { errEl.hidden = true; input.classList.remove("invalid"); }
      };

      const submit = async () => {
        const path = input.value.trim();
        if (!path) { input.focus(); return; }
        setErr("");
        save.disabled = true; cancel.disabled = true;
        save.innerHTML = '<span class="spinner spinner-sm"></span> Validating…';
        try {
          await api("POST", "/repowise/repo-path", { owner, repo, path }, { retryOn409: false });
          // Persisted: close modal and continue the prepare sequence.
          modalState && (modalState.onClose = null); // success — don't bounce to Review
          closeModal();
          rwPrepare();
        } catch (e) {
          save.disabled = false; cancel.disabled = false;
          save.textContent = "Save";
          // Field-scoped inline error — NOT a toast; modal stays open.
          setErr(`⚠ ${e.message}${e.hint ? ` — ${e.hint}` : ""}`);
          input.classList.add("invalid");
        }
      };
      save.addEventListener("click", submit);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); submit(); }
        if (e.key !== "Escape") e.stopPropagation();
      });
      input.addEventListener("input", () => setErr(""));

      foot.append(cancel, save);
      modal.appendChild(foot);
    },
  });
}

// ============================================================================
// Global keyboard shortcuts: v e a c f s b j k q g
// ============================================================================
function isTyping(e) {
  const t = e.target;
  return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
}

document.addEventListener("keydown", (e) => {
  // q / Esc always close an open modal.
  if (modalIsOpen()) {
    if (e.key === "Escape" || (e.key === "q" && !isTyping(e))) { e.preventDefault(); closeModal(); }
    return;
  }
  // Selection-explain popover: Escape closes it; swallow other shortcuts so the
  // reviewer can read it without j/k/e/etc. firing underneath.
  if (selPopoverOpen()) {
    if (e.key === "Escape") { e.preventDefault(); closeSelPopover(); }
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  // g toggles Review ⇄ Repowise on either tab (mnemonic: "go to").
  if (e.key === "g" && !isTyping(e) && (activeScreen === "review" || activeScreen === "repowise")) {
    if (activeScreen === "review" && diffSelection()) return; // selecting diff text
    e.preventDefault();
    show(activeScreen === "repowise" ? "review" : "repowise");
    return;
  }

  if (activeScreen === "submit") {
    if (e.key === "q" || e.key === "Escape") { if (!isTyping(e)) { e.preventDefault(); show("review"); } }
    return;
  }
  if (activeScreen === "repowise") {
    if ((e.key === "q" || e.key === "Escape") && !isTyping(e)) { e.preventDefault(); show("review"); }
    return;
  }
  if (activeScreen !== "review") return;
  if (isTyping(e)) return;
  if (diffSelection()) return; // user is selecting diff text — don't navigate/act

  switch (e.key) {
    case "j": e.preventDefault(); navTo(1); break;
    case "k": e.preventDefault(); navTo(-1); break;
    case "b": e.preventDefault(); navTo(-1); break;
    case "v": e.preventDefault(); markViewed(); break;
    case "e": e.preventDefault(); startExplain(currentFile().filename); break;
    case "a": e.preventDefault(); focusAsk(); break;
    case "c": e.preventDefault(); openCommentModal(); break;
    case "f": e.preventDefault(); openFlagModal(); break;
    case "s": e.preventDefault(); show("submit"); break;
    case "q": case "Escape": e.preventDefault(); show("landing"); break;
  }
});

// ----------------------------------------------------------------------------
// Boot
// ----------------------------------------------------------------------------
show("landing");
refInput.focus();
