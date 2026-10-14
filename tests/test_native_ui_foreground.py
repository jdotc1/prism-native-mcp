"""Window-ownership contracts with every native control surface isolated."""

from __future__ import annotations

import copy
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
    for module in (native_ui, placement, runtime):
        monkeypatch.setattr(module, "osa", forbidden)
    monkeypatch.setattr(runtime.subprocess, "run", forbidden)
    monkeypatch.setattr(runtime.subprocess, "Popen", forbidden)
    monkeypatch.setattr(native_ui, "time", Clock())
    monkeypatch.setattr(native_ui, "_active", forbidden)
    monkeypatch.setattr(native_ui, "_window_state", forbidden)
    monkeypatch.setattr(placement, "enforce", lambda: None)
    monkeypatch.setattr(placement, "POLICY", tmp_path / "unused-placement.json")
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace())


def observed_window(monkeypatch, state):
    """Focus requests succeed; the independent window readback is authoritative."""
    snapshots = []
    states = state if isinstance(state, list) else [state]

    def osa(script, **kwargs):
        if "get bundle identifier" in script:
            return "com.GraphPad.Prism"
        if "get unix id" in script:
            return "4321"
        return ""

    def read_window():
        result = copy.deepcopy(states[min(len(snapshots), len(states) - 1)])
        snapshots.append(result)
        return result

    monkeypatch.setattr(native_ui, "osa", osa)
    monkeypatch.setattr(native_ui, "_active", lambda: True)
    monkeypatch.setattr(native_ui, "_window_state", read_window)
    return snapshots


@pytest.mark.parametrize("main", [False, None, "true", 1])
def test_exact_front_window_without_verified_axmain_never_allows_edits(tmp_path, monkeypatch, main):
    target = tmp_path / "owned.prism"
    state = {"title": target.name, "url": target.as_uri(), "main": main}
    snapshots = observed_window(monkeypatch, state)
    entered = False
    with pytest.raises(PrismError) as error:
        with native_ui.foreground(target):
            entered = True
    assert error.value.code == "UI_TARGET_MISMATCH"
    assert not entered
    assert snapshots
    assert error.value.details["window"] == state


def test_missing_axmain_readback_never_allows_edits(tmp_path, monkeypatch):
    target = tmp_path / "owned.prism"
    observed_window(monkeypatch, {"title": target.name, "url": target.as_uri()})
    with pytest.raises(PrismError) as error:
        with native_ui.foreground(target):
            pytest.fail("An absent AXMain value must not grant window ownership")
    assert error.value.code == "UI_TARGET_MISMATCH"


@pytest.mark.parametrize("document", ["same_name_elsewhere", "another_document", "missing", "empty"])
def test_axmain_on_the_wrong_document_never_allows_edits(tmp_path, monkeypatch, document):
    target = tmp_path / "owned.prism"
    urls = {
        "same_name_elsewhere": (tmp_path / "other" / target.name).as_uri(),
        "another_document": (tmp_path / "unrelated.prism").as_uri(),
        "missing": None,
        "empty": "",
    }
    # A matching displayed title is insufficient even if the window is main.
    state = {"title": target.name, "url": urls[document], "main": True}
    observed_window(monkeypatch, state)
    with pytest.raises(PrismError) as error:
        with native_ui.foreground(target):
            pytest.fail("Only the exact checkpoint document may receive edits")
    assert error.value.code == "UI_TARGET_MISMATCH"
    assert error.value.details["window"] == state


def test_exact_document_with_axmain_true_allows_edits_after_readback(tmp_path, monkeypatch):
    target = tmp_path / "checkpoint #2 β.prism"
    state = {"title": target.name, "url": target.as_uri(), "main": True}
    snapshots = observed_window(monkeypatch, state)
    with native_ui.foreground(target) as app:
        assert snapshots
        assert snapshots[-1] == state
        assert app == 4321


def test_transient_native_panel_must_yield_to_exact_main_document_before_edits(tmp_path, monkeypatch):
    target = tmp_path / "owned.prism"
    panel = {"title": "", "url": None, "main": False}
    raised = {"title": target.name, "url": target.as_uri(), "main": False}
    ready = {"title": target.name, "url": target.as_uri(), "main": True}
    snapshots = observed_window(monkeypatch, [panel, raised, ready])
    with native_ui.foreground(target) as app:
        assert snapshots[-1] == ready
        assert panel in snapshots
        assert raised in snapshots
        assert native_ui.time.elapsed > 0
        assert app == 4321


def test_exact_main_document_in_inactive_prism_never_allows_edits(tmp_path, monkeypatch):
    target = tmp_path / "owned.prism"
    observed_window(monkeypatch, {"title": target.name, "url": target.as_uri(), "main": True})
    monkeypatch.setattr(native_ui, "_active", lambda: False)
    with pytest.raises(PrismError) as error:
        with native_ui.foreground(target):
            pytest.fail("A main document in an inactive Prism process is not ready for editing")
    assert error.value.code == "UI_TARGET_MISMATCH"
