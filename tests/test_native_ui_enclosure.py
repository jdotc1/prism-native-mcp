"""Selection guardrails using tiny synthetic UI bitmaps and recorded events."""

from __future__ import annotations

import copy
import json
import math
import sys
from types import SimpleNamespace

import pytest

from prism_mcp import native_ui, placement, runtime
from prism_mcp.errors import PrismError


def forbidden(*args, **kwargs):
    raise AssertionError("This test must never invoke native application control")


class Bitmap:
    """Only UI handles are represented; these bytes contain no plotted data."""

    def __init__(self, scale=1, boxes=None, fmt=2, sample_bits=8, alpha=True, short_stride=False, truncated=False):
        self.scale, self.fmt, self.sample_bits, self.alpha = scale, fmt, sample_bits, alpha
        self.width, self.height = 200 * scale, 180 * scale
        self.stride = self.width * 4 - int(short_stride)
        data = bytearray([255]) * (self.stride * self.height)
        if boxes is None:
            boxes = [(30, 30), (130, 30), (30, 120), (130, 120)]
        for left, top in boxes:
            for y in range(top * scale, (top + 7) * scale):
                for x in range(left * scale, (left + 8) * scale):
                    offset = y * self.stride + x * 4
                    data[offset:offset + 4] = bytes((60, 130, 220, 255))
        self.data = bytes(data[:-1] if truncated else data)

    def samplesPerPixel(self): return 4
    def bitsPerPixel(self): return 32
    def bitsPerSample(self): return self.sample_bits
    def hasAlpha(self): return self.alpha
    def bitmapFormat(self): return self.fmt
    def isPlanar(self): return False
    def colorSpaceName(self): return "NSCalibratedRGBColorSpace"
    def pixelsWide(self): return self.width
    def pixelsHigh(self): return self.height
    def bytesPerRow(self): return self.stride
    def bitmapData(self): return self.data


class Events:
    kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGEventLeftMouseDragged = 1, 2, 3
    kCGMouseButtonLeft, kCGMouseEventClickState, kCGHIDEventTap = 0, 10, 20

    def __init__(self, fail_kind=None):
        self.posted, self.fail_kind = [], fail_kind

    def CGEventCreateMouseEvent(self, source, kind, point, button):
        return {"kind": kind, "point": point}

    def CGEventSetFlags(self, event, flags): event["flags"] = flags
    def CGEventSetIntegerValueField(self, event, field, value): event["click_count"] = value

    def CGEventPost(self, tap, event):
        # Failure after dispatch is uncertain: cleanup must release the button.
        self.posted.append(copy.deepcopy(event))
        if event["kind"] == self.fail_kind:
            raise RuntimeError("Uncertain native event dispatch")


@pytest.fixture(autouse=True)
def isolated_native_surfaces(monkeypatch, tmp_path):
    for module in (native_ui, placement, runtime):
        monkeypatch.setattr(module, "osa", forbidden)
    for name in ("select_text", "focus_canvas", "owned_window", "_active"):
        monkeypatch.setattr(native_ui, name, forbidden)
    monkeypatch.setattr(runtime.subprocess, "run", forbidden)
    monkeypatch.setattr(runtime.subprocess, "Popen", forbidden)
    monkeypatch.setattr(native_ui, "time", SimpleNamespace(sleep=lambda seconds: None))
    monkeypatch.setattr(placement, "enforce", forbidden)
    monkeypatch.setattr(placement, "POLICY", tmp_path / "unused-placement.json")
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace())


def canvas(monkeypatch, bitmap, fail_kind=None):
    bounds = {"X": -640, "Y": 200, "Width": 200, "Height": 180}
    area = {"p": [-630, 210], "s": [180, 160]}
    events, cleared = Events(fail_kind), []
    monkeypatch.setitem(sys.modules, "Quartz", events)
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSBitmapImageRep=SimpleNamespace(imageRepWithContentsOfFile_=lambda path: bitmap)))
    monkeypatch.setattr(native_ui, "select_text", lambda *args, **kwargs: {"selected_title": "Panel A"})
    monkeypatch.setattr(native_ui, "focus_canvas", lambda *args: cleared.append(True))
    monkeypatch.setattr(native_ui, "owned_window", lambda *args: (77, copy.deepcopy(bounds)))
    monkeypatch.setattr(native_ui, "_active", lambda: True)
    monkeypatch.setattr(native_ui.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(native_ui, "osa", lambda script, **kwargs: json.dumps(area) if kwargs.get("jxa") else "true")
    return events, cleared


def select(tmp_path, **options):
    return native_ui.select_enclosed_graphs(4321, tmp_path / "owned.prism", "Panel A", tmp_path / "evidence", **options)


@pytest.mark.parametrize("scale", [1, 2])
def test_verified_rgba_frame_maps_to_the_same_native_rectangle_at_both_scales(tmp_path, monkeypatch, scale):
    events, cleared = canvas(monkeypatch, Bitmap(scale=scale))
    result = select(tmp_path)
    assert result["screen_rectangle"] == [-614, 226, -498, 331]
    assert result["native_group_action_enabled"] is True
    assert cleared
    assert events.posted[0]["kind"] == Events.kCGEventLeftMouseDown
    assert events.posted[-1]["kind"] == Events.kCGEventLeftMouseUp
    assert any(event["kind"] == Events.kCGEventLeftMouseDragged for event in events.posted)


@pytest.mark.parametrize("options", [
    {"fmt": 0}, {"fmt": 1}, {"fmt": 4}, {"fmt": 1 << 9}, {"sample_bits": 16},
    {"alpha": False}, {"short_stride": True}, {"truncated": True},
])
def test_unverified_bitmap_layout_cannot_start_a_marquee(tmp_path, monkeypatch, options):
    events, cleared = canvas(monkeypatch, Bitmap(**options))
    with pytest.raises(PrismError) as error:
        select(tmp_path)
    assert error.value.code == "UNSUPPORTED_SELECTION_FRAME"
    assert not cleared
    assert not events.posted


@pytest.mark.parametrize("boxes", [
    [(30, 30), (130, 30), (30, 120)],
    [(30, 30), (130, 30), (30, 120), (130, 120), (170, 70)],
    [(30, 30), (130, 30), (30, 120), (140, 120)],
    [(12, 30), (130, 30), (12, 120), (130, 120)],
])
def test_missing_extra_misaligned_or_clipped_handles_cannot_start_a_marquee(tmp_path, monkeypatch, boxes):
    events, cleared = canvas(monkeypatch, Bitmap(boxes=boxes))
    with pytest.raises(PrismError) as error:
        select(tmp_path)
    assert error.value.code == "UNSUPPORTED_SELECTION_FRAME"
    assert not cleared
    assert not events.posted


@pytest.mark.parametrize("fail_kind", [Events.kCGEventLeftMouseDown, Events.kCGEventLeftMouseDragged])
def test_uncertain_mouse_dispatch_always_attempts_button_release(tmp_path, monkeypatch, fail_kind):
    events, _ = canvas(monkeypatch, Bitmap(), fail_kind=fail_kind)
    with pytest.raises(RuntimeError, match="Uncertain native event dispatch"):
        select(tmp_path)
    assert events.posted[-1]["kind"] == Events.kCGEventLeftMouseUp
    assert not (tmp_path / "evidence" / "pair-selection.json").exists()


def test_window_move_during_selection_clearing_prevents_drag(tmp_path, monkeypatch):
    events, cleared = canvas(monkeypatch, Bitmap())
    initial_window = native_ui.owned_window

    def window(*args):
        number, bounds = initial_window(*args)
        if cleared:
            bounds["X"] += 1
        return number, bounds

    monkeypatch.setattr(native_ui, "owned_window", window)
    with pytest.raises(PrismError) as error:
        select(tmp_path)
    assert error.value.code == "UI_TARGET_MISMATCH"
    assert not events.posted


def recorded_frame(rectangle):
    return {
        "window_number": 77,
        "window_bounds": {"X": -640, "Y": 200, "Width": 200, "Height": 180},
        "screen_rectangle": rectangle,
    }


def test_frame_only_returns_owned_frame_without_clearing_or_dragging(tmp_path, monkeypatch):
    events, cleared = canvas(monkeypatch, Bitmap())
    result = select(tmp_path, frame_only=True)
    expected = recorded_frame([-614, 226, -498, 331])
    assert {key: result[key] for key in expected} == expected
    assert result["marquee_dispatched"] is False
    assert "native_group_action_enabled" not in result
    assert not cleared
    assert not events.posted
    assert not (tmp_path / "evidence" / "pair-selection.json").exists()


def test_frame_only_can_record_visible_part_of_left_clipped_group(tmp_path, monkeypatch):
    bitmap = Bitmap(boxes=[(5, 30), (130, 30), (5, 120), (130, 120)])
    events, cleared = canvas(monkeypatch, bitmap)
    result = select(tmp_path, frame_only=True)
    assert result["screen_rectangle"][0] < -630  # Conservative edge extends past the viewport.
    assert result["window_number"] == 77
    assert result["marquee_dispatched"] is False
    assert not cleared
    assert not events.posted


@pytest.mark.parametrize("rectangle", [
    [-620, 220, -610, 240],  # Partial overlap.
    [-620, 220, -490, 340],  # Previous frame contains the candidate.
    [-600, 240, -550, 300],  # Candidate contains the previous frame.
])
def test_any_overlap_with_previous_frame_prevents_selection(tmp_path, monkeypatch, rectangle):
    events, cleared = canvas(monkeypatch, Bitmap())
    with pytest.raises(PrismError) as error:
        select(tmp_path, excluded_frames=[recorded_frame(rectangle)])
    assert error.value.code == "UI_SELECTION_OVERLAP"
    assert not cleared
    assert not events.posted


@pytest.mark.parametrize("change", ["window_number", "position", "size"])
def test_exclusion_from_another_window_or_geometry_cannot_authorize_selection(tmp_path, monkeypatch, change):
    events, cleared = canvas(monkeypatch, Bitmap())
    prior = recorded_frame([-630, 240, -620, 300])
    if change == "window_number":
        prior["window_number"] = 78
    elif change == "position":
        prior["window_bounds"]["X"] += 1
    else:
        prior["window_bounds"]["Width"] += 1
    with pytest.raises(PrismError) as error:
        select(tmp_path, excluded_frames=[prior])
    assert error.value.code == "UI_TARGET_MISMATCH"
    assert not cleared
    assert not events.posted


@pytest.mark.parametrize("rectangle", [
    [-630, 240, -620, 300],  # Separation in X is sufficient.
    [-600, 340, -550, 360],  # Separation in Y is sufficient.
])
def test_disjoint_frame_in_same_window_allows_selection(tmp_path, monkeypatch, rectangle):
    events, _ = canvas(monkeypatch, Bitmap())
    result = select(tmp_path, excluded_frames=[recorded_frame(rectangle)])
    assert result["excluded_previous_panel_count"] == 1
    assert result["native_group_action_enabled"] is True
    assert events.posted[-1]["kind"] == Events.kCGEventLeftMouseUp


@pytest.mark.parametrize("rectangle", [
    [-600, 240, math.nan, 300],
    [math.inf, 240, math.inf, 300],
    [-450, 240, -550, 300],
    [-600, 240, -550],
])
def test_malformed_excluded_rectangle_fails_closed_before_selection(tmp_path, monkeypatch, rectangle):
    events, cleared = canvas(monkeypatch, Bitmap())
    with pytest.raises(PrismError) as error:
        select(tmp_path, excluded_frames=[recorded_frame(rectangle)])
    assert error.value.code in {"INVALID_ARGUMENT", "UNSUPPORTED_SELECTION_FRAME", "UI_TARGET_MISMATCH"}
    assert not cleared
    assert not events.posted
