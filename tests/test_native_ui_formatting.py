"""Pure mocked guardrails for the native formatting helpers.

Every application, Apple Event, event-posting, subprocess, and placement surface
is isolated. These tests establish failure handling, not live Prism behavior.
"""

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


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


@pytest.fixture(autouse=True)
def isolated_native_surfaces(monkeypatch, tmp_path):
    monkeypatch.setattr(native_ui, "osa", forbidden)
    monkeypatch.setattr(runtime, "osa", forbidden)
    monkeypatch.setattr(placement, "osa", forbidden)
    monkeypatch.setattr(native_ui.subprocess, "run", forbidden)
    monkeypatch.setattr(native_ui.subprocess, "Popen", forbidden)
    monkeypatch.setattr(native_ui, "time", Clock())
    monkeypatch.setattr(native_ui, "_active", lambda: False)
    monkeypatch.setattr(placement, "POLICY", tmp_path / "unused-placement.json")
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace())


def color_readback(monkeypatch, text):
    calls = []

    def osa(*args, **kwargs):
        calls.append(args)
        return "" if len(calls) == 1 else text

    monkeypatch.setattr(native_ui, "osa", osa)
    return calls


@pytest.mark.parametrize("color, opacity", [("red", 1), ("#12345", 1), ("#12GG00", 1), (None, 1), ("#FF0000", -0.01), ("#FF0000", 1.01), ("#FF0000", math.nan), ("#FF0000", True)])
def test_invalid_color_input_is_rejected_before_native_dispatch(color, opacity):
    with pytest.raises(PrismError) as error:
        native_ui.native_color("an owned color well", color, opacity)
    assert error.value.code == "INVALID_ARGUMENT"


def test_matching_native_color_readback_returns_observed_evidence(monkeypatch):
    color_readback(monkeypatch, "rgb 1 0 0 0.25")
    result = native_ui.native_color("an owned color well", "#ff0000", 0.25)
    assert result["color"] == "#FF0000"
    assert result["opacity"] == 0.25
    assert result["native_value"] == "rgb 1 0 0 0.25"
    assert result["native_rgba_8bit"] == [255, 0, 0, 64]
    assert result["verification"] == "finite_exact_8bit_rgba"


@pytest.mark.parametrize("text", ["rgb 0 0 1 1", "rgb 1 0 0 0.5", "rgb 1 0 0 0.9960784314", "unreadable color", "rgb 1 0 0", "rgb inf 0 0 1", "rgb 1 0 0 1.001", "1 0 0 1"])
def test_wrong_or_malformed_native_color_does_not_report_success(monkeypatch, text):
    color_readback(monkeypatch, text)
    with pytest.raises(PrismError) as error:
        native_ui.native_color("an owned color well", "#FF0000", 1)
    assert error.value.code == "UI_COLOR_NOT_APPLIED"


@pytest.mark.parametrize("text", ["rgb nan nan nan nan", "rgb 1 0 0 nan"])
def test_nonfinite_native_color_cannot_satisfy_tolerance(monkeypatch, text):
    color_readback(monkeypatch, text)
    with pytest.raises(PrismError) as error:
        native_ui.native_color("an owned color well", "#FF0000", 1)
    assert error.value.code == "UI_COLOR_NOT_APPLIED"


def test_transient_color_is_not_accepted_as_the_final_requested_rgb(monkeypatch):
    # #FF0001 is the helper's temporary commit color for requested #FF0000.
    # Accepting a full 8-bit step would also accept a failed final AXConfirm.
    color_readback(monkeypatch, "rgb 1 0 0.00392156862745098 1")
    with pytest.raises(PrismError) as error:
        native_ui.native_color("an owned color well", "#FF0000", 1)
    assert error.value.code == "UI_COLOR_NOT_APPLIED"


def test_rounded_native_components_must_quantize_to_exact_requested_rgba(monkeypatch):
    color_readback(monkeypatch, "rgb 0.314 0.627 0.941 0.22")
    result = native_ui.native_color("an owned color well", "#50A0F0", 0.22)
    assert result["native_rgba_8bit"] == [80, 160, 240, 56]


@pytest.mark.parametrize("width, height", [(0, 1), (21, 1), (1, -1), (math.nan, 1), (1, math.inf)])
def test_invalid_axis_dimensions_are_rejected_before_menu_or_native_dispatch(monkeypatch, width, height):
    monkeypatch.setattr(native_ui, "menu", forbidden)
    with pytest.raises(PrismError) as error:
        native_ui.axes_frame(width, height)
    assert error.value.code == "INVALID_ARGUMENT"


def axes_dialog(monkeypatch, readbacks):
    monkeypatch.setattr(native_ui, "menu", lambda *args, **kwargs: None)
    monkeypatch.setattr(native_ui, "_dialog", lambda *args, **kwargs: None)
    monkeypatch.setattr(native_ui, "_require_dialog_absent", lambda *args, **kwargs: None)
    monkeypatch.setattr(native_ui, "native_color", lambda *args, **kwargs: {"native_value": "verified test color"})
    monkeypatch.setattr(native_ui, "close_dialog", lambda *args, **kwargs: None)
    monkeypatch.setattr(native_ui, "_committed_scale_bar_hidden", lambda visibility: {
        "native_scale_bar": False, "committed_state_reopened": True,
        "second_native_transaction_required": False})
    responses = iter(readbacks)
    calls = []

    def osa(source, **kwargs):
        calls.append(source)
        return next(responses)

    monkeypatch.setattr(native_ui, "osa", osa)
    return calls


def test_axis_units_are_checked_before_writing_size_fields(monkeypatch):
    calls = axes_dialog(monkeypatch, ["cm."])
    with pytest.raises(PrismError) as error:
        native_ui.axes_frame(2, 3)
    assert error.value.code == "UNSUPPORTED_UI_UNITS"
    assert not any("set value of text field" in source for source in calls)


@pytest.mark.parametrize("readback", [
    "9\n9\nShow both X and Y\n0\n1/2 pt", "2\n3\nHide both X and Y\n0\n1/2 pt",
    "nan\n3\nShow both X and Y\n0\n1/2 pt", "2\n3\nShow both X and Y\n0\n1 pt",
])
def test_axis_fields_must_confirm_dimensions_and_visibility(monkeypatch, readback):
    axes_dialog(monkeypatch, ["in.", readback])
    with pytest.raises(PrismError) as error:
        native_ui.axes_frame(2, 3, hide_axes=False)
    assert error.value.code == "UI_FORMAT_NOT_APPLIED"


def test_axis_readback_explicitly_accounts_for_native_two_decimal_display(monkeypatch):
    axes_dialog(monkeypatch, ["in., in.", "1.46\n2.44\nHide both X and Y\n0\n1/2 pt"])
    result = native_ui.axes_frame(37 / 25.4, 62 / 25.4, hide_axes=True)
    assert result["native_axis_width_inches"] == 1.46
    assert result["native_axis_height_inches"] == 2.44
    assert result["field_resolution_inches"] == 0.01
    assert result["native_scale_bar"] is False


@pytest.mark.parametrize("committed_values", [["0"], ["1", "0"], ["1", "1"]])
def test_transient_draft_scale_bar_requires_hidden_committed_state(monkeypatch, committed_values):
    committed_check = native_ui._committed_scale_bar_hidden
    axes_dialog(monkeypatch, ["in.", "2\n3\nHide both X and Y\n1\n1/2 pt"])
    initial_closes, committed_events = [], []
    monkeypatch.setattr(native_ui, "close_dialog", lambda title: initial_closes.append(title))

    def verify(visibility):
        assert initial_closes == ["Format Axes"]
        assert visibility == "Hide both X and Y"
        events = committed_axes_dialog(monkeypatch, [visibility + "\n" + value for value in committed_values])
        committed_events.append(events)
        return committed_check(visibility)

    monkeypatch.setattr(native_ui, "_committed_scale_bar_hidden", verify)
    if committed_values[-1] == "1":
        with pytest.raises(PrismError) as error:
            native_ui.axes_frame(2, 3, hide_axes=True)
        assert error.value.code == "UI_FORMAT_NOT_APPLIED"
    else:
        result = native_ui.axes_frame(2, 3, hide_axes=True)
        assert result["native_fields"].splitlines()[3] == "1"
        assert result["native_scale_bar"] is False
        assert result["committed_scale_bar"] == {
            "native_scale_bar": False, "committed_state_reopened": True,
            "second_native_transaction_required": len(committed_values) == 2,
        }
    assert initial_closes == ["Format Axes"] and len(committed_events) == 1
    events = committed_events[0]
    assert [event[1] for event in events if event[0] == "read"] == [
        "Hide both X and Y\n" + value for value in committed_values]
    assert events.count(("toggle_scale_bar",)) == len(committed_values) - 1
    assert events[-1] == ("close", "Format Axes", "Cancel")


def committed_axes_dialog(monkeypatch, readbacks):
    """Read committed native state independently from the checkbox action."""
    events, responses = [], iter(readbacks)
    monkeypatch.setattr(native_ui, "menu", lambda *args: events.append(("menu", *args)))
    monkeypatch.setattr(native_ui, "_dialog", lambda title: events.append(("dialog", title)))
    monkeypatch.setattr(native_ui, "close_dialog", lambda title, button="OK": events.append(("close", title, button)))

    def osa(source, **kwargs):
        if "return (value of pop up button 7" in source:
            result = next(responses)
            events.append(("read", result))
            return result
        if 'perform action "AXPress" of c' in source:
            events.append(("toggle_scale_bar",))
            return ""
        pytest.fail("Unexpected native action in committed axes verification")

    monkeypatch.setattr(native_ui, "osa", osa)
    return events


@pytest.mark.parametrize("visibility", ["Hide both X and Y", "Show both X and Y"])
def test_committed_scale_bar_already_hidden_closes_without_mutation(monkeypatch, visibility):
    events = committed_axes_dialog(monkeypatch, [visibility + "\n0"])
    result = native_ui._committed_scale_bar_hidden(visibility)
    assert result == {"native_scale_bar": False, "committed_state_reopened": True,
                      "second_native_transaction_required": False}
    assert events == [
        ("menu", "Change", "Frame and Origin..."), ("dialog", "Format Axes"),
        ("read", visibility + "\n0"), ("close", "Format Axes", "Cancel"),
    ]


def test_scale_bar_reenabled_during_commit_is_repaired_then_verified_in_a_new_dialog(monkeypatch):
    visibility = "Hide both X and Y"
    events = committed_axes_dialog(monkeypatch, [visibility + "\n1", visibility + "\n0"])
    result = native_ui._committed_scale_bar_hidden(visibility)
    assert result["native_scale_bar"] is False
    assert result["committed_state_reopened"] is True
    assert result["second_native_transaction_required"] is True
    assert events == [
        ("menu", "Change", "Frame and Origin..."), ("dialog", "Format Axes"),
        ("read", visibility + "\n1"), ("toggle_scale_bar",), ("close", "Format Axes", "OK"),
        ("menu", "Change", "Frame and Origin..."), ("dialog", "Format Axes"),
        ("read", visibility + "\n0"), ("close", "Format Axes", "Cancel"),
    ]


def test_scale_bar_still_enabled_after_second_transaction_cannot_report_success(monkeypatch):
    visibility = "Hide both X and Y"
    events = committed_axes_dialog(monkeypatch, [visibility + "\n1", visibility + "\n1"])
    with pytest.raises(PrismError) as error:
        native_ui._committed_scale_bar_hidden(visibility)
    assert error.value.code == "UI_FORMAT_NOT_APPLIED"
    assert events.count(("toggle_scale_bar",)) == 1
    assert events.count(("close", "Format Axes", "OK")) == 1
    assert events.count(("menu", "Change", "Frame and Origin...")) == 2
    assert events[-1] == ("close", "Format Axes", "Cancel")


@pytest.mark.parametrize("readback", ["Show both X and Y\n0", "", "Hide both X and Y\n2", "Hide both X and Y\n0\nextra"])
def test_changed_or_malformed_committed_axis_state_is_not_mutated(monkeypatch, readback):
    events = committed_axes_dialog(monkeypatch, [readback])
    with pytest.raises(PrismError) as error:
        native_ui._committed_scale_bar_hidden("Hide both X and Y")
    assert error.value.code == "UI_FORMAT_NOT_APPLIED"
    assert error.value.details["actual"] == readback.splitlines()
    assert ("toggle_scale_bar",) not in events
    assert ("close", "Format Axes", "OK") not in events


def test_visibility_changed_after_scale_bar_repair_still_fails_without_another_mutation(monkeypatch):
    events = committed_axes_dialog(monkeypatch, ["Hide both X and Y\n1", "Show both X and Y\n0"])
    with pytest.raises(PrismError) as error:
        native_ui._committed_scale_bar_hidden("Hide both X and Y")
    assert error.value.code == "UI_FORMAT_NOT_APPLIED"
    assert events.count(("toggle_scale_bar",)) == 1
    assert events.count(("close", "Format Axes", "OK")) == 1


def test_axis_frame_requires_reopened_scale_bar_verification_after_initial_dialog_commit(monkeypatch):
    axes_dialog(monkeypatch, ["in.", "2\n3\nHide both X and Y\n0\n1/2 pt"])
    events = []
    monkeypatch.setattr(native_ui, "close_dialog", lambda title: events.append(("close", title)))

    def verify(visibility):
        assert events == [("close", "Format Axes")]
        assert visibility == "Hide both X and Y"
        raise PrismError("UI_FORMAT_NOT_APPLIED", "Scale bar remained enabled after commit")

    monkeypatch.setattr(native_ui, "_committed_scale_bar_hidden", verify)
    with pytest.raises(PrismError) as error:
        native_ui.axes_frame(2, 3, hide_axes=True)
    assert error.value.code == "UI_FORMAT_NOT_APPLIED"


def test_existing_axes_dialog_blocks_menu_dispatch(monkeypatch):
    monkeypatch.setattr(native_ui, "osa", lambda *args, **kwargs: "1")
    monkeypatch.setattr(native_ui, "menu", forbidden)
    with pytest.raises(PrismError) as error:
        native_ui.axes_frame(2, 3)
    assert error.value.code == "UNEXPECTED_DIALOG"


@pytest.mark.parametrize("units", ["min.", "in., cm.", "millimeters"])
def test_unverified_or_conflicting_units_do_not_allow_axis_field_writes(monkeypatch, units):
    calls = axes_dialog(monkeypatch, [units])
    with pytest.raises(PrismError) as error:
        native_ui.axes_frame(2, 3)
    assert error.value.code == "UNSUPPORTED_UI_UNITS"
    assert not any("set value of text field" in source for source in calls)


@pytest.mark.parametrize("counts", [[0], [2]])
def test_missing_or_ambiguous_dialog_is_rejected_without_placement(monkeypatch, counts):
    calls = []
    monkeypatch.setattr(native_ui, "osa", lambda *args, **kwargs: str(counts[0]))
    monkeypatch.setattr(native_ui, "_window_state", lambda: {"title": "Other native window", "url": None})
    monkeypatch.setattr(placement, "enforce", lambda: calls.append("placement"))
    with pytest.raises(PrismError) as error:
        native_ui._dialog("Format Graph")
    assert error.value.code == "UNEXPECTED_DIALOG"
    assert error.value.details["matches"] == counts[0]
    assert not calls


def test_dialog_waits_for_one_match_and_propagates_placement_failure(monkeypatch):
    values = iter(["0", "0", "1"])
    monkeypatch.setattr(native_ui, "osa", lambda *args, **kwargs: next(values))

    def unavailable():
        raise PrismError("DISPLAY_UNAVAILABLE", "Recorded display is absent")

    monkeypatch.setattr(placement, "enforce", unavailable)
    with pytest.raises(PrismError) as error:
        native_ui._dialog("Format Graph")
    assert error.value.code == "DISPLAY_UNAVAILABLE"


def test_dialog_disappearing_during_placement_is_not_reported_as_ready(monkeypatch):
    counts = iter(["1", "0"])
    monkeypatch.setattr(native_ui, "osa", lambda *args, **kwargs: next(counts))
    with pytest.raises(PrismError) as error:
        native_ui._dialog("Format Graph")
    assert error.value.code == "UNEXPECTED_DIALOG"


@pytest.mark.parametrize("count", ["1", "2"])
def test_preexisting_format_dialog_blocks_a_new_format_action(monkeypatch, count):
    calls = []

    def count_only(*args, **kwargs):
        calls.append(args)
        return count

    monkeypatch.setattr(native_ui, "osa", count_only)
    with pytest.raises(PrismError) as error:
        native_ui.classic_format_menu()
    assert error.value.code == "UNEXPECTED_DIALOG"
    assert len(calls) == 1


def test_format_dispatch_requires_its_own_completion_token(monkeypatch):
    monkeypatch.setattr(native_ui, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="current-dispatch")))
    responses = iter(["0", "previous-dispatch"])
    monkeypatch.setattr(native_ui, "osa", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(native_ui, "_dialog", forbidden)
    with pytest.raises(PrismError) as error:
        native_ui.classic_format_menu()
    assert error.value.code == "UI_ACTION_NOT_DISPATCHED"


def test_format_dispatch_waits_for_a_new_unique_dialog(monkeypatch):
    monkeypatch.setattr(native_ui, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="current-dispatch")))
    responses = iter(["0", "current-dispatch", "0", "1", "1"])
    monkeypatch.setattr(native_ui, "osa", lambda *args, **kwargs: next(responses))
    native_ui.classic_format_menu()
    with pytest.raises(StopIteration):
        next(responses)


def test_menu_dispatch_does_not_accept_an_empty_native_return(monkeypatch):
    monkeypatch.setattr(native_ui, "osa", lambda *args, **kwargs: "")
    with pytest.raises(PrismError) as error:
        native_ui.menu("Change", "Frame and Origin...")
    assert error.value.code == "UI_ACTION_NOT_DISPATCHED"


@pytest.mark.parametrize("count", ["0", "2"])
def test_dialog_close_requires_exactly_one_target(monkeypatch, count):
    responses = iter([count])
    monkeypatch.setattr(native_ui, "osa", lambda *args, **kwargs: next(responses))
    with pytest.raises(PrismError) as error:
        native_ui.close_dialog("Format Graph")
    assert error.value.code == "UNEXPECTED_DIALOG"


def test_dialog_close_requires_terminal_closed_evidence(monkeypatch):
    responses = iter(["1", "still-open"])
    monkeypatch.setattr(native_ui, "osa", lambda *args, **kwargs: next(responses))
    with pytest.raises(PrismError) as error:
        native_ui.close_dialog("Format Graph")
    assert error.value.code == "UI_DIALOG_NOT_CLOSED"


def test_unverified_source_column_is_rejected_before_native_dispatch():
    with pytest.raises(PrismError) as error:
        native_ui.classic_dataset("C", "Group A")
    assert error.value.code == "INVALID_ARGUMENT"


def test_dataset_readback_cannot_report_a_different_source_column(monkeypatch):
    monkeypatch.setattr(native_ui, "osa", lambda *args, **kwargs: "1:Bound table:B:Group A")
    with pytest.raises(PrismError) as error:
        native_ui.classic_dataset("A", "Group A")
    assert error.value.code == "UI_FORMAT_NOT_APPLIED"


class FakeQuartz:
    kCGWindowListOptionOnScreenOnly = 1
    kCGNullWindowID = 0
    kCGEventLeftMouseDown = 1
    kCGEventLeftMouseUp = 2
    kCGMouseButtonLeft = 0
    kCGMouseEventClickState = 1
    kCGHIDEventTap = 0
    kCGNormalWindowLevelKey = "normal"
    kCGFloatingWindowLevelKey = "floating"
    kCGModalPanelWindowLevelKey = "modal"
    kCGUtilityWindowLevelKey = "utility"

    @staticmethod
    def CGWindowLevelForKey(key):
        return {"normal": 0, "floating": 3, "modal": 8, "utility": 19}[key]

    def __init__(self, rows):
        self.rows = rows
        self.events = []

    def CGWindowListCopyWindowInfo(self, *args):
        return copy.deepcopy(self.rows)

    def CGEventCreateMouseEvent(self, source, kind, point, button):
        return {"kind": kind, "point": point, "button": button}

    def CGEventSetFlags(self, event, flags):
        event["flags"] = flags

    def CGEventSetIntegerValueField(self, event, field, value):
        event[field] = value

    def CGEventPost(self, tap, event):
        self.events.append(dict(event))


def palette(monkeypatch, rows, active=True):
    quartz = FakeQuartz(rows)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    values = iter(["", "42", "true"])
    monkeypatch.setattr(native_ui, "osa", lambda *args, **kwargs: next(values))
    monkeypatch.setattr(native_ui, "_active", lambda: active)
    return quartz


def palette_row(**overrides):
    return {"kCGWindowOwnerPID": 42, "kCGWindowNumber": 101, "kCGWindowName": "Data Point Symbols",
            "kCGWindowBounds": {"X": -2000, "Y": 400, "Width": 233, "Height": 205}, **overrides}


@pytest.mark.parametrize("case", ["absent", "foreign", "duplicate", "resized", "focus_lost"])
def test_unverified_symbol_palette_cannot_receive_global_mouse_events(monkeypatch, case):
    rows = [palette_row()]
    if case == "absent":
        rows = []
    elif case == "foreign":
        rows = [palette_row(kCGWindowOwnerPID=99)]
    elif case == "duplicate":
        rows.append(palette_row(kCGWindowNumber=102))
    elif case == "resized":
        rows[0]["kCGWindowBounds"]["Width"] = 234
    quartz = palette(monkeypatch, rows, active=case != "focus_lost")
    with pytest.raises(PrismError) as error:
        native_ui.classic_bordered_circle()
    assert error.value.code == "UNSUPPORTED_SYMBOL_PALETTE"
    assert not quartz.events


def test_unavailable_windowserver_result_is_a_structured_palette_failure(monkeypatch):
    quartz = palette(monkeypatch, None)
    with pytest.raises(PrismError) as error:
        native_ui.classic_bordered_circle()
    assert error.value.code == "UNSUPPORTED_SYMBOL_PALETTE"
    assert not quartz.events


def test_nonfinite_palette_position_is_rejected_before_mouse_events(monkeypatch):
    row = palette_row()
    row["kCGWindowBounds"]["X"] = math.nan
    quartz = palette(monkeypatch, [row])
    with pytest.raises(PrismError) as error:
        native_ui.classic_bordered_circle()
    assert error.value.code == "UNSUPPORTED_SYMBOL_PALETTE"
    assert not quartz.events


def test_palette_movement_during_focus_check_cannot_produce_a_stale_click(monkeypatch):
    quartz = palette(monkeypatch, [palette_row()])

    def moved_while_checking_focus():
        quartz.rows[0]["kCGWindowBounds"]["X"] -= 300
        return True

    monkeypatch.setattr(native_ui, "_active", moved_while_checking_focus)
    with pytest.raises(PrismError) as error:
        native_ui.classic_bordered_circle()
    assert error.value.code in {"UNSUPPORTED_SYMBOL_PALETTE", "UI_TARGET_MISMATCH"}
    assert not quartz.events


def test_existing_border_state_cannot_hide_a_palette_that_failed_to_close(monkeypatch):
    quartz = palette(monkeypatch, [palette_row()])
    with pytest.raises(PrismError) as error:
        native_ui.classic_bordered_circle()
    assert error.value.code == "UI_FORMAT_NOT_APPLIED"
    assert len(quartz.events) == 2


def test_owned_palette_selection_requires_closure_and_border_readback(monkeypatch):
    quartz = palette(monkeypatch, [palette_row()])
    post = quartz.CGEventPost

    def choose(tap, event):
        post(tap, event)
        if event["kind"] == quartz.kCGEventLeftMouseUp:
            quartz.rows = []

    monkeypatch.setattr(quartz, "CGEventPost", choose)
    result = native_ui.classic_bordered_circle()
    assert result["symbol"] == "native_circle_with_independent_fill_and_border"
    assert len(quartz.events) == 2
    assert all(event["flags"] == 0 for event in quartz.events)


def owned_windows(monkeypatch, rows):
    quartz = FakeQuartz(rows)
    application = SimpleNamespace(processIdentifier=lambda: 42)
    applications = SimpleNamespace(runningApplicationsWithBundleIdentifier_=lambda bundle: [application])
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(NSRunningApplication=applications))
    return quartz


def window_row(identifier, x, layer=0, owner=42):
    return {"kCGWindowOwnerPID": owner, "kCGWindowNumber": identifier, "kCGWindowLayer": layer,
            "kCGWindowBounds": {"X": x, "Y": 500, "Width": 500, "Height": 400}}


@pytest.mark.parametrize("layer", [3, 8, 19])
def test_passive_placement_includes_owned_floating_modal_and_utility_panels(monkeypatch, layer):
    # CGWindowLevel.h separates floating/modal/utility panels from normal level 0.
    owned_windows(monkeypatch, [window_row(1, -2000), window_row(2, 100, layer), window_row(3, 100, layer, 99)])
    result = placement._visible_prism_frames()
    assert {row["window_id"] for row in result} == {1, 2}


def test_off_display_owned_panel_prevents_successful_placement_fast_path(monkeypatch, tmp_path):
    owned_windows(monkeypatch, [window_row(1, -2000), window_row(2, 100, 3)])
    screen = {"uuid": "00000000-0000-4000-8000-000000000001", "id": 5,
              "x": -3000, "y": 0, "width": 3000, "height": 2000, "is_main": False}
    path = tmp_path / "placement.json"
    path.write_text(json.dumps({"display": screen, "document_frame": {"x": -2000, "y": 500, "width": 500, "height": 400}}))
    monkeypatch.setattr(placement, "POLICY", path)
    monkeypatch.setattr(placement, "displays", lambda: [screen])
    monkeypatch.setattr(placement, "osa", lambda *args, **kwargs: json.dumps({"configured": True, "moved": []}))
    with pytest.raises(PrismError) as error:
        placement.enforce()
    assert error.value.code == "PLACEMENT_TRANSIENT"


def test_windowserver_unavailable_is_not_confused_with_no_visible_windows(monkeypatch):
    owned_windows(monkeypatch, None)
    with pytest.raises(PrismError) as error:
        placement._visible_prism_frames()
    assert error.value.code == "PLACEMENT_UNAVAILABLE"
