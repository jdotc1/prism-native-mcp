"""Exact saved-window close contracts without native application access."""

from __future__ import annotations

import contextlib
import copy
import sys
from types import SimpleNamespace

import pytest

from prism_mcp import native_ui, placement, runtime
from prism_mcp.errors import PrismError


def forbidden(*args, **kwargs):
    raise AssertionError('This test must never invoke native application control')


class Clock:
    def __init__(self): self.elapsed = 0.0
    def monotonic(self): return self.elapsed
    def sleep(self, seconds): self.elapsed += seconds


@pytest.fixture(autouse=True)
def isolated_native_surfaces(monkeypatch, tmp_path):
    for module in (native_ui, placement, runtime):
        monkeypatch.setattr(module, 'osa', forbidden)
    for name in ('foreground', 'menu', '_active'):
        monkeypatch.setattr(native_ui, name, forbidden)
    monkeypatch.setattr(runtime, 'windows', forbidden)
    monkeypatch.setattr(runtime, 'native_script_windows', forbidden)
    monkeypatch.setattr(runtime.subprocess, 'run', forbidden)
    monkeypatch.setattr(runtime.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(native_ui, 'time', Clock())
    monkeypatch.setattr(placement, 'enforce', forbidden)
    monkeypatch.setattr(placement, 'POLICY', tmp_path / 'unused-placement.json')
    monkeypatch.setitem(sys.modules, 'AppKit', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'Quartz', SimpleNamespace())


def clean_window(path, **changes):
    return {'title': path.name, 'path': str(path.resolve()), 'edited': False, 'modal': False, **changes}


def observed_windows(monkeypatch, snapshots):
    calls, reads = [], []

    def windows():
        result = copy.deepcopy(snapshots[min(len(reads), len(snapshots) - 1)])
        reads.append(result)
        return result

    @contextlib.contextmanager
    def foreground(path):
        calls.append(('foreground', path))
        yield 4321

    monkeypatch.setattr(runtime, 'windows', windows)
    monkeypatch.setattr(native_ui, 'foreground', foreground)
    monkeypatch.setattr(native_ui, 'menu', lambda *items: calls.append(('menu', items)))
    return calls, reads


@pytest.mark.parametrize('fault, code', [
    ('missing', 'UI_TARGET_MISMATCH'), ('same_name_elsewhere', 'UI_TARGET_MISMATCH'),
    ('duplicate', 'UI_TARGET_MISMATCH'), ('edited', 'UNSAVED_CHANGES'),
    ('unknown_saved_state', 'UNSAVED_CHANGES'), ('modal', 'PRISM_MODAL'),
])
def test_unsafe_saved_window_never_receives_file_close(tmp_path, monkeypatch, fault, code):
    target = tmp_path / 'working.prism'
    rows = [clean_window(target)]
    if fault == 'missing': rows = []
    elif fault == 'same_name_elsewhere': rows = [clean_window(tmp_path / 'other' / target.name)]
    elif fault == 'duplicate': rows *= 2
    elif fault == 'edited': rows[0]['edited'] = True
    elif fault == 'unknown_saved_state': rows[0]['edited'] = None
    else: rows.append({'title': 'A native dialog', 'path': None, 'modal': True})
    calls, _ = observed_windows(monkeypatch, [rows])
    with pytest.raises(PrismError) as error:
        native_ui.close_saved_document(target)
    assert error.value.code == code
    assert not calls


@pytest.mark.parametrize('fault, code', [
    ('target_changed', 'UI_TARGET_MISMATCH'), ('edited', 'UNSAVED_CHANGES'), ('modal', 'PRISM_MODAL'),
])
def test_window_state_is_revalidated_after_foreground_before_file_close(tmp_path, monkeypatch, fault, code):
    target = tmp_path / 'working.prism'
    first = [clean_window(target)]
    changed = [clean_window(target)]
    if fault == 'target_changed': changed[0]['path'] = str(tmp_path / 'unrelated.prism')
    elif fault == 'edited': changed[0]['edited'] = True
    else: changed.append({'title': 'A native dialog', 'path': None, 'modal': True})
    calls, _ = observed_windows(monkeypatch, [first, changed])
    with pytest.raises(PrismError) as error:
        native_ui.close_saved_document(target)
    assert error.value.code == code
    assert calls == [('foreground', target)]


def test_success_waits_for_exact_document_to_disappear_and_preserves_other_window(tmp_path, monkeypatch):
    target = tmp_path / 'checkpoint #2 β.prism'
    other = clean_window(tmp_path / 'other' / target.name, edited=True)
    before = [clean_window(target), other]
    calls, reads = observed_windows(monkeypatch, [before, before, before, [other]])
    result = native_ui.close_saved_document(target)
    assert calls == [('foreground', target), ('menu', ('File', 'Close'))]
    assert reads[-1] == [other]
    assert native_ui.time.elapsed > 0
    assert result['path'] == str(target.resolve())
    assert result['saved_state_before_close'] and result['window_closed']


def test_still_open_saved_window_cannot_report_success(tmp_path, monkeypatch):
    target = tmp_path / 'working.prism'
    calls, _ = observed_windows(monkeypatch, [[clean_window(target)]])
    with pytest.raises(PrismError) as error:
        native_ui.close_saved_document(target)
    assert error.value.code == 'UI_CLOSE_UNVERIFIED'
    assert calls == [('foreground', target), ('menu', ('File', 'Close'))]
