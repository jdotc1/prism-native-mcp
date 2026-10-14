"""Recovery evidence tests. Native process and UI boundaries are never invoked."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from prism_mcp import placement, runtime
from prism_mcp.errors import PrismError


@pytest.fixture(autouse=True)
def isolated_native_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, 'CONTROL_ROOT', tmp_path / 'global-control')
    monkeypatch.setattr(runtime, 'application_info', lambda: {
        'installed': True, 'version_compatible': True, 'path': '/test-only/Prism.app',
    })
    monkeypatch.setattr(runtime, 'verify_running_application', lambda: '/test-only/Prism.app')
    monkeypatch.setattr(runtime, 'native_script_windows', lambda: [])

    def forbidden(*args, **kwargs):
        pytest.fail('Recovery unit test attempted actual native control')

    monkeypatch.setattr(runtime, 'osa', forbidden)
    monkeypatch.setattr(runtime.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(runtime.Operation, '_dismiss_error', forbidden)
    monkeypatch.setattr(placement, 'POLICY', tmp_path / 'placement-policy.json')
    monkeypatch.setattr(placement, 'osa', forbidden)


@pytest.fixture
def pending(tmp_path):
    instance = runtime.Runtime(tmp_path / 'owner-state')
    operation = instance.operation('recovery-test', {'path': str(tmp_path / 'target.prism')})
    step_dir = operation.directory / 'step-01'
    step_dir.mkdir()
    script = step_dir / 'run.pzc'
    done = step_dir / 'completed.txt'
    log = step_dir / 'run.log'
    token = f'{operation.run_id}:1:{uuid.uuid4().hex}'
    script.write_text('\n'.join(['CreateLog', 'RecalcAll', f'OpenOutput {runtime.quote(done)}, clear',
                                f'WText {runtime.quote(token)}', 'CloseOutput', 'CloseScriptDialog', '']))
    step = {'number': 1, 'script': str(script), 'log': str(log), 'completion_token': token,
            'script_sha256': runtime.digest(script), 'started_at_ns': time.time_ns() - 1_000_000_000,
            'status': 'failed', 'expected_outputs': [str(tmp_path / 'missing-export.pdf')]}
    operation.record.update(status='failed', error=PrismError('PRISM_TIMEOUT', 'simulated prior timeout').as_dict(),
                            steps=[step])
    operation.persist()
    checkpoint = {'run_id': operation.run_id, 'script': str(script), 'log': str(log), 'done': str(done),
                  'token': token, 'operation': str(operation.directory / 'operation.json')}
    runtime.write_json(runtime.CONTROL_ROOT / 'inflight.json', checkpoint)
    log.write_bytes('COMPLETE! No Errors.\n'.encode('utf-32-le'))
    done.write_bytes((token + '\n').encode('utf-32-le'))
    return {'runtime': instance, 'operation': operation, 'checkpoint': checkpoint, 'step': step,
            'script': script, 'log': log, 'done': done, 'token': token}


def blocked(instance):
    with pytest.raises(PrismError) as error:
        instance.recover()
    assert error.value.code == 'RECOVERY_REQUIRED'
    assert (runtime.CONTROL_ROOT / 'inflight.json').exists()
    report = json.loads(Path(error.value.details['recovery_record']).read_text())
    assert report['status'] == 'blocked'
    assert report['quarantined']
    assert not report['quarantine_cleared']
    return error.value


def test_no_quarantine_reports_no_action(tmp_path):
    result = runtime.Runtime(tmp_path / 'state').recover()
    assert result['status'] == 'not_needed'
    assert not result['quarantine_cleared']
    assert not result['quarantined']
    assert Path(result['record_path']).is_file()


def test_matching_native_completion_clears_gate_without_claiming_semantic_success(pending):
    result = pending['runtime'].recover()
    assert result['status'] == 'quarantine_cleared'
    assert result['quarantine_cleared']
    assert result['native_outcome'] == 'completed'
    assert not result['semantic_completion_verified']
    assert not (runtime.CONTROL_ROOT / 'inflight.json').exists()
    status = pending['runtime'].status(pending['operation'].run_id)
    assert status['status'] == 'failed'
    assert status['error']['code'] == 'PRISM_TIMEOUT'
    assert status['recoveries'][-1]['recovery_id'] == result['recovery_id']
    assert status['steps'][0]['recovery']['terminal_evidence_verified']
    assert result['evidence']['script']['sha256'] == runtime.digest(pending['script'])


def test_recovery_can_verify_operation_owned_by_another_state_directory(pending, tmp_path):
    other = runtime.Runtime(tmp_path / 'different-state')
    result = other.recover()
    assert result['run_id'] == pending['operation'].run_id
    assert result['quarantine_cleared']
    assert pending['runtime'].status(result['run_id'])['recoveries']


def test_interrupted_running_operation_becomes_failed_after_native_termination(pending):
    op = pending['operation']
    op.record['status'] = 'running'
    op.record['steps'][0]['status'] = 'running'
    op.record.pop('error')
    op.persist()
    result = pending['runtime'].recover()
    status = pending['runtime'].status(op.run_id)
    assert result['prior_operation_status'] == 'running'
    assert status['status'] == status['steps'][0]['status'] == 'failed'
    assert status['error']['code'] == 'RECOVERED_UNVERIFIED_OPERATION'


def test_terminal_native_error_dismisses_only_recorded_script_and_clears_gate(pending, monkeypatch):
    pending['done'].unlink()
    pending['log'].write_bytes('Command failed\nPROBLEM! Not completed because of error.\n'.encode('utf-32-le'))
    dismissed = []
    monkeypatch.setattr(runtime.Operation, '_dismiss_error', lambda script: dismissed.append(script))
    result = pending['runtime'].recover()
    assert result['native_outcome'] == 'failed'
    assert result['quarantine_cleared']
    assert dismissed == [pending['script']]
    assert result['error_dialog_dismissal'] == 'attempted_exact_script_only'


def test_terminal_error_after_application_exit_does_not_attempt_ui(pending, monkeypatch):
    pending['log'].write_text('PROBLEM! Not completed because of error.')
    monkeypatch.setattr(runtime, 'verify_running_application', lambda: '')
    result = pending['runtime'].recover()
    assert result['quarantine_cleared']
    assert result['error_dialog_dismissal'] == 'application_not_running'


@pytest.mark.parametrize('mode', ['wrong_token', 'token_substring', 'missing_token', 'missing_complete', 'nonterminal_error', 'no_log'])
def test_unknown_active_or_unmatched_evidence_remains_quarantined(pending, mode):
    if mode == 'wrong_token':
        pending['done'].write_text('another-operation')
    elif mode == 'token_substring':
        pending['done'].write_text(pending['token'] + ':unrelated')
    elif mode == 'missing_token':
        pending['done'].unlink()
    elif mode == 'missing_complete':
        pending['log'].write_text('Native script is still running')
    elif mode == 'nonterminal_error':
        pending['log'].write_text('PROBLEM! Not completed because of error.\nLater command is active')
    else:
        pending['log'].unlink()
    blocked(pending['runtime'])


def test_application_absence_alone_is_not_safe_recovery(pending, monkeypatch):
    pending['log'].write_text('No terminal native evidence')
    pending['done'].unlink()
    monkeypatch.setattr(runtime, 'verify_running_application', lambda: '')
    error = blocked(pending['runtime'])
    assert error.details['process_absence_is_not_terminal_evidence']


@pytest.mark.parametrize('corruption', ['truncated', 'duplicate_key', 'wrong_run', 'other_script', 'changed_script', 'different_step_token', 'wrong_owner'])
def test_corrupt_or_mismatched_operation_identity_never_clears_gate(pending, tmp_path, corruption):
    checkpoint_path = runtime.CONTROL_ROOT / 'inflight.json'
    checkpoint = pending['checkpoint'].copy()
    if corruption == 'truncated':
        checkpoint_path.write_text('{"run_id":')
    elif corruption == 'duplicate_key':
        checkpoint_path.write_text('{"run_id":"a","run_id":"b"}')
    elif corruption == 'changed_script':
        pending['script'].write_text(pending['script'].read_text() + 'Quit\n')
    elif corruption == 'wrong_owner':
        checkpoint['operation'] = str(tmp_path / 'unrelated.json')
        runtime.write_json(checkpoint_path, checkpoint)
    else:
        if corruption == 'wrong_run':
            checkpoint['run_id'] = 'a' * 32
        elif corruption == 'other_script':
            checkpoint['script'] = str(tmp_path / 'unrelated.pzc')
        else:
            checkpoint['token'] = f"{pending['operation'].run_id}:1:{'a' * 32}"
        runtime.write_json(checkpoint_path, checkpoint)
    before = checkpoint_path.read_bytes()
    blocked(pending['runtime'])
    assert checkpoint_path.read_bytes() == before


@pytest.mark.parametrize('member', ['log', 'done'])
def test_completion_evidence_from_before_dispatch_cannot_release_quarantine(pending, member):
    old = pending['step']['started_at_ns'] - 1_000_000_000
    os.utime(pending[member], ns=(old, old))
    blocked(pending['runtime'])


def test_recovery_uses_the_same_global_os_lock_as_mutations(pending, tmp_path):
    other = runtime.Runtime(tmp_path / 'other-state')
    with pending['runtime'].locked(recovery=True):
        with pytest.raises(PrismError) as error:
            other.recover()
        assert error.value.code == 'PRISM_BUSY'
    assert (runtime.CONTROL_ROOT / 'inflight.json').exists()


def test_changed_evidence_during_recovery_keeps_gate(pending, monkeypatch):
    pending['done'].unlink()
    pending['log'].write_text('PROBLEM! Not completed because of error.')
    monkeypatch.setattr(runtime.Operation, '_dismiss_error', lambda script: pending['log'].write_text('Another script is now active'))
    blocked(pending['runtime'])


def test_recovery_does_not_follow_an_evidence_symlink(pending, tmp_path):
    destination = tmp_path / 'unrelated.log'
    destination.write_bytes(pending['log'].read_bytes())
    pending['log'].unlink()
    pending['log'].symlink_to(destination)
    blocked(pending['runtime'])


def test_failure_to_persist_operation_preserves_quarantine(pending, monkeypatch):
    original_write = runtime.write_json
    operation_path = pending['operation'].directory / 'operation.json'

    def failed_write(path, value):
        if path == operation_path:
            raise OSError('simulated storage failure')
        return original_write(path, value)

    monkeypatch.setattr(runtime, 'write_json', failed_write)
    blocked(pending['runtime'])


def test_final_audit_write_failure_reports_actual_released_gate_state(pending, monkeypatch):
    original_write = runtime.write_json

    def failed_final_write(path, value):
        if value.get('status') in {'quarantine_cleared', 'recording_failed'}:
            raise OSError('simulated final audit storage failure')
        return original_write(path, value)

    monkeypatch.setattr(runtime, 'write_json', failed_final_write)
    with pytest.raises(PrismError) as error:
        pending['runtime'].recover()
    assert error.value.code == 'RECOVERY_RECORD_FAILED'
    assert error.value.details['quarantine_cleared']
    assert not error.value.details['quarantined']
    assert not (runtime.CONTROL_ROOT / 'inflight.json').exists()
    # Terminal evidence and the owning operation update were durable before release.
    status = pending['runtime'].status(pending['operation'].run_id)
    assert status['recoveries'][-1]['terminal_evidence_verified']


def test_initial_recovery_record_write_failure_is_structured_and_preserves_gate(pending, monkeypatch):
    def failed_write(path, value):
        raise OSError('simulated unavailable audit storage')

    monkeypatch.setattr(runtime, 'write_json', failed_write)
    with pytest.raises(PrismError) as error:
        pending['runtime'].recover()
    assert error.value.code == 'RECOVERY_REQUIRED'
    assert error.value.details['recovery_record_write_error'] == 'OSError'
    assert (runtime.CONTROL_ROOT / 'inflight.json').exists()


@pytest.mark.parametrize('uncertain, controllers', [
    (True, []),
    (False, [{'title': '100% done'}]),
])
def test_recovery_keeps_gate_while_running_prism_has_uncertain_cleanup(pending, monkeypatch, uncertain, controllers):
    operation = pending['operation']
    operation.record['steps'][0]['native_cleanup_uncertain'] = uncertain
    operation.persist()
    original_operation = (operation.directory / 'operation.json').read_bytes()
    monkeypatch.setattr(runtime, 'native_script_windows', lambda: controllers)
    error = blocked(pending['runtime'])
    assert error.details['native_terminal_evidence_verified']
    assert not error.details['native_controller_cleanup_verified']
    assert (operation.directory / 'operation.json').read_bytes() == original_operation


@pytest.mark.parametrize('terminal_proof', [True, False])
def test_cleanup_failure_recovers_after_app_exit_only_with_terminal_proof(pending, monkeypatch, terminal_proof):
    operation = pending['operation']
    operation.record['steps'][0]['native_cleanup_uncertain'] = True
    operation.record['error'] = PrismError('PRISM_CLEANUP_TIMEOUT', 'Test-only prior cleanup failure').as_dict()
    operation.persist()
    monkeypatch.setattr(runtime, 'verify_running_application', lambda: '')

    def no_closed_app_ui():
        pytest.fail('Closed Prism must not require native window inspection')

    monkeypatch.setattr(runtime, 'native_script_windows', no_closed_app_ui)
    if not terminal_proof:
        pending['done'].write_text('unrelated-token')
        error = blocked(pending['runtime'])
        assert error.details['process_absence_is_not_terminal_evidence']
        return
    result = pending['runtime'].recover()
    assert result['quarantine_cleared']
    assert result['native_outcome'] == 'completed'
    assert not result['semantic_completion_verified']
    status = pending['runtime'].status(operation.run_id)
    assert status['status'] == 'failed'
    assert status['error']['code'] == 'PRISM_CLEANUP_TIMEOUT'
    assert status['steps'][0]['recovery']['terminal_evidence_verified']
