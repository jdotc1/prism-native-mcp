"""Supplemental runtime safety checks; all Apple Events/process dispatch is fake."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from prism_mcp import placement, runtime
from prism_mcp.errors import PrismError


@pytest.fixture(autouse=True)
def isolated_control(monkeypatch, tmp_path):
    native_window_detector = runtime.native_script_windows
    monkeypatch.setattr(runtime, "CONTROL_ROOT", tmp_path / "global-control")
    monkeypatch.setattr(runtime, "application_info", lambda: {
        "installed": True, "version_compatible": True, "path": "/test-only/Prism 11.app",
        "version": "11.1.0", "build": "110218",
    })
    monkeypatch.setattr(runtime, "verify_running_application", lambda: "")
    monkeypatch.setattr(runtime, "native_script_windows", lambda: [])

    def prohibited(*args, **kwargs):
        pytest.fail("A unit test attempted real application control")

    monkeypatch.setattr(runtime, "osa", prohibited)
    monkeypatch.setattr(runtime.subprocess, "Popen", prohibited)
    monkeypatch.setattr(placement, "POLICY", tmp_path / "placement-policy.json")
    monkeypatch.setattr(placement, "osa", prohibited)
    monkeypatch.setattr(placement, "enforce", lambda: {"configured": False, "moved": []})
    return native_window_detector


def test_prism_percent_escaping_is_distinct_from_applescript_quoting():
    value = '/tmp/100% cohort/back\\slash %Y.prism'
    assert runtime.quote(value) == '"/tmp/100%% cohort/back\\slash %%Y.prism"'
    assert runtime.apple_quote(value) == '"/tmp/100% cohort/back\\\\slash %Y.prism"'
    assert runtime.apple_quote('a"b') == '"a\\"b"'


@pytest.mark.parametrize("value", ['a"b', 'a\nb', 'a\rb', 'a\x00b'])
def test_prism_script_arguments_reject_command_injection(value):
    with pytest.raises(PrismError) as error:
        runtime.quote(value)
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("encoding", ["utf-32", "utf-32-le", "utf-16", "utf-8"])
def test_native_log_decoding_preserves_success_and_error_text(encoding):
    text = "Prism\r\nμs\r\nCOMPLETE! No Errors.\r\n"
    assert runtime.decode_log(text.encode(encoding)) == text


def test_utf32_big_endian_bom_and_incomplete_little_endian_tail():
    text = "COMPLETE! No Errors."
    assert runtime.decode_log(b"\x00\x00\xfe\xff" + text.encode("utf-32-be")) == text
    assert runtime.decode_log(text.encode("utf-32-le") + b"\x00\x00") == text
    assert runtime.decode_log(b"") == ""


def test_global_lock_conflicts_across_different_state_directories(tmp_path):
    first = runtime.Runtime(tmp_path / "state-one")
    second = runtime.Runtime(tmp_path / "state-two")
    assert first.state != second.state
    with first.locked():
        with pytest.raises(PrismError) as error:
            with second.locked():
                pytest.fail("Concurrent Prism control entered")
        assert error.value.code == "PRISM_BUSY"
    with second.locked():
        pass  # The real OS lock is released after the first context exits.


def test_existing_inflight_checkpoint_quarantines_all_state_directories(tmp_path):
    first = runtime.Runtime(tmp_path / "state-one")
    second = runtime.Runtime(tmp_path / "state-two")
    checkpoint = {"run_id": "a" * 32, "token": "uncertain-native-operation"}
    runtime.write_json(runtime.CONTROL_ROOT / "inflight.json", checkpoint)
    for instance in (first, second):
        with pytest.raises(PrismError) as error:
            with instance.locked():
                pytest.fail("Uncertain native operation did not block dispatch")
        assert error.value.code == "RECOVERY_REQUIRED"
        assert error.value.details["checkpoint"] == checkpoint
    with second.locked(recovery=True):
        assert (runtime.CONTROL_ROOT / "inflight.json").exists()


def test_unreadable_inflight_checkpoint_still_returns_structured_recovery_error(tmp_path):
    instance = runtime.Runtime(tmp_path / "state")
    (runtime.CONTROL_ROOT / "inflight.json").write_text('{"run_id":')
    with pytest.raises(PrismError) as error:
        with instance.locked():
            pytest.fail("Corrupt uncertainty checkpoint allowed native mutation")
    assert error.value.code in {"RECOVERY_REQUIRED", "CORRUPT_CHECKPOINT"}
    assert (runtime.CONTROL_ROOT / "inflight.json").exists()


@pytest.mark.parametrize("run_id", ["../other", "a" * 31, "A" * 32, "g" * 32, ""])
def test_operation_status_rejects_noncanonical_ids(tmp_path, run_id):
    with pytest.raises(PrismError) as error:
        runtime.Runtime(tmp_path / "state").status(run_id)
    assert error.value.code == "INVALID_RUN_ID"


class FakeProcess:
    def __init__(self, returncode=0):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO("simulated Apple Events failure")
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("test-only-osascript", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15


def fake_dispatch(monkeypatch, operation, *, completion="current", native_log="COMPLETE! No Errors.", returncode=0, produce=None):
    process = FakeProcess(returncode)

    def dispatch(*args, **kwargs):
        step = operation.record["steps"][-1]
        directory = Path(step["script"]).parent
        if native_log is not None:
            Path(step["log"]).write_bytes(native_log.encode("utf-32-le"))
        if completion is not None:
            token = step["completion_token"] if completion == "current" else "previous-operation-token"
            (directory / "completed.txt").write_bytes(token.encode("utf-32-le"))
        if produce is not None:
            produce()
        return process

    monkeypatch.setattr(runtime.subprocess, "Popen", dispatch)
    return process


@pytest.mark.parametrize("completion,native_log", [("stale", "COMPLETE! No Errors."), ("current", "script began only"), (None, "COMPLETE! No Errors.")])
def test_success_requires_current_token_and_native_completion_tail(tmp_path, monkeypatch, completion, native_log):
    instance = runtime.Runtime(tmp_path / "state")
    operation = instance.operation("test", {"path": str(tmp_path / "target.prism")})
    fake_dispatch(monkeypatch, operation, completion=completion, native_log=native_log)
    monkeypatch.setattr(runtime.time, "sleep", lambda seconds: None)
    with pytest.raises(PrismError) as error:
        operation.script(["RecalcAll"], timeout=0.01)
    assert error.value.code == "PRISM_TIMEOUT"
    assert operation.record["steps"][-1]["status"] == "failed"
    assert (runtime.CONTROL_ROOT / "inflight.json").exists()


def test_existing_export_cannot_count_as_fresh_output(tmp_path, monkeypatch):
    instance = runtime.Runtime(tmp_path / "state")
    operation = instance.operation("test", {"path": str(tmp_path / "target.prism")})
    output = tmp_path / "old.pdf"
    output.write_bytes(b"%PDF-1.7 old fixture")
    prior = output.read_bytes()
    fake_dispatch(monkeypatch, operation)
    with pytest.raises(PrismError) as error:
        operation.script(["RecalcAll"], expected=[output], timeout=0.1)
    assert error.value.code == "STALE_OUTPUT"
    assert output.read_bytes() == prior
    assert not (runtime.CONTROL_ROOT / "inflight.json").exists()


def test_missing_output_cannot_be_reported_as_completed(tmp_path, monkeypatch):
    instance = runtime.Runtime(tmp_path / "state")
    operation = instance.operation("test", {})
    fake_dispatch(monkeypatch, operation)
    with pytest.raises(PrismError) as error:
        operation.script(["RecalcAll"], expected=[tmp_path / "missing.svg"], timeout=0.1)
    assert error.value.code == "MISSING_OUTPUT"


def test_current_completion_records_output_hash_and_clears_quarantine(tmp_path, monkeypatch):
    instance = runtime.Runtime(tmp_path / "state")
    operation = instance.operation("test", {})
    output = tmp_path / "fresh.svg"
    fake_dispatch(monkeypatch, operation, produce=lambda: output.write_text("<svg>native-operation-placeholder</svg>"))
    step = operation.script(["RecalcAll"], expected=[output], timeout=0.1)
    assert step["status"] == "succeeded"
    assert step["output_sha256"][str(output)] == runtime.digest(output)
    assert not (runtime.CONTROL_ROOT / "inflight.json").exists()
    assert Path(step["script"]).exists()
    assert (Path(step["script"]).parent / "run.log.txt").read_text() == "COMPLETE! No Errors."


def test_failed_dispatch_keeps_durable_checkpoint_and_blocks_next_mutation(tmp_path, monkeypatch):
    instance = runtime.Runtime(tmp_path / "state")
    operation = instance.operation("test", {})
    fake_dispatch(monkeypatch, operation, completion=None, native_log=None, returncode=1)
    with pytest.raises(PrismError) as error:
        operation.script(["RecalcAll"], timeout=0.1)
    assert error.value.code == "CONTROL_UNAVAILABLE"
    checkpoint = json.loads((runtime.CONTROL_ROOT / "inflight.json").read_text())
    assert checkpoint["run_id"] == operation.run_id
    assert instance.status(operation.run_id)["steps"][0]["status"] == "failed"
    with pytest.raises(PrismError) as next_error:
        with runtime.Runtime(tmp_path / "other-state").locked():
            pytest.fail("Failure quarantine was bypassed")
    assert next_error.value.code == "RECOVERY_REQUIRED"


def test_native_timeout_terminates_only_helper_and_keeps_quarantine(tmp_path, monkeypatch):
    instance = runtime.Runtime(tmp_path / "state")
    operation = instance.operation("test", {})
    process = fake_dispatch(monkeypatch, operation, completion=None, native_log=None, returncode=None)
    with pytest.raises(PrismError) as error:
        operation.script(["RecalcAll"], timeout=0)
    assert error.value.code == "PRISM_TIMEOUT"
    assert process.terminated
    assert (runtime.CONTROL_ROOT / "inflight.json").exists()


def test_launcher_crash_keeps_inflight_checkpoint_for_next_runtime(tmp_path, monkeypatch):
    instance = runtime.Runtime(tmp_path / "state")
    operation = instance.operation("test", {})

    def launcher_crash(*args, **kwargs):
        raise OSError("simulated launcher crash")

    monkeypatch.setattr(runtime.subprocess, "Popen", launcher_crash)
    with pytest.raises((PrismError, OSError)):
        operation.script(["RecalcAll"], timeout=0.1)
    checkpoint = json.loads((runtime.CONTROL_ROOT / "inflight.json").read_text())
    assert checkpoint["run_id"] == operation.run_id
    with pytest.raises(PrismError) as error:
        with runtime.Runtime(tmp_path / "after-crash-state").locked():
            pytest.fail("A crashed launch lost the durable uncertainty gate")
    assert error.value.code == "RECOVERY_REQUIRED"


def test_cancellation_before_dispatch_creates_no_native_step(tmp_path):
    instance = runtime.Runtime(tmp_path / "state")
    operation = instance.operation("test", {})
    (operation.directory / "cancel.request").touch()
    with pytest.raises(PrismError) as error:
        operation.script(["RecalcAll"])
    assert error.value.code == "CANCELLED"
    assert operation.record["steps"] == []
    assert not (runtime.CONTROL_ROOT / "inflight.json").exists()


def simulated_clock(monkeypatch):
    clock = {'now': 100.0}
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(runtime.time, 'sleep', lambda seconds: clock.update(now=clock['now'] + seconds))
    return clock


def test_success_rejects_token_substrings(tmp_path, monkeypatch):
    operation = runtime.Runtime(tmp_path / 'state').operation('test', {})

    def corrupt_completion():
        step = operation.record['steps'][-1]
        (Path(step['script']).parent / 'completed.txt').write_text(step['completion_token'] + ':unrelated')

    fake_dispatch(monkeypatch, operation, produce=corrupt_completion)
    simulated_clock(monkeypatch)
    with pytest.raises(PrismError) as error:
        operation.script(['RecalcAll'], timeout=.5)
    assert error.value.code == 'PRISM_TIMEOUT'
    assert not operation.record['steps'][-1].get('native_completion_verified')
    assert (runtime.CONTROL_ROOT / 'inflight.json').exists()


def test_completion_precedes_transient_final_placement_and_requires_recheck(tmp_path, monkeypatch):
    operation = runtime.Runtime(tmp_path / 'state').operation('test', {})
    fake_dispatch(monkeypatch, operation)
    simulated_clock(monkeypatch)
    phases = []

    def changing_windows():
        step = operation.record['steps'][-1]
        phases.append(bool(step.get('native_completion_verified')))
        if len(phases) in (2, 3):
            raise PrismError('PLACEMENT_TRANSIENT', 'simulated disappearing window')
        return {'configured': True, 'moved': []}

    monkeypatch.setattr(placement, 'enforce', changing_windows)
    step = operation.script(['RecalcAll'], timeout=.1)
    assert phases == [False, True, True, True]
    assert step['status'] == 'succeeded'
    assert step['native_completion_verified']
    assert [event['status'] for event in step['placement_events']] == ['succeeded', 'failed', 'failed', 'succeeded']
    assert step['placement_events'][-1]['phase'] == 'after_native_completion'
    assert not (runtime.CONTROL_ROOT / 'inflight.json').exists()


def test_completion_arriving_during_window_transition_is_read_before_next_poll(tmp_path, monkeypatch):
    operation = runtime.Runtime(tmp_path / 'state').operation('test', {})
    fake_dispatch(monkeypatch, operation, completion=None, native_log=None)
    simulated_clock(monkeypatch)
    checks = []

    def changing_windows():
        step = operation.record['steps'][-1]
        checks.append(bool(step.get('native_completion_verified')))
        if len(checks) == 2:
            Path(step['log']).write_text('COMPLETE! No Errors.')
            (Path(step['script']).parent / 'completed.txt').write_text(step['completion_token'])
            raise PrismError('PLACEMENT_TRANSIENT', 'simulated welcome transition')
        return {'configured': True, 'moved': []}

    monkeypatch.setattr(placement, 'enforce', changing_windows)
    step = operation.script(['RecalcAll'], timeout=1)
    assert checks == [False, False, True]
    assert step['status'] == 'succeeded'
    assert not (runtime.CONTROL_ROOT / 'inflight.json').exists()


@pytest.mark.parametrize('native_complete', [True, False])
def test_persistent_window_transition_keeps_gate_until_native_control_is_ready(tmp_path, monkeypatch, native_complete):
    operation = runtime.Runtime(tmp_path / 'state').operation('test', {})
    fake_dispatch(monkeypatch, operation, completion='current' if native_complete else None,
                  native_log='COMPLETE! No Errors.' if native_complete else None)
    clock = simulated_clock(monkeypatch)
    checks = []

    def never_stable():
        checks.append(clock['now'])
        if len(checks) > 1:
            raise PrismError('PLACEMENT_TRANSIENT', 'simulated persistent window replacement')
        return {'configured': True, 'moved': []}

    monkeypatch.setattr(placement, 'enforce', never_stable)
    with pytest.raises(PrismError) as error:
        operation.script(['RecalcAll'], timeout=5)
    assert error.value.code == 'PLACEMENT_UNAVAILABLE'
    assert error.value.details['native_completion_verified'] is native_complete
    assert 2 <= checks[-1] - checks[0] < 2.2
    step = operation.record['steps'][-1]
    assert step['status'] == 'failed'
    assert step['native_terminal_verified'] is native_complete
    assert (runtime.CONTROL_ROOT / 'inflight.json').exists() is not bool(step.get('native_control_ready'))
    assert operation.record['status'] != 'succeeded'


@pytest.mark.parametrize('code', ['DISPLAY_UNAVAILABLE', 'CONTROL_UNAVAILABLE'])
def test_final_display_or_permission_failure_is_not_retried_or_reported_as_success(tmp_path, monkeypatch, code):
    operation = runtime.Runtime(tmp_path / 'state').operation('test', {})
    fake_dispatch(monkeypatch, operation)
    checks = []

    def hard_failure():
        checks.append(None)
        if len(checks) > 1:
            raise PrismError(code, 'simulated hard placement failure', stderr='Not authorized (-1743)')
        return {'configured': True, 'moved': []}

    monkeypatch.setattr(placement, 'enforce', hard_failure)
    with pytest.raises(PrismError) as error:
        operation.script(['RecalcAll'], timeout=.1)
    assert error.value.code == code
    assert len(checks) == 2
    step = operation.record['steps'][-1]
    assert step['status'] == 'failed'
    assert step['native_completion_verified']
    assert step['error']['code'] == code
    assert (runtime.CONTROL_ROOT / 'inflight.json').exists() is not bool(step.get('native_control_ready'))


def test_failed_placement_before_dispatch_creates_no_uncertain_native_operation(tmp_path, monkeypatch):
    operation = runtime.Runtime(tmp_path / 'state').operation('test', {})

    def unavailable():
        raise PrismError('PLACEMENT_TRANSIENT', 'simulated window transition before dispatch')

    monkeypatch.setattr(placement, 'enforce', unavailable)
    with pytest.raises(PrismError) as error:
        operation.script(['RecalcAll'], timeout=.1)
    assert error.value.code == 'PLACEMENT_TRANSIENT'
    assert operation.record['steps'][-1]['status'] == 'failed'
    assert not (runtime.CONTROL_ROOT / 'inflight.json').exists()


def test_native_window_detector_includes_modal_dialogs_but_excludes_nonmodal_documents_and_welcome(monkeypatch, isolated_control):
    script = {'title': 'GraphPad Prism Script'}
    starting = {'title': '0% done'}
    finished = {'title': '100% done'}
    axes = {'title': 'Format Axes', 'modal': True}
    unnamed_modal = {'title': None, 'modal': True}
    monkeypatch.setattr(runtime, 'windows', lambda: [
        {'title': 'owned.prism', 'modal': False}, {'title': 'Welcome to Prism', 'modal': False},
        script, starting, finished, axes, unnamed_modal,
        {'title': 'Notes about 30% done.prism', 'modal': False}, {'title': None, 'modal': False},
    ])
    assert isolated_control() == [script, starting, finished, axes, unnamed_modal]


@pytest.mark.parametrize('title', ['GraphPad Prism Script', '37% done', 'Format Axes', None])
def test_existing_native_controller_or_modal_prevents_any_script_dispatch(tmp_path, monkeypatch, title, isolated_control):
    operation = runtime.Runtime(tmp_path / 'state').operation('test', {})
    controllers = [{'title': title, 'modal': True}]
    monkeypatch.setattr(runtime, 'windows', lambda: controllers + [
        {'title': 'unrelated.prism', 'modal': False}, {'title': 'Welcome to Prism', 'modal': False},
    ])
    monkeypatch.setattr(runtime, 'native_script_windows', isolated_control)
    with pytest.raises(PrismError) as error:
        operation.script(['RecalcAll'])
    assert error.value.code == 'PRISM_BUSY'
    assert error.value.details['native_script_windows'] == controllers
    assert operation.record['steps'][-1]['status'] == 'failed'
    assert not (runtime.CONTROL_ROOT / 'inflight.json').exists()


def test_terminal_output_waits_for_sender_and_controller_cleanup_before_success(tmp_path, monkeypatch):
    instance = runtime.Runtime(tmp_path / 'state')
    operation = instance.operation('test', {})
    output = tmp_path / 'fresh-output.txt'
    process = fake_dispatch(monkeypatch, operation, returncode=None,
                            produce=lambda: output.write_text('test-only native output'))
    clock = simulated_clock(monkeypatch)
    waits, controller_checks = [], []

    def delayed_sender(timeout=None):
        waits.append(timeout)
        assert timeout is not None and timeout >= 2
        durable = instance.status(operation.run_id)['steps'][-1]
        assert durable['native_completion_verified']
        assert durable['native_cleanup_uncertain']
        assert not durable.get('native_control_ready')
        assert durable['status'] != 'succeeded'
        assert 'output_sha256' not in durable
        assert (runtime.CONTROL_ROOT / 'inflight.json').exists()
        clock['now'] += 2
        process.returncode = 0
        return 0

    def controllers():
        controller_checks.append(None)
        if len(controller_checks) == 1:  # No controller exists before dispatch.
            return []
        assert waits and process.returncode == 0
        assert (runtime.CONTROL_ROOT / 'inflight.json').exists()
        assert operation.record['steps'][-1]['status'] != 'succeeded'
        return [{'title': '100% done'}] if len(controller_checks) < 4 else []

    monkeypatch.setattr(process, 'wait', delayed_sender)
    monkeypatch.setattr(runtime, 'native_script_windows', controllers)
    step = operation.script(['RecalcAll'], expected=[output], timeout=.1)
    assert step['status'] == 'succeeded'
    assert step['sender_returncode'] == 0
    assert step['native_control_ready']
    assert not step['native_cleanup_uncertain']
    assert step['output_sha256'][str(output)] == runtime.digest(output)
    assert not process.terminated
    assert not (runtime.CONTROL_ROOT / 'inflight.json').exists()
    assert instance.status(operation.run_id)['steps'][-1]['native_control_ready']


@pytest.mark.parametrize('failure, code', [
    ('sender_timeout', 'PRISM_CLEANUP_TIMEOUT'),
    ('sender_nonzero', 'PRISM_CLEANUP_FAILED'),
    ('persistent_controller', 'PRISM_CLEANUP_TIMEOUT'),
    ('controller_read_failed', 'CONTROL_UNAVAILABLE'),
])
def test_terminal_output_with_uncertain_cleanup_never_releases_gate(tmp_path, monkeypatch, failure, code):
    instance = runtime.Runtime(tmp_path / 'state')
    operation = instance.operation('test', {})
    returncode = None if failure == 'sender_timeout' else 7 if failure == 'sender_nonzero' else 0
    process = fake_dispatch(monkeypatch, operation, returncode=returncode)
    simulated_clock(monkeypatch)
    checks = []

    def controllers():
        checks.append(None)
        if len(checks) == 1:
            return []
        if failure == 'controller_read_failed':
            raise PrismError('CONTROL_UNAVAILABLE', 'Test-only native cleanup inspection failure')
        return [{'title': 'GraphPad Prism Script'}] if failure == 'persistent_controller' else []

    monkeypatch.setattr(runtime, 'native_script_windows', controllers)
    with pytest.raises(PrismError) as error:
        operation.script(['RecalcAll'], timeout=.1)
    assert error.value.code == code
    step = instance.status(operation.run_id)['steps'][-1]
    assert step['status'] == 'failed'
    assert step['native_completion_verified']
    assert step['native_cleanup_uncertain']
    assert not step.get('native_control_ready')
    assert 'output_sha256' not in step
    assert process.terminated is (failure == 'sender_timeout')
    assert (runtime.CONTROL_ROOT / 'inflight.json').exists()
    with pytest.raises(PrismError) as next_error:
        with runtime.Runtime(tmp_path / 'another-state').locked():
            pytest.fail('Unverified native cleanup permitted another mutation')
    assert next_error.value.code == 'RECOVERY_REQUIRED'


def test_native_error_releases_gate_only_after_sender_and_controller_cleanup(tmp_path, monkeypatch):
    instance = runtime.Runtime(tmp_path / 'state')
    operation = instance.operation('test', {})
    fake_dispatch(monkeypatch, operation, completion=None, returncode=3,
                  native_log='Test-only native failure\nPROBLEM! Not completed because of error.')
    simulated_clock(monkeypatch)
    dismissed = []

    def dismiss(script):
        durable = instance.status(operation.run_id)['steps'][-1]
        assert durable['native_cleanup_uncertain']
        assert durable['native_terminal_outcome'] == 'failed'
        assert (runtime.CONTROL_ROOT / 'inflight.json').exists()
        dismissed.append(script)

    monkeypatch.setattr(runtime.Operation, '_dismiss_error', staticmethod(dismiss))
    with pytest.raises(PrismError) as error:
        operation.script(['Test-only failing command'], timeout=.1)
    assert error.value.code == 'PRISM_SCRIPT_ERROR'
    step = instance.status(operation.run_id)['steps'][-1]
    assert dismissed == [Path(step['script'])]
    assert step['status'] == 'failed' and step['native_terminal_outcome'] == 'failed'
    assert step['sender_returncode'] == 3
    assert step['native_control_ready'] and not step['native_cleanup_uncertain']
    assert not (runtime.CONTROL_ROOT / 'inflight.json').exists()


@pytest.mark.parametrize('failure', ['sender', 'controller'])
def test_native_error_keeps_quarantine_when_sender_or_controller_hangs(tmp_path, monkeypatch, failure):
    instance = runtime.Runtime(tmp_path / 'state')
    operation = instance.operation('test', {})
    fake_dispatch(monkeypatch, operation, completion=None, returncode=None if failure == 'sender' else 3,
                  native_log='PROBLEM! Not completed because of error.')
    simulated_clock(monkeypatch)
    monkeypatch.setattr(runtime.Operation, '_dismiss_error', staticmethod(lambda script: None))
    checks = []

    def controllers():
        checks.append(None)
        return [{'title': 'GraphPad Prism Script'}] if failure == 'controller' and len(checks) > 1 else []

    monkeypatch.setattr(runtime, 'native_script_windows', controllers)
    with pytest.raises(PrismError) as error:
        operation.script(['Test-only failing command'], timeout=.1)
    assert error.value.code == 'PRISM_CLEANUP_TIMEOUT'
    step = instance.status(operation.run_id)['steps'][-1]
    assert step['status'] == 'failed' and step['native_terminal_outcome'] == 'failed'
    assert step['native_cleanup_uncertain'] and not step.get('native_control_ready')
    assert (runtime.CONTROL_ROOT / 'inflight.json').exists()
