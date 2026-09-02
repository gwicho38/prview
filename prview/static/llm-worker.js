/* In-browser model engine, off the main thread.
 *
 * Generation blocks for seconds per response and would freeze the diff pane, so
 * the model is loaded and run here and only text crosses back. Weights come from
 * the MLC CDN on first use and are then served from the browser's cache — this is
 * the one part of prview that talks to a third party, and it only runs when the
 * user selects the browser engine. */

const WEBLLM = "https://esm.run/@mlc-ai/web-llm";
// Raising this costs KV-cache VRAM on every model, so it buys room for the prompt
// budget in app.js rather than trying to fit a whole PR.
const CONTEXT_WINDOW = 8192;

let engine = null;
let loadedModel = null;
let cancelled = false;

function post(type, payload) {
  self.postMessage({ type, ...payload });
}

async function ensureEngine(model) {
  if (engine && loadedModel === model) return engine;
  const { CreateMLCEngine } = await import(WEBLLM);
  // Every prebuilt model ships overrides.context_window_size = 4096, well under what
  // the models themselves handle. A PR diff overruns it in a few hundred lines.
  engine = await CreateMLCEngine(model, {
    initProgressCallback: (p) => post("progress", { text: p.text, progress: p.progress ?? 0 }),
  }, { context_window_size: CONTEXT_WINDOW });
  loadedModel = model;
  return engine;
}

async function generate({ id, model, prompt }) {
  cancelled = false;
  try {
    const llm = await ensureEngine(model);
    if (cancelled) return post("cancelled", { id });
    post("started", { id });
    const stream = await llm.chat.completions.create({
      messages: [{ role: "user", content: prompt }],
      stream: true,
      temperature: 0.2,
    });
    let text = "";
    for await (const chunk of stream) {
      if (cancelled) {
        // Draining is not enough — the runtime keeps decoding until interrupted.
        try { await llm.interruptGenerate(); } catch { /* best-effort */ }
        return post("cancelled", { id });
      }
      const delta = chunk.choices?.[0]?.delta?.content || "";
      if (!delta) continue;
      text += delta;
      post("token", { id, text });
    }
    post("done", { id, text });
  } catch (e) {
    post("error", { id, error: String((e && e.message) || e) });
  }
}

self.onmessage = (e) => {
  const msg = e.data || {};
  if (msg.type === "generate") generate(msg);
  else if (msg.type === "cancel") cancelled = true;
};
