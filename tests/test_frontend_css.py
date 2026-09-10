"""Source-level assertions on static/styles.css.

Only rules whose absence is a visible bug, not styling taste. The sidebar is a
fixed 320px column with overflow:auto, so a flex item that refuses to shrink
does not widen the layout — it silently clips its own siblings out of view.
"""
from pathlib import Path

CSS = (Path(__file__).parent.parent / "prview" / "static" / "styles.css").read_text()


def _rule(selector: str) -> str:
    start = CSS.index(selector + " {")
    return CSS[start:CSS.index("}", start)]


def test_the_sidebar_column_is_fixed_width():
    # The reason the rules below matter: the column cannot grow to fit content.
    assert "grid-template-columns: 320px 1fr;" in CSS


def test_a_behavior_title_can_shrink_below_its_content_width():
    # A flex item's automatic minimum is its content width, so without this a
    # long commit subject widens the row and pushes the file count and comment
    # button outside the visible column.
    assert "min-width: 0" in _rule(".fl-behavior-caret")


def test_a_long_behavior_title_wraps_rather_than_clipping():
    caret = _rule(".fl-behavior-caret")
    assert "overflow-wrap: anywhere" in caret
    assert "white-space: normal" in caret


def test_the_behavior_row_wraps_so_its_meta_is_never_pushed_out():
    assert "flex-wrap: wrap" in _rule(".fl-behavior")


def test_the_filter_controls_wrap():
    # Order + Grouped + Name + Tests is four controls in a 320px column.
    assert "flex-wrap: wrap" in _rule(".fl-head-controls")
