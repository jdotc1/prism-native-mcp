"""Canvas OCR ownership with all native application access disabled."""

from __future__ import annotations

import copy
import math
import sys
from types import SimpleNamespace

import pytest

from prism_mcp import native_ui, placement, runtime
from prism_mcp.errors import PrismError


BOUNDS = {"X": -1200, "Y": 200, "Width": 1000, "Height": 800}
AREA = {"p": [-950, 300], "s": [500, 400]}
# Vision's bottom-left coordinates for this canvas are x=.25..75, y=.375..875.


def forbidden(*args, **kwargs):
    raise AssertionError("This test must never invoke native application control")


@pytest.fixture(autouse=True)
def isolated_native_surfaces(monkeypatch, tmp_path):
    for module in (native_ui, placement, runtime):
        monkeypatch.setattr(module, "osa", forbidden)
    for name in ("_active", "_window_state", "_selection_canvas", "focus_canvas"):
        monkeypatch.setattr(native_ui, name, forbidden)
    monkeypatch.setattr(runtime.subprocess, "run", forbidden)
    monkeypatch.setattr(runtime.subprocess, "Popen", forbidden)
    monkeypatch.setattr(placement, "enforce", forbidden)
    monkeypatch.setattr(placement, "POLICY", tmp_path / "unused-placement.json")
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace(
        kCGWindowListOptionOnScreenOnly=1, kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=forbidden))


def label(bounds, location="canvas"):
    return {"text": "Panel A summary", "bounds": bounds, "location": location}


def test_canvas_filter_excludes_matching_navigator_toolbar_status_and_partial_labels():
    visible = label([.4, .6, .2, .05])
    rows = [
        label([.05, .6, .15, .05], "navigator"),
        label([.4, .9, .2, .05], "toolbar"),
        label([.4, .02, .2, .04], "status"),
        label([.2, .6, .2, .05], "crosses_canvas_edge"),
        label([.7, .6, .1, .05], "crosses_right_edge"),
        visible,
    ]
    before = copy.deepcopy((rows, AREA, BOUNDS))
    result = native_ui._canvas_ocr_rows(rows, AREA, BOUNDS)
    assert result == [visible]
    assert result[0] is visible
    assert (rows, AREA, BOUNDS) == before


def test_canvas_edges_are_inclusive_and_vision_y_is_measured_from_window_bottom():
    edge = label([.25, .375, .5, .5])
    upper = label([.4, .8, .1, .05], "upper_canvas")
    below = label([.4, .2, .1, .05], "below_canvas")
    assert native_ui._canvas_ocr_rows([edge, upper, below], AREA, BOUNDS) == [edge, upper]
    assert native_ui._canvas_ocr_rows([], AREA, BOUNDS) == []


def test_rounding_at_exact_window_edge_is_clamped_without_admitting_outside_labels():
    area = {"p": [-950, 300], "s": [750 + 1e-10, 400]}
    at_edge = label([.99, .6, .01, .05])
    outside = label([1, .6, .001, .05])
    assert native_ui._canvas_ocr_rows([at_edge, outside], area, BOUNDS) == [at_edge]


@pytest.mark.parametrize("area", [
    None, {}, {"p": [-950], "s": [500, 400]},
    {"p": [-950, 300], "s": [500]},
    {"p": [-950, 300], "s": [0, 400]},
    {"p": [-950, 300], "s": [500, -1]},
    {"p": [math.nan, 300], "s": [500, 400]},
    {"p": [-950, math.inf], "s": [500, 400]},
    {"p": [-950, 300], "s": [True, 400]},
    {"p": [-950, 300], "s": ["500", 400]},
    {"p": [-1201, 300], "s": [500, 400]},
    {"p": [-950, 199], "s": [500, 400]},
    {"p": [-950, 300], "s": [751, 400]},
    {"p": [-950, 300], "s": [500, 701]},
])
def test_invalid_or_outside_canvas_geometry_has_a_structured_error(area):
    with pytest.raises(PrismError) as error:
        native_ui._canvas_ocr_rows([], area, BOUNDS)
    assert error.value.code == "TEXT_SELECTION_UNAVAILABLE"


@pytest.mark.parametrize("change", [
    {"X": math.nan}, {"Y": math.inf}, {"Width": 0},
    {"Height": -1}, {"Width": True}, {"Height": "800"},
])
def test_invalid_owned_window_geometry_cannot_define_a_canvas(change):
    with pytest.raises(PrismError) as error:
        native_ui._canvas_ocr_rows([], AREA, {**BOUNDS, **change})
    assert error.value.code == "TEXT_SELECTION_UNAVAILABLE"


@pytest.mark.parametrize("row", [
    None, {}, {"text": "Panel A summary"},
    {"text": 123, "bounds": [.4, .6, .2, .05]},
    label([.4, .6, .2]), label([.4, .6, .2, .05, 0]),
    label([math.nan, .6, .2, .05]), label([.4, math.inf, .2, .05]),
    label([.4, .6, 0, .05]), label([.4, .6, .2, -.05]),
    label([False, .6, .2, .05]), label([.4, .6, ".2", .05]),
])
def test_malformed_recognition_rows_cannot_be_silently_selected_or_discarded(row):
    with pytest.raises(PrismError) as error:
        native_ui._canvas_ocr_rows([label([.4, .6, .2, .05]), row], AREA, BOUNDS)
    assert error.value.code == "TEXT_SELECTION_UNAVAILABLE"


@pytest.mark.parametrize("main", [False, None, "true", 1])
def test_owned_window_requires_literal_axmain_true_before_windowserver_lookup(tmp_path, monkeypatch, main):
    target = tmp_path / "owned.prism"
    monkeypatch.setattr(native_ui, "_window_state", lambda: {"url": target.as_uri(), "main": main})
    with pytest.raises(PrismError) as error:
        native_ui.owned_window(4321, target)
    assert error.value.code == "UI_TARGET_MISMATCH"


@pytest.mark.parametrize("document", ["same_name_elsewhere", "another_document", "missing", "empty"])
def test_owned_window_requires_exact_main_document_before_windowserver_lookup(tmp_path, monkeypatch, document):
    target = tmp_path / "owned.prism"
    urls = {
        "same_name_elsewhere": (tmp_path / "other" / target.name).as_uri(),
        "another_document": (tmp_path / "unrelated.prism").as_uri(),
        "missing": None,
        "empty": "",
    }
    monkeypatch.setattr(native_ui, "_window_state", lambda: {
        "title": target.name, "url": urls[document], "main": True})
    with pytest.raises(PrismError) as error:
        native_ui.owned_window(4321, target)
    assert error.value.code == "UI_TARGET_MISMATCH"


def test_owned_window_resolves_encoded_document_url_and_filters_process_title_layer(tmp_path, monkeypatch):
    target = tmp_path / "checkpoint #2 β.prism"
    monkeypatch.setattr(native_ui, "_window_state", lambda: {"url": target.as_uri(), "main": True})
    window = {"kCGWindowOwnerPID": 4321, "kCGWindowLayer": 0,
              "kCGWindowName": target.name, "kCGWindowNumber": 77,
              "kCGWindowBounds": dict(BOUNDS)}
    rows = [
        {**window, "kCGWindowOwnerPID": 9999},
        {**window, "kCGWindowLayer": 8},
        {**window, "kCGWindowName": "other.prism"},
        window,
    ]
    monkeypatch.setattr(sys.modules["Quartz"], "CGWindowListCopyWindowInfo", lambda *args: rows)
    number, bounds = native_ui.owned_window(4321, target)
    assert number == 77
    assert bounds == BOUNDS and bounds is not window["kCGWindowBounds"]
    assert all(type(value) is float for value in bounds.values())


def test_native_float_subclasses_are_normalized_before_strict_canvas_geometry_checks(tmp_path, monkeypatch):
    class NativeFloat(float):
        """Matches the numeric behavior of PyObjC's native window-bound values."""

    target = tmp_path / "owned.prism"
    monkeypatch.setattr(native_ui, "_window_state", lambda: {"url": target.as_uri(), "main": True})
    raw = {key: NativeFloat(value) for key, value in BOUNDS.items()}
    window = {"kCGWindowOwnerPID": 4321, "kCGWindowLayer": 0,
              "kCGWindowName": target.name, "kCGWindowNumber": 77,
              "kCGWindowBounds": raw}
    monkeypatch.setattr(sys.modules["Quartz"], "CGWindowListCopyWindowInfo", lambda *args: [window])
    number, bounds = native_ui.owned_window(4321, target)
    assert number == 77 and bounds == BOUNDS
    assert all(type(value) is float for value in bounds.values())
    visible = label([.4, .6, .2, .05])
    assert native_ui._canvas_ocr_rows([visible], AREA, bounds) == [visible]
    assert all(type(value) is NativeFloat for value in raw.values())


@pytest.mark.parametrize("raw", [
    {**BOUNDS, "X": True}, {**BOUNDS, "Width": False},
    {**BOUNDS, "X": math.nan}, {**BOUNDS, "Height": math.inf},
    {**BOUNDS, "Width": "1000"}, {**BOUNDS, "Width": 0},
    {**BOUNDS, "Height": -1}, {key: value for key, value in BOUNDS.items() if key != "Y"},
    None,
])
def test_owned_window_rejects_invalid_native_bounds(tmp_path, monkeypatch, raw):
    target = tmp_path / "owned.prism"
    monkeypatch.setattr(native_ui, "_window_state", lambda: {"url": target.as_uri(), "main": True})
    window = {"kCGWindowOwnerPID": 4321, "kCGWindowLayer": 0,
              "kCGWindowName": target.name, "kCGWindowNumber": 77,
              "kCGWindowBounds": raw}
    monkeypatch.setattr(sys.modules["Quartz"], "CGWindowListCopyWindowInfo", lambda *args: [window])
    with pytest.raises(PrismError) as error:
        native_ui.owned_window(4321, target)
    assert error.value.code == "UI_TARGET_MISMATCH"


def test_missing_native_bounds_has_a_structured_ownership_error(tmp_path, monkeypatch):
    target = tmp_path / "owned.prism"
    monkeypatch.setattr(native_ui, "_window_state", lambda: {"url": target.as_uri(), "main": True})
    window = {"kCGWindowOwnerPID": 4321, "kCGWindowLayer": 0,
              "kCGWindowName": target.name, "kCGWindowNumber": 77}
    monkeypatch.setattr(sys.modules["Quartz"], "CGWindowListCopyWindowInfo", lambda *args: [window])
    with pytest.raises(PrismError) as error:
        native_ui.owned_window(4321, target)
    assert error.value.code == "UI_TARGET_MISMATCH"


@pytest.mark.parametrize("count", [0, 2])
def test_owned_main_document_still_requires_one_matching_visible_window(tmp_path, monkeypatch, count):
    target = tmp_path / "owned.prism"
    monkeypatch.setattr(native_ui, "_window_state", lambda: {"url": target.as_uri(), "main": True})
    window = {"kCGWindowOwnerPID": 4321, "kCGWindowLayer": 0,
              "kCGWindowName": target.name, "kCGWindowNumber": 77,
              "kCGWindowBounds": dict(BOUNDS)}
    monkeypatch.setattr(sys.modules["Quartz"], "CGWindowListCopyWindowInfo", lambda *args: [window] * count)
    with pytest.raises(PrismError) as error:
        native_ui.owned_window(4321, target)
    assert error.value.code == "UI_TARGET_MISMATCH"
