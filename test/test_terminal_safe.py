"""Tests for the stdlib-only terminal text sanitizer."""

from kiro_crew.terminal_safe import safe_terminal_text


def test_removes_osc_broad_csi_two_byte_escape_and_controls() -> None:
    value = "a\x1b]0;hidden\x07b" "\x1b[>1;2mc" "\x1bMd" "\x00e\x7ff\x9fg"

    assert safe_terminal_text(value) == "abcdefg"


def test_preserves_newlines_tabs_and_ordinary_unicode() -> None:
    assert safe_terminal_text("one\n\ttwø") == "one\n\ttwø"


def test_caps_with_an_ellipsis() -> None:
    assert safe_terminal_text("abcdef", cap=4) == "abc…"
    assert safe_terminal_text("abcdef", cap=1) == "…"
    assert safe_terminal_text("abcdef", cap=0) == ""


def test_default_cap_is_2000_characters() -> None:
    rendered = safe_terminal_text("x" * 3000)

    assert len(rendered) == 2000
    assert rendered.endswith("…")
