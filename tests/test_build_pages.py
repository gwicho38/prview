"""Hosted-build tests. The staging step is the only thing standing between a
non-stdlib import and a page that breaks in someone's browser, so it is tested
like production code."""
import re

import pytest

import scripts.build_pages as bp


def test_stdlib_only_passes_for_the_real_pure_core():
    sources = {n: (bp.ROOT / "prview" / n).read_text() for n in bp.PURE_MODULES}
    bp.assert_stdlib_only(sources)


def test_stdlib_only_rejects_a_third_party_import():
    with pytest.raises(SystemExit) as e:
        bp.assert_stdlib_only({"core.py": "import httpx\n"})
    assert "httpx" in str(e.value)


def test_stdlib_only_allows_relative_and_stdlib_imports():
    bp.assert_stdlib_only({"order.py": "import re\nfrom .core import FileDiff\nfrom typing import Any\n"})


def test_bootstrap_hides_the_tab_that_needs_a_local_cli():
    assert "window.__prviewNoRepowise = true;" in bp._BOOTSTRAP


def _real_shell():
    return (bp.ROOT / "prview" / "static" / "index.html").read_text()


def test_rewrite_index_points_assets_at_the_same_directory():
    html = bp.rewrite_index(_real_shell())
    assert 'href="./styles.css"' in html
    assert "/static/" not in html


def test_rewrite_index_loads_the_app_exactly_once_and_after_the_transport():
    # app.js declares consts at top level: a second load throws
    # "Identifier ... has already been declared" and silently halves the page.
    html = bp.rewrite_index(_real_shell())
    assert html.count("app.js") == 1, "app.js must appear only in the bootstrap injection"
    assert '<script src="./app.js"></script>' not in html
    assert html.index("installTransport") < html.index('app.src = "./app.js"')


def test_rewrite_index_fails_loudly_on_an_unexpected_shell():
    with pytest.raises(SystemExit):
        bp.rewrite_index("<html>no body close</html>")
    with pytest.raises(SystemExit, match="double-load"):
        bp.rewrite_index("<title>prview</title></body>")


def test_build_stages_every_asset_the_page_needs(tmp_path):
    import shutil
    shutil.copytree(bp.ROOT / "prview", tmp_path / "prview",
                    ignore=shutil.ignore_patterns("__pycache__"))
    out = bp.build(tmp_path)
    for name in ("index.html", "app.js", "styles.css", "llm-worker.js", ".nojekyll"):
        assert (out / name).is_file(), name
    for name in ("__init__.py", *bp.PURE_MODULES):
        assert (out / "prview" / name).is_file(), name
    assert (out / "vendor" / "diff2html.min.js").is_file()
    assert "def parse_diff" in (out / "prview" / "core.py").read_text()


def test_hosted_bootstrap_points_the_worker_at_the_page_directory():
    html = bp.rewrite_index(_real_shell())
    assert 'window.__prviewWorkerUrl = "./llm-worker.js"' in html


def test_hosted_build_reviews_with_the_browser_model_by_default():
    html = bp.rewrite_index(_real_shell())
    assert 'window.__prviewDefaultEngine = "browser"' in html
    assert "window.__prviewNoClaude = true" in html


def test_asset_urls_carry_a_content_stamp(tmp_path):
    import shutil
    shutil.copytree(bp.ROOT / "prview", tmp_path / "prview",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(bp.ROOT / "pages" / "adapter.js", tmp_path / "adapter-src.js")
    (tmp_path / "pages").mkdir(exist_ok=True)
    shutil.copy2(bp.ROOT / "pages" / "adapter.js", tmp_path / "pages" / "adapter.js")
    html = (bp.build(tmp_path) / "index.html").read_text()
    # A returning visitor otherwise runs a cached app.js against a new index.html.
    assert re.search(r'app\.js\?v=[0-9a-f]{8}', html), html[-600:]
    assert re.search(r'styles\.css\?v=[0-9a-f]{8}', html)
    assert re.search(r'llm-worker\.js\?v=[0-9a-f]{8}', html)
    assert re.search(r'adapter\.js\?v=[0-9a-f]{8}', html)


def test_the_stamp_changes_when_the_asset_changes():
    assert bp._stamp("one") != bp._stamp("two")
    assert bp._stamp("same") == bp._stamp("same")


def _adapter():
    return (bp.ROOT / "pages" / "adapter.js").read_text()


def test_remembering_a_token_is_opt_in():
    assert 'id="gh-remember"' in bp._BOOTSTRAP
    assert 'type="checkbox" id="gh-remember" />' in bp._BOOTSTRAP, "must not ship checked"
    assert "remember.checked" in bp._BOOTSTRAP


def test_a_saved_token_is_verified_and_attributed_before_a_pr_load_needs_it():
    assert "verifyGhToken" in bp._BOOTSTRAP
    assert "Signed in as @" in bp._BOOTSTRAP
    assert "export async function verifyGhToken" in _adapter()
    # Fire-and-forget here would let app.js spend a revoked remembered token on a
    # PR load while /user is still in flight — the failure this bar exists to prevent.
    assert "await useToken(ghToken(), ghTokenIsRemembered());" in bp._BOOTSTRAP
    assert bp._BOOTSTRAP.index("await useToken") < bp._BOOTSTRAP.index('app.src = "./app.js"')


def test_verification_does_not_claim_to_know_the_tokens_scopes():
    src = _adapter()
    assert 'res.headers.get("x-oauth-scopes")' not in src
    assert "scopes:" not in src


def test_only_one_store_ever_holds_the_token():
    src = _adapter()
    assert "sessionStorage.removeItem(TOKEN_KEY);\n    localStorage.removeItem(TOKEN_KEY);" in src
    assert "(remember ? localStorage : sessionStorage).setItem(TOKEN_KEY, token)" in src


def test_github_refusals_are_told_apart():
    src = _adapter()
    # Each of these is a different fix for the reviewer, so each needs its own message.
    assert "GitHub rejected the token" in src                       # 401
    assert "GitHub rate limit reached" in src                       # 403 + no quota
    assert "requires the token to be SSO-authorized" in src         # 403 + SAML org
    assert "Contents: read + Pull requests: read" in src            # 403 + missing scope
    assert "if the repository is private, add a GitHub token" in src  # 404, anonymous


def test_sso_detection_does_not_depend_on_a_header_the_browser_may_not_see():
    src = _adapter()
    assert "/saml|single sign|sso|must be authorized/i.test(message)" in src


def test_the_token_only_ever_reaches_the_github_api():
    targets = re.findall(r"fetch\(([^,)]+)", _adapter())
    assert targets
    for t in targets:
        assert t.startswith("`${GH}") or t.startswith("`./"), f"unexpected fetch target: {t}"
