"""Stage the hosted build under pages/.

The page runs the real prview core, not a JavaScript port of it: core.py, order.py
and behaviors.py are copied in and executed by Pyodide, so parsing, ordering,
behavior derivation and prompt building have one implementation and one test suite.

A third-party import in those modules would break in Pyodide, which has no install
step, so `assert_stdlib_only` fails the build here rather than in someone's browser.
"""
import hashlib
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "prview" / "static"
PAGES = ROOT / "pages"

PURE_MODULES = ("core.py", "order.py", "behaviors.py")
COPIED_ASSETS = ("app.js", "styles.css", "llm-worker.js")

# Pyodide ships the standard library and nothing else.
_ALLOWED_IMPORT = re.compile(
    r"^(?:import|from)\s+(?:__future__|re|json|sys|os|time|dataclasses|pathlib|typing"
    r"|collections|functools|enum|itertools|math|textwrap)\b|^from\s+\.",
)

_BOOTSTRAP = """
  <div class="hosted-bar">
    <span class="hosted-note" id="hosted-status">Loading the prview core…</span>
    <input class="text-input hosted-token" id="gh-token" type="password" autocomplete="off"
           spellcheck="false" placeholder="GitHub token (optional — for private PRs, comments)"
           aria-label="GitHub token" />
    <button class="btn" id="gh-token-save">Use token</button>
  </div>
  <script type="module">
    import { installTransport, ghToken, setGhToken } from "./adapter.js";
    // The local app serves the worker from /static; here it sits beside this page.
    window.__prviewWorkerUrl = "./llm-worker.js";
    // No local process is reachable from a hosted page, so claude cannot answer here
    // and the browser model is what reviews a PR.
    window.__prviewDefaultEngine = "browser";
    window.__prviewNoClaude = true;
    const status = document.getElementById("hosted-status");
    installTransport((text) => { status.textContent = text || "Running locally in your browser"; });
    const field = document.getElementById("gh-token");
    if (ghToken()) field.placeholder = "GitHub token saved for this tab";
    document.getElementById("gh-token-save").addEventListener("click", () => {
      setGhToken(field.value.trim());
      field.value = "";
      field.placeholder = ghToken() ? "GitHub token saved for this tab" : "GitHub token (optional)";
      status.textContent = ghToken() ? "Token stored for this tab only" : "Token cleared";
    });
    // Loaded last and on purpose: the UI issues its first request as it boots, and
    // the transport above has to be in place before that happens.
    const app = document.createElement("script");
    app.src = "./app.js";
    document.body.appendChild(app);
  </script>
"""


def assert_stdlib_only(sources: dict[str, str]) -> None:
    """Raise if any staged module imports something Pyodide cannot provide."""
    offenders = []
    for name, text in sources.items():
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if not _ALLOWED_IMPORT.match(stripped):
                offenders.append(f"{name}: {stripped}")
    if offenders:
        raise SystemExit(
            "pure core gained a non-stdlib import; Pyodide cannot install it:\n  "
            + "\n  ".join(offenders)
        )


def _stamp(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def rewrite_index(html: str, stamps: dict[str, str] | None = None) -> str:
    """Point the shell at same-directory assets and boot the serverless transport.

    Asset URLs carry a content stamp. Without it a returning visitor keeps a cached
    app.js while getting the new index.html, and the two disagree about what exists.
    """
    stamps = stamps or {}
    html = html.replace('href="/static/', 'href="./').replace('src="/static/', 'src="./')
    html = html.replace(
        "<title>prview</title>",
        "<title>prview — read a pull request as a story</title>",
    )
    html = html.replace("localhost · gh+claude", "hosted · your browser")
    # The bootstrap injects app.js itself, after the transport exists. Leaving the
    # original tag in place loads the file twice and the second copy dies on
    # duplicate top-level declarations.
    tag = '<script src="./app.js"></script>'
    if tag not in html:
        raise SystemExit(f"index.html no longer carries {tag}; the bootstrap would double-load it")
    html = html.replace(tag, "")
    if "</body>" not in html:
        raise SystemExit("index.html has no </body> to bootstrap into")
    bootstrap = _BOOTSTRAP
    for name, stamp in stamps.items():
        bootstrap = bootstrap.replace(f'"./{name}"', f'"./{name}?v={stamp}"')
        html = html.replace(f'"./{name}"', f'"./{name}?v={stamp}"')
    return html.replace("</body>", bootstrap + "</body>")


def build(root: Path = ROOT) -> Path:
    static, pages = root / "prview" / "static", root / "pages"
    sources = {name: (root / "prview" / name).read_text() for name in PURE_MODULES}
    assert_stdlib_only(sources)

    (pages / "prview").mkdir(parents=True, exist_ok=True)
    (pages / "prview" / "__init__.py").write_text("")
    for name, text in sources.items():
        (pages / "prview" / name).write_text(text)
    for name in COPIED_ASSETS:
        shutil.copy2(static / name, pages / name)
    shutil.copytree(static / "vendor", pages / "vendor", dirs_exist_ok=True)
    stamped = {name: _stamp((pages / name).read_text())
               for name in (*COPIED_ASSETS, "adapter.js", "styles.css")
               if (pages / name).is_file()}
    (pages / "index.html").write_text(
        rewrite_index((static / "index.html").read_text(), stamped))
    (pages / ".nojekyll").write_text("")
    return pages


if __name__ == "__main__":
    out = build()
    print(f"staged {out}")
    for path in sorted(out.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(out)}  {path.stat().st_size}")
    sys.exit(0)
