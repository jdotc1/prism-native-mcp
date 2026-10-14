"""Pure text-format and pre-dispatch contracts; no native application calls."""

from __future__ import annotations

import math
import re
import sys
from types import SimpleNamespace

import pytest

from prism_mcp import native_style, native_ui, placement, runtime
from prism_mcp.errors import PrismError


def forbidden(*args, **kwargs):
    raise AssertionError("This test must never dispatch native UI or subprocess work")


@pytest.fixture(autouse=True)
def no_native_calls(monkeypatch, tmp_path):
    for module in (native_style, native_ui, placement, runtime):
        monkeypatch.setattr(module, "osa", forbidden)
    for name in ("foreground", "menu", "_dialog", "contextual_paste", "select_text"):
        monkeypatch.setattr(native_ui, name, forbidden)
    monkeypatch.setattr(native_ui.subprocess, "run", forbidden)
    monkeypatch.setattr(runtime.subprocess, "Popen", forbidden)
    monkeypatch.setattr(placement, "enforce", forbidden)
    monkeypatch.setattr(placement, "POLICY", tmp_path / "unused-placement.json")
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace())


def rtf(text="A native label", **changes):
    options = {"size": 6, "color": "#444444", "bold": False, "italic": False}
    return native_style._rtf(text, **(options | changes))


def test_rtf_escapes_literal_braces_and_backslashes_in_user_text():
    # Apparent RTF instructions in a label must remain visible literal text.
    encoded = rtf(r"A {B} C\D \b1 {\fonttbl injected}").decode("ascii")
    assert encoded.endswith(r"A \{B\} C\\D \\b1 \{\\fonttbl injected\}" + "}")
    assert encoded.startswith(r"{\rtf1\ansi")


@pytest.mark.parametrize(
    "text, signed_utf16_units",
    [
        ("é Δ 한", [233, 916, -10916]),
        ("😀", [-10179, -8704]),
        ("𝄞", [-10188, -8930]),
        ("α😀한", [945, -10179, -8704, -10916]),
    ],
)
def test_rtf_unicode_uses_signed_utf16_with_surrogate_pairs(text, signed_utf16_units):
    encoded = rtf(text)
    assert encoded.isascii()
    assert b"\\uc1" in encoded
    units = [int(value) for value in re.findall(rb"\\u(-?\d+)\?", encoded)]
    assert units == signed_utf16_units
    decoded = b"".join((value % 65536).to_bytes(2, "little") for value in units).decode("utf-16-le")
    assert decoded == text.replace(" ", "")


@pytest.mark.parametrize("alignment, control", [("left", "ql"), ("center", "qc"), ("right", "qr")])
def test_rtf_style_uses_requested_half_points_color_and_alignment(alignment, control):
    encoded = rtf(size=6.5, color="#aB01fF", bold=True, italic=True, alignment=alignment)
    assert b"\\red171\\green1\\blue255;" in encoded
    assert ("\\pard\\" + control + "\\f0\\fs13\\cf1\\b1\\i1 ").encode("ascii") in encoded


@pytest.mark.parametrize("color", ["red", "#12345", "#12GG00", "#12345678", None, 123456, b"#123456", []])
def test_rtf_invalid_colors_raise_structured_errors(color):
    with pytest.raises(PrismError) as error:
        rtf(color=color)
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("size", [3.5, 72.5, 6.25, math.nan, math.inf, True, "6", None])
def test_rtf_invalid_sizes_raise_structured_errors(size):
    with pytest.raises(PrismError) as error:
        rtf(size=size)
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("alignment", ["justify", "", None, []])
def test_rtf_invalid_alignment_raises_structured_error(alignment):
    with pytest.raises(PrismError) as error:
        rtf(alignment=alignment)
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "changes",
    [
        {"graph_index": 0},
        {"graph_index": True},
        {"graph_index": 1.0},
        {"scale_percent": 0},
        {"scale_percent": 1001},
        {"scale_percent": 100.0},
        {"scale_percent": True},
        {"x_inches": -0.01},
        {"x_inches": math.nan},
        {"x_inches": True},
        {"y_inches": 30.01},
        {"y_inches": math.inf},
        {"y_inches": "1"},
    ],
)
def test_graph_placement_rejects_invalid_arguments_before_ui(tmp_path, changes):
    options = {"graph_index": 1, "graph_title": "Owned graph", "x_inches": 1, "y_inches": 1}
    with pytest.raises(PrismError) as error:
        native_style.place_graph(tmp_path / "owned.prism", **(options | changes))
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "changes",
    [
        {"x_inches": -0.01},
        {"x_inches": math.nan},
        {"x_inches": True},
        {"y_inches": 30.01},
        {"y_inches": math.inf},
        {"y_inches": "1"},
        {"font_size": 6.25},
        {"color": "red"},
        {"color": None},
        {"horizontal_anchor": "justify"},
        {"bold": 1},
        {"italic": "false"},
    ],
)
def test_text_annotation_rejects_invalid_arguments_before_ui_or_evidence(tmp_path, changes):
    evidence = tmp_path / "must-not-be-created"
    options = {"text": "A native label", "x_inches": 1, "y_inches": 1}
    with pytest.raises(PrismError) as error:
        native_style.annotate_text(tmp_path / "owned.prism", evidence=evidence, **(options | changes))
    assert error.value.code == "INVALID_ARGUMENT"
    assert not evidence.exists()
