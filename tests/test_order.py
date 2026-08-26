"""Review-order tests. prview.order is pure: filenames and diff text in,
filename lists out — no fixtures, no I/O."""
import prview.core as core
import prview.order as order


def _fd(filename, diff_text="", additions=0, deletions=0):
    return core.FileDiff(filename=filename, diff_text=diff_text,
                         additions=additions, deletions=deletions)


def test_story_tiers_run_schema_to_generated():
    tiered = [
        ("migrations/0001_init.sql", 0),
        ("prview/api_models.py", 1),
        ("prview/core.py", 2),
        ("prview/server.py", 3),
        ("prview/static/app.js", 4),
        ("pyproject.toml", 5),
        ("tests/test_core.py", 6),
        ("uv.lock", 7),
    ]
    for path, tier in tiered:
        assert order.story_tier(path) == tier, path


def test_unknown_path_lands_in_the_source_tier():
    assert order.story_tier("weird/thing.xyz") == order.DEFAULT_TIER == 2


def test_story_order_puts_definitions_before_call_sites():
    files = [_fd("prview/server.py"), _fd("tests/test_api.py"),
             _fd("prview/api_models.py"), _fd("prview/core.py")]
    assert order.order_names(files, "story") == [
        "prview/api_models.py", "prview/core.py",
        "prview/server.py", "tests/test_api.py",
    ]


def test_story_ties_break_on_churn_then_filename():
    files = [_fd("a/core.py", additions=1), _fd("b/core.py", additions=9),
             _fd("c/core.py", additions=9)]
    assert order.order_names(files, "story") == ["b/core.py", "c/core.py", "a/core.py"]


def test_complexity_counts_decision_points_added():
    plain = _fd("plain.py", "+x = 1\n+y = 2\n", additions=2)
    branchy = _fd("branchy.py", "+if a and b:\n+    for x in y:\n", additions=2)
    assert order.complexity_delta(branchy) > order.complexity_delta(plain)
    assert order.order_names([plain, branchy], "complexity")[0] == "branchy.py"


def test_complexity_credits_removed_decision_points_negatively():
    simplifier = _fd("s.py", "-if a:\n-    while b:\n", deletions=2)
    adder = _fd("a.py", "+if a:\n", additions=1)
    assert order.complexity_delta(simplifier) < 0 < order.complexity_delta(adder)


def test_complexity_breaks_ties_on_churn_so_big_files_outrank_one_liners():
    big = _fd("big.py", "+a\n" * 400, additions=400)
    guard = _fd("guard.py", "+if x: return\n", additions=1)
    assert order.order_names([big, guard], "complexity")[0] == "big.py"


def test_churn_and_alpha_modes():
    files = [_fd("b.py", additions=1), _fd("a.py", additions=50)]
    assert order.order_names(files, "churn") == ["a.py", "b.py"]
    assert order.order_names(files, "alpha") == ["a.py", "b.py"]
    assert order.order_names(files, "alpha") == sorted(f.filename for f in files)


def test_every_mode_is_a_permutation_of_the_changed_set():
    files = [_fd("prview/server.py", "+if a:\n", additions=1),
             _fd("uv.lock", additions=900),
             _fd("prview/core.py", "-while b:\n", deletions=1),
             _fd("tests/test_core.py", additions=12)]
    names = {f.filename for f in files}
    for mode in order.MODES:
        got = order.order_names(files, mode)
        assert len(got) == len(files), mode
        assert set(got) == names, mode


def test_orders_map_covers_every_mode():
    files = [_fd("prview/core.py", additions=3), _fd("tests/t.py", additions=1)]
    orders = order.orders_map(files)
    assert set(orders) == set(order.MODES)
    assert orders["story"] == ["prview/core.py", "tests/t.py"]


def test_unknown_mode_falls_back_to_churn():
    files = [_fd("b.py", additions=1), _fd("a.py", additions=50)]
    assert order.order_names(files, "nonsense") == ["a.py", "b.py"]


def test_default_mode_is_story():
    assert order.DEFAULT_MODE == "story"


def test_a_monorepo_backend_package_is_not_an_adapter():
    # `packages/api/**` is a backend, so its files keep their own tiers.
    assert order.story_tier("packages/api/ultron_api/db/models/channel.py") == 1
    assert order.story_tier("packages/api/ultron_api/services/assigner.py") == 2
    assert order.story_tier("packages/api/ultron_api/main.py") == 3
