"""Hosted-build tests. The staging step is the only thing standing between a
non-stdlib import and a page that breaks in someone's browser, so it is tested
like production code."""
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
