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
  submit: document.getElementById("screen-submit"),
};
let activeScreen = "landing";

function show(screen) {
  activeScreen = screen;
  for (const [name, el] of Object.entries(Screens)) el.hidden = name !== screen;
  if (screen === "landing") loadResumeList();
  if (screen === "submit") renderSubmit();
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
  for (const f of State.files) {
    f.viewed = viewed.has(f.filename);
    f.flagged = Object.prototype.hasOwnProperty.call(flagged, f.filename);
    f.flag_note = flagged[f.filename] || "";
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

function renderSummary() {
  const pr = State.pr;
  const ci = ciGlyph(pr.ci_status);
  const dec = decisionGlyph(pr.review_decision);
  const el = document.getElementById("pr-summary");
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
  submit.innerHTML = 'Submit (<span class="kbd">s</span>)';
  submit.addEventListener("click", () => show("submit"));
  statusRow.append(ciEl, decEl, submit);

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

    const name = document.createElement("span");
    name.className = "fl-name";
    name.textContent = f.filename;
    name.title = f.filename;

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

  const diffRegion = document.createElement("div");
  diffRegion.className = "diff-region";
  diffRegion.id = "diff-region";
  diffRegion.innerHTML = '<div class="diff-loading"><span class="spinner"></span> Loading diff…</div>';

  const actions = buildActionBar(f);

  el.append(header, aiPanel, diffRegion, actions);

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
      outputFormat: "side-by-side",
      colorScheme: "dark",      // GitHub-dark palette (matches app theme) — readable contrast
      highlight: false,         // vendored ui-base bundle has no highlight.js; would throw
    });
    ui.draw();
  } catch {
    const ph = document.createElement("div");
    ph.className = "diff-placeholder";
    ph.textContent = "binary file changed";
    region.appendChild(ph);
  }
}

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

function pollJob(path) {
  const ai = aiFor(path);
  if (ai.status !== "running" || !ai.jobId) return;
  ai.timer = setTimeout(async () => {
    let snap;
    try {
      snap = await api("GET", `/job/${ai.jobId}`);
    } catch {
      pollJob(path); // transient; keep polling
      return;
    }
    if (ai.status !== "running") return; // cancelled meanwhile
    ai.elapsed = snap.elapsed;
    if (snap.status === "done") {
      ai.status = "done";
      if (ai.mode === "ask") ai.qa = { q: ai.question, a: snap.result || "" };
      else ai.result = snap.result || "";
      if (isCurrent(path)) renderAiPanel(path);
      announce("AI response ready");
    } else if (snap.status === "error") {
      ai.status = "error";
      ai.error = snap.error || "claude call failed";
      if (isCurrent(path)) renderAiPanel(path);
      announce("AI request failed");
    } else {
      if (isCurrent(path)) renderAiPanel(path); // refresh elapsed timer
      pollJob(path);
    }
  }, POLL_MS);
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
  if (ai.mode === "explain") body.textContent = ai.result || "";
  else if (ai.mode === "ask") body.textContent = ai.result || ""; // (summary stays visible below if present)
  else body.textContent = ai.result || (ai.status === "idle" ? "Loading summary…" : "");
  el.appendChild(body);

  // Ask Q/A block (State C) shown beneath whatever body we have.
  if (ai.qa) {
    el.appendChild(divider());
    const qa = document.createElement("div");
    qa.className = "ai-qa";
    qa.innerHTML = `<div class="ai-q">Q: ${escapeHtml(ai.qa.q)}</div><div>A: ${escapeHtml(ai.qa.a)}</div>`;
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
  openModal({
    title: `Comment on  ${f.filename}`,
    render: (modal, body) => {
      body.className = "modal-body";
      const lbl = document.createElement("label");
      lbl.textContent = "Comment";
      const ta = document.createElement("textarea");
      ta.className = "textarea";
      ta.id = "comment-text";
      ta.placeholder = "Add a comment scoped to this file…";
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
          const res = await api("POST", "/comment", { ...prKey(), path: f.filename, text });
          if (res && res.ok) {
            State.review.comments = (State.review.comments || 0) + 1;
            closeModal();
            toast("Comment posted");
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
// Global keyboard shortcuts: v e a c f s b j k q
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
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  if (activeScreen === "submit") {
    if (e.key === "q" || e.key === "Escape") { if (!isTyping(e)) { e.preventDefault(); show("review"); } }
    return;
  }
  if (activeScreen !== "review") return;
  if (isTyping(e)) return;

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
