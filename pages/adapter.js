/* Serverless transport for the hosted build.
 *
 * prview's UI talks to one function, `rawFetch(method, path, body)`. Locally that
 * reaches a FastAPI process. Here it reaches this file, which answers the same
 * paths three ways: the GitHub REST API for data, the real prview core running in
 * Pyodide for parsing and ordering, and localStorage for review state.
 *
 * The Python is the same code the local app runs — copied in at build time and
 * checked to be stdlib-only, so no logic is reimplemented in JavaScript here. */

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.28.0/full/pyodide.mjs";
const PY_MODULES = ["__init__.py", "core.py", "order.py", "behaviors.py"];
const GH = "https://api.github.com";
const TOKEN_KEY = "prview:gh-token";

let py = null;
let pyReady = null;

export function ghToken() {
  try { return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY) || ""; }
  catch { return ""; }
}

export function ghTokenIsRemembered() {
  try { return !!localStorage.getItem(TOKEN_KEY); } catch { return false; }
}

export function setGhToken(token, remember = false) {
  // sessionStorage by default: a token this page never sends anywhere but
  // api.github.com should not outlive the tab either. Remembering moves it to
  // localStorage, and only ever one store holds it, so the bar's label and
  // Forget cannot disagree about where it is.
  try {
    sessionStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TOKEN_KEY);
    if (token) (remember ? localStorage : sessionStorage).setItem(TOKEN_KEY, token);
  } catch { /* private mode — the token simply stays in memory */ }
}

/** Confirm a token works and say who it belongs to, before a PR load depends on it.
 *  Scopes are deliberately not reported: x-oauth-scopes is not reliably exposed to
 *  browser JS, so an empty value would read as "no scopes" rather than "unknown". */
export async function verifyGhToken(token) {
  const res = await fetch(`${GH}/user`, {
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
  if (!res.ok) throw await ghFailure(res, token);
  const body = await res.json();
  return { login: body.login || "" };
}

async function loadPy(onStatus) {
  if (pyReady) return pyReady;
  pyReady = (async () => {
    onStatus?.("Loading Python core…");
    const { loadPyodide } = await import(PYODIDE);
    py = await loadPyodide({ indexURL: PYODIDE.replace("pyodide.mjs", "") });
    const sources = await Promise.all(
      PY_MODULES.map((name) => fetch(`./prview/${name}`).then((r) => {
        if (!r.ok) throw new Error(`missing ./prview/${name}`);
        return r.text();
      })),
    );
    py.FS.mkdirTree("/home/pyodide/prview");
    PY_MODULES.forEach((name, i) => py.FS.writeFile(`/home/pyodide/prview/${name}`, sources[i]));
    py.runPython(`
import sys
sys.path.insert(0, "/home/pyodide")
import json
import prview.core as core
import prview.order as order
import prview.behaviors as behaviors
`);
    onStatus?.("");
    return py;
  })();
  return pyReady;
}

/** Call a Python helper with JSON in and JSON out, so nothing crosses as a proxy. */
async function pyCall(expr, payload) {
  await loadPy();
  py.globals.set("_payload", JSON.stringify(payload ?? {}));
  const out = py.runPython(`
_in = json.loads(_payload)
json.dumps(${expr})
`);
  return JSON.parse(out);
}

class HttpError extends Error {
  constructor(status, error, hint) {
    super(error);
    this.status = status;
    this.error = error;
    this.hint = hint;
  }
}

/* GitHub answers "no" in several materially different ways, and a reviewer can only
 * act on the difference. The response body always carries the reason; x-github-sso and
 * x-oauth-scopes are not reliably exposed to a browser, so nothing here depends on them. */
async function ghFailure(res, token) {
  const body = await res.json().catch(() => ({}));
  const message = body.message || "";

  if (res.status === 401) {
    return new HttpError(401, "GitHub rejected the token",
      "it is invalid, expired, or revoked");
  }
  if (res.status === 403 || res.status === 429) {
    if (/secondary rate limit/i.test(message)) {
      const retry = res.headers.get("retry-after");
      return new HttpError(res.status, "GitHub secondary rate limit — too many requests at once",
        retry ? `retry in ${retry}s` : "wait a minute and retry");
    }
    if (res.headers.get("x-ratelimit-remaining") === "0") {
      const reset = Number(res.headers.get("x-ratelimit-reset") || 0) * 1000;
      return new HttpError(res.status, "GitHub rate limit reached",
        reset ? `it resets at ${new Date(reset).toLocaleTimeString()}`
              : (token ? "wait and retry" : "add a token to raise the limit"));
    }
    const sso = res.headers.get("x-github-sso") || "";
    if (sso || /saml|single sign|sso|must be authorized/i.test(message)) {
      const url = (sso.match(/url=(\S+)/) || [])[1];
      return new HttpError(403, "This organization requires the token to be SSO-authorized",
        url ? `authorize it at ${url}`
            : "authorize the token for the organization in GitHub's token settings");
    }
    return new HttpError(403, message || "GitHub refused the request",
      "the token needs `repo` (classic) or Contents: read + Pull requests: read (fine-grained)");
  }
  if (res.status === 404) {
    return new HttpError(404, "Not found on GitHub", token
      ? "check the PR reference, or that this token can see it — a private repo needs `repo` / Contents + Pull requests read, and an SSO org needs the token authorized"
      : "check the PR reference — if the repository is private, add a GitHub token above");
  }
  return new HttpError(res.status, `GitHub returned ${res.status}`, message);
}

async function gh(path, { method = "GET", accept = "application/vnd.github+json", body } = {}) {
  const token = ghToken();
  const headers = { Accept: accept, "X-GitHub-Api-Version": "2022-11-28" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`${GH}${path}`, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw await ghFailure(res, token);
  return accept.includes("json") ? res.json() : res.text();
}

// ---- review state: the local app's ~/.prview/state, in localStorage ----------
const stateKey = (o, r, n) => `prview:state:${o}/${r}#${n}`;

function loadState(o, r, n) {
  const empty = { viewed: [], flagged: {}, comments: 0, comment_threads: {}, submitted: false };
  try { return { ...empty, ...JSON.parse(localStorage.getItem(stateKey(o, r, n)) || "{}") }; }
  catch { return empty; }
}

function saveState(o, r, n, state) {
  try { localStorage.setItem(stateKey(o, r, n), JSON.stringify(state)); } catch { /* best-effort */ }
  try {
    const index = JSON.parse(localStorage.getItem("prview:reviews") || "[]")
      .filter((row) => !(row.owner === o && row.repo === r && row.number === n));
    index.unshift({ owner: o, repo: r, number: n, ...state, updated_at: new Date().toISOString() });
    localStorage.setItem("prview:reviews", JSON.stringify(index.slice(0, 50)));
  } catch { /* best-effort */ }
}

function mutateState(o, r, n, fn) {
  const next = fn(loadState(o, r, n));
  saveState(o, r, n, next);
  return next;
}

// ---- the PR itself ----------------------------------------------------------
const prCache = new Map();          // "o/r#n" -> {pr, files}
const behaviorCache = new Map();    // "o/r#n@sha" -> behaviors

async function loadPr(owner, repo, number) {
  const [meta, diff] = await Promise.all([
    gh(`/repos/${owner}/${repo}/pulls/${number}`),
    gh(`/repos/${owner}/${repo}/pulls/${number}`, { accept: "application/vnd.github.v3.diff" }),
  ]);
  const files = await pyCall("[f.__dict__ for f in core.parse_diff(_in['raw'])]", { raw: diff });
  files.sort((a, b) => (b.additions + b.deletions) - (a.additions + a.deletions));
  const pr = {
    owner, repo, number,
    title: meta.title || "", author: meta.user?.login || "", body: meta.body || "",
    base: meta.base?.ref || "", head: meta.head?.ref || "", head_sha: meta.head?.sha || "",
    state: meta.merged_at ? "MERGED" : (meta.state || "").toUpperCase(),
    review_decision: "", ci_status: "",
    additions: meta.additions || 0, deletions: meta.deletions || 0,
    changed_files: meta.changed_files || files.length,
  };
  prCache.set(`${owner}/${repo}#${number}`, { pr, files });
  return { pr, files };
}

function cachedPr(owner, repo, number) {
  const hit = prCache.get(`${owner}/${repo}#${number}`);
  if (!hit) throw new HttpError(409, "PR not loaded (cache miss) — reload the PR", "re-enter the PR reference");
  return hit;
}

async function prResponse(owner, repo, number, entry) {
  const state = loadState(owner, repo, number);
  const viewed = new Set(state.viewed);
  const files = entry.files.map((f) => ({
    filename: f.filename, additions: f.additions, deletions: f.deletions,
    flagged: Object.hasOwn(state.flagged, f.filename), flag_note: state.flagged[f.filename] || "",
    viewed: viewed.has(f.filename),
    comments: (state.comment_threads || {})[f.filename] || [],
  }));
  const orders = await pyCall(
    "order.orders_map([core.FileDiff(**f) for f in _in['files']])",
    { files: entry.files.map((f) => ({ filename: f.filename, diff_text: f.diff_text,
                                       additions: f.additions, deletions: f.deletions })) },
  );
  return { pr: entry.pr, files, state, orders };
}

async function behaviorsFor(owner, repo, number) {
  const { pr, files } = cachedPr(owner, repo, number);
  const key = `${owner}/${repo}#${number}@${pr.head_sha}`;
  const commitsRaw = await gh(`/repos/${owner}/${repo}/pulls/${number}/commits?per_page=100`);
  const commits = commitsRaw.map((c) => ({
    sha: c.sha,
    subject: (c.commit?.message || "").split("\n")[0].trim(),
    is_merge: (c.parents || []).length > 1,
  }));
  const groupable = await pyCall("behaviors.is_groupable(_in['commits'])", { commits });
  if (!behaviorCache.has(key)) {
    const authored = commits.filter((c) => !c.is_merge);
    const lists = await Promise.all(
      authored.map((c) => gh(`/repos/${owner}/${repo}/commits/${c.sha}`)
        .then((d) => [c.sha, (d.files || []).map((f) => f.filename)])),
    );
    const derived = await pyCall(
      "[{'id': b.id, 'title': b.title, 'filenames': list(b.filenames), 'also_in': b.also_in, 'noise': b.noise} "
      + "for b in behaviors.behaviors_from_commits(_in['commits'], _in['files_by_sha'], set(_in['pr_files']))]",
      { commits, files_by_sha: Object.fromEntries(lists), pr_files: files.map((f) => f.filename) },
    );
    behaviorCache.set(key, derived);
  }
  return { behaviors: behaviorCache.get(key), head_sha: pr.head_sha, groupable };
}

// ---- routes -----------------------------------------------------------------
const UNAVAILABLE = {
  error: "Not available in the hosted app",
  hint: "repowise indexes a local clone and claude runs as a local process — run prview locally for these",
};

function parsePath(path) {
  const [route, query] = path.split("?");
  return [route.split("/").filter(Boolean), new URLSearchParams(query || "")];
}

async function route(method, path, body) {
  const [seg, q] = parsePath(path);

  if (seg[0] === "repowise") throw new HttpError(501, UNAVAILABLE.error, UNAVAILABLE.hint);

  if (method === "GET" && seg[0] === "reviews") {
    try { return JSON.parse(localStorage.getItem("prview:reviews") || "[]"); } catch { return []; }
  }

  if (method === "POST" && seg[0] === "pr" && seg.length === 1) {
    const ref = await pyCall("list(core.parse_pr_ref(_in['ref']))", { ref: body.ref });
    const [owner, repo, number] = ref;
    return prResponse(owner, repo, number, await loadPr(owner, repo, number));
  }

  if (seg[0] === "pr" && seg.length >= 4) {
    const [, owner, repo, nStr] = seg;
    const number = Number(nStr);
    if (method === "GET" && seg.length === 4) {
      return prResponse(owner, repo, number, await loadPr(owner, repo, number));
    }
    if (method === "GET" && seg[4] === "behaviors") return behaviorsFor(owner, repo, number);
    if (method === "GET" && seg[4] === "file" && seg.length === 5) {
      const { files } = cachedPr(owner, repo, number);
      const fd = files.find((f) => f.filename === q.get("path"));
      if (!fd) throw new HttpError(404, `File not in PR: ${q.get("path")}`);
      const state = loadState(owner, repo, number);
      return {
        filename: fd.filename, additions: fd.additions, deletions: fd.deletions,
        flagged: Object.hasOwn(state.flagged, fd.filename), flag_note: state.flagged[fd.filename] || "",
        viewed: state.viewed.includes(fd.filename),
        comments: (state.comment_threads || {})[fd.filename] || [],
        diff_text: fd.diff_text,
      };
    }
    if (method === "GET" && seg[4] === "file" && seg[5] === "full") {
      const { pr, files } = cachedPr(owner, repo, number);
      const filename = q.get("path");
      const fd = files.find((f) => f.filename === filename);
      if (!fd) throw new HttpError(404, `File not in PR: ${filename}`);
      const content = await gh(
        `/repos/${owner}/${repo}/contents/${filename.split("/").map(encodeURIComponent).join("/")}?ref=${pr.head_sha}`,
        { accept: "application/vnd.github.raw" },
      );
      const added = await pyCall("core.added_line_numbers(_in['diff'])", { diff: fd.diff_text });
      return { content, added_lines: added };
    }
  }

  if (method === "GET" && seg[0] === "state" && seg.length === 4) {
    return loadState(seg[1], seg[2], Number(seg[3]));
  }

  if (method === "POST" && seg[0] === "ai" && seg[1] === "prompt") {
    const { owner, repo, number, kind, path: file, question, selection } = body;
    const entry = cachedPr(owner, repo, number);
    if (kind === "overview") {
      return { prompt: await pyCall(
        "core.build_overview_prompt(core.PRInfo(**_in['pr']), [core.FileDiff(**f) for f in _in['files']])",
        { pr: entry.pr, files: entry.files }) };
    }
    const fd = entry.files.find((f) => f.filename === file);
    if (!fd) throw new HttpError(404, `File not in PR: ${file}`);
    const builders = {
      summary: "core.build_summary_prompt(_pr, _fd)",
      explain: "core.build_explain_prompt(_pr, _fd)",
      ask: "core.build_ask_prompt(_pr, _fd, _in['question'])",
      "explain-selection": "core.build_explain_selection_prompt(_pr, _fd, _in['selection'])",
    };
    const build = builders[kind];
    if (!build) throw new HttpError(400, `Unknown prompt kind: ${kind}`);
    await loadPy();
    py.globals.set("_payload", JSON.stringify({ pr: entry.pr, fd, question, selection }));
    const prompt = py.runPython(`
_in = json.loads(_payload)
_pr = core.PRInfo(**_in["pr"])
_fd = core.FileDiff(**{k: v for k, v in _in["fd"].items() if k in {"filename", "diff_text", "additions", "deletions"}})
${build}
`);
    return { prompt };
  }

  // No server-side overview cache here: the client generates one per session
  // with the in-browser model, so an empty answer means "nothing stored yet".
  if (method === "GET" && seg[0] === "overview" && seg.length === 4) return {};

  if (method === "POST" && seg[0] === "overview" && seg[1] === "comment") {
    const { owner, repo, number, markdown } = body;
    if (!markdown) {
      throw new HttpError(404, "no overview generated for this PR", "generate the overview first");
    }
    await gh(`/repos/${owner}/${repo}/issues/${number}/comments`,
             { method: "POST", body: { body: markdown } });
    return { ok: true };
  }

  // Every AI path other than the prompt needs a local process.
  if (seg[0] === "ai" || seg[0] === "job") {
    throw new HttpError(501, "This engine needs the local app",
      "switch the AI engine to `in-browser`, which runs without a server");
  }

  if (method === "POST" && seg[0] === "file" && seg[1] === "viewed") {
    const { owner, repo, number, path: file } = body;
    const next = mutateState(owner, repo, number, (st) => ({
      ...st,
      viewed: st.viewed.includes(file) ? st.viewed.filter((v) => v !== file) : [...st.viewed, file],
    }));
    return { viewed: next.viewed.includes(file), synced: false };
  }

  if (method === "POST" && seg[0] === "file" && seg[1] === "flag") {
    const { owner, repo, number, path: file, note } = body;
    const next = mutateState(owner, repo, number, (st) => {
      const flagged = { ...st.flagged };
      if (Object.hasOwn(flagged, file)) delete flagged[file];
      else flagged[file] = note || "";
      return { ...st, flagged };
    });
    return { flagged: Object.hasOwn(next.flagged, file), flag_note: next.flagged[file] || "" };
  }

  if (method === "POST" && (seg[0] === "comment" || (seg[0] === "behaviors" && seg[1] === "comment"))) {
    if (!ghToken()) throw new HttpError(401, "A GitHub token is needed to post", "add one from the header");
    const { owner, repo, number, text } = body;
    const entry = cachedPr(owner, repo, number);
    let file = body.path;
    let anchor = { line: body.line, start_line: body.start_line, side: body.side || "RIGHT" };
    let comment = text;
    if (seg[0] === "behaviors") {
      const { behaviors: list } = await behaviorsFor(owner, repo, number);
      const b = list.find((x) => x.id === body.behavior_id);
      if (!b) throw new HttpError(404, `Behavior not in PR: ${body.behavior_id}`);
      comment = `**On behavior: ${b.title}**\n(${b.filenames.join(", ")})\n\n${text}`;
      const ranked = await pyCall(
        "sorted(_in['names'], key=lambda n: order.story_tier(n))", { names: b.filenames });
      for (const name of ranked) {
        const fd = entry.files.find((f) => f.filename === name);
        if (!fd) continue;
        const span = await pyCall("core.first_hunk_range(_in['diff'])", { diff: fd.diff_text });
        if (span) { file = name; anchor = { start_line: span[0], line: span[1], side: span[2] }; break; }
      }
    }
    if (anchor.line) {
      await gh(`/repos/${owner}/${repo}/pulls/${number}/comments`, {
        method: "POST",
        body: {
          body: comment, commit_id: entry.pr.head_sha, path: file, line: anchor.line, side: anchor.side,
          ...(anchor.start_line && anchor.start_line < anchor.line
            ? { start_line: anchor.start_line, start_side: anchor.side } : {}),
        },
      });
    } else {
      await gh(`/repos/${owner}/${repo}/issues/${number}/comments`, {
        method: "POST", body: { body: seg[0] === "behaviors" ? comment : `**${file}**\n\n${comment}` },
      });
    }
    mutateState(owner, repo, number, (st) => {
      const threads = { ...(st.comment_threads || {}) };
      threads[file] = [...(threads[file] || []), { text, line: anchor.line ?? null, start_line: anchor.start_line ?? null }];
      return { ...st, comments: (st.comments || 0) + 1, comment_threads: threads };
    });
    return seg[0] === "behaviors"
      ? { ok: true, anchored: !!anchor.line, path: file, line: anchor.line ?? null }
      : { ok: true };
  }

  if (method === "POST" && seg[0] === "review" && seg[1] === "submit") {
    if (!ghToken()) throw new HttpError(401, "A GitHub token is needed to submit", "add one from the header");
    const { owner, repo, number, event, body: reviewBody } = body;
    const res = await gh(`/repos/${owner}/${repo}/pulls/${number}/reviews`, {
      method: "POST", body: { event: event || "COMMENT", body: reviewBody || "" },
    });
    mutateState(owner, repo, number, (st) => ({ ...st, submitted: true }));
    return { ok: true, url: res.html_url || "" };
  }

  if (method === "POST" && seg[0] === "review" && seg[1] === "archive") {
    const { owner, repo, number } = body;
    mutateState(owner, repo, number, (st) => ({ ...st, archived: true }));
    return { ok: true };
  }

  throw new HttpError(404, `No hosted route for ${method} ${path}`,
    "this part of prview needs the local app");
}

export function installTransport(onStatus) {
  loadPy(onStatus);   // start the download immediately; routes await it anyway
  window.__prviewTransport = async (method, path, body) => {
    try {
      return await route(method, path, body);
    } catch (e) {
      if (e instanceof HttpError) {
        const err = new Error(e.error);
        err.status = e.status;
        err.hint = e.hint;
        throw err;
      }
      throw e;
    }
  };
}
