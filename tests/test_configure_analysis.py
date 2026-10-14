"""Analysis-configuration orchestration tests; native execution is always simulated."""

from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import shutil
import zipfile

import pytest
from mcp import types as mcp_types

from prism_mcp import backend as module, placement, runtime
from prism_mcp.analysis_settings import DescriptiveSettings
from prism_mcp.archive import read_project
from prism_mcp.backend import Backend
from prism_mcp.errors import PrismError


@pytest.fixture(autouse=True)
def forbid_native_control(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, 'CONTROL_ROOT', tmp_path / 'global-control')
    monkeypatch.setenv('PRISM_MCP_STATE_DIR', str(tmp_path / 'server-state'))
    monkeypatch.setattr(runtime, 'application_info', lambda: {
        'installed': True, 'version_compatible': True, 'path': '/test-only/Prism.app',
        'version': '11.1.0', 'build': '110218',
    })
    monkeypatch.setattr(module, 'application_info', runtime.application_info)
    monkeypatch.setattr(runtime, 'verify_running_application', lambda: '')
    monkeypatch.setattr(module, 'windows', lambda: [])

    def prohibited(*args, **kwargs):
        pytest.fail('Analysis configuration test attempted actual application/process control')

    monkeypatch.setattr(runtime.Operation, 'script', prohibited)
    monkeypatch.setattr(runtime, 'osa', prohibited)
    monkeypatch.setattr(runtime.subprocess, 'Popen', prohibited)
    monkeypatch.setattr(placement, 'osa', prohibited)
    monkeypatch.setattr(placement, 'enforce', prohibited)


@pytest.fixture
def backend(tmp_path):
    return Backend(tmp_path / 'state')


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'source.prism'
    path.write_bytes(module.TEMPLATE.read_bytes())
    return path


def identity(source):
    return read_project(source)['analyses'][0]['id']


def rewrite(path, changes):
    """Alter only test-owned archives, preserving all unselected member payloads."""
    temporary = path.with_name(path.stem + '-test-rewrite.prism')
    with zipfile.ZipFile(path) as original, zipfile.ZipFile(temporary, 'w') as output:
        output.comment = original.comment
        for info in original.infolist():
            raw = original.read(info)
            output.writestr(copy.copy(info), changes[info.filename](raw) if info.filename in changes else raw)
    os.replace(temporary, path)


def change_json(path, member, transform):
    def update(raw):
        value = json.loads(raw)
        transform(value)
        return json.dumps(value).encode()
    rewrite(path, {member: update})


def parameters_member(source):
    return f'analyses/{identity(source)}/parameters.json'


def fake_native(monkeypatch, tmp_path, *, opened=None, on_save=None, fail=False, recalculate=True):
    state = {'current': opened, 'opened': [], 'saved_from': [], 'commands': []}

    def windows():
        current = state['current']
        return [] if current is None else [{'path': str(current), 'title': current.name, 'edited': False, 'modal': False}]

    def argument(command):
        text = command.split(' ', 1)[1]
        assert text.startswith('"') and text.endswith('"')
        path = Path(text[1:-1].replace('%%', '%'))
        assert path.is_relative_to(tmp_path), 'A simulated native writer escaped the test directory'
        return path

    def script(operation, commands, *, expected=(), **kwargs):
        state['commands'].append(list(commands))
        for command in commands:
            if command.startswith('Open '):
                state['current'] = argument(command)
                assert state['current'].is_file()
                state['opened'].append(state['current'])
            elif command == 'RecalcAll':
                if fail:
                    raise PrismError('PRISM_TIMEOUT', 'Simulated uncertain native execution')
                if recalculate:
                    from scipy.stats import t
                    for analysis in read_project(state['current'])['analyses']:
                        level = analysis['parameters']['content']['options']['confidenceLevel']

                        def compute_ci(results):
                            for sample in results['content']['samples'].values():
                                result = sample['value']
                                margin = float(t.ppf((1 + level / 100) / 2, result['n'] - 1)) * result['meanSE']
                                result['meanCI'] = {'lower': result['mean'] - margin, 'upper': result['mean'] + margin, 'level': level}

                        change_json(state['current'], f'analyses/{analysis["id"]}/results.json', compute_ci)
            elif command.startswith('Save '):
                current, destination = state['current'], argument(command)
                state['saved_from'].append(current)
                # Simulate Prism's ability to autosave its input before Save As.
                with zipfile.ZipFile(current, 'a') as archive:
                    archive.comment = b'simulated native autosave'
                shutil.copy2(current, destination)
                state['current'] = destination
                if on_save:
                    on_save(operation, destination)
            elif command == 'Close':
                state['current'] = None
        assert all(path.is_file() and path.stat().st_size for path in expected)
        return {'status': 'succeeded', 'test_only_native_adapter': True}

    monkeypatch.setattr(module, 'windows', windows)
    monkeypatch.setattr(runtime.Operation, 'script', script)
    return state


@pytest.mark.parametrize('was_open', [False, True])
def test_configure_analysis_preserves_originals_and_reopens_only_previously_open_target(
        backend, source, tmp_path, monkeypatch, was_open):
    original = source.read_bytes()
    before = read_project(source)
    native = fake_native(monkeypatch, tmp_path, opened=source if was_open else None)
    result = backend.configure_analysis(str(source), identity(source), DescriptiveSettings(
        mean_ci=True, sum=True, coefficient_of_variation=True, confidence_level=90, significant_digit_count=6),
        expected_sha256=before['sha256'], request_id='descriptive-change')

    operation = backend.runtime.status(result['run_id'])
    checkpoint = Path(operation['directory'])
    assert (checkpoint / 'original.prism').read_bytes() == original
    assert (checkpoint / 'input.prism').read_bytes() == original
    assert module.TEMPLATE not in native['opened']
    assert checkpoint / 'original.prism' not in native['opened']
    assert checkpoint / 'input.prism' not in native['opened']
    assert native['saved_from'] == [checkpoint / 'analysis-settings.prism']
    assert native['current'] == (source if was_open else None)
    after = read_project(source)
    assert after['tables'] == before['tables']
    assert after['graphs'] == before['graphs']
    assert after['layouts'] == before['layouts']
    assert result['configured_analysis_id'] == before['analyses'][0]['id']
    settings = result['analysis']['parameters']['content']
    assert settings['calc']['meanCI'] and settings['calc']['sum'] and settings['calc']['coefficientOfVariation']
    assert settings['options']['confidenceLevel'] == 90
    assert settings['output']['significantDigitCount'] == 6
    assert result['native_settings_verification']['parameters_roundtrip_verified']
    assert result['native_settings_verification']['original_observations_unchanged']
    assert not result['native_settings_verification']['displayed_significant_digits_verified']
    for check in result['native_settings_verification']['descriptive_results']:
        assert check['mean_ci_level_and_bounds_structure_verified']
        assert not check['mean_ci_endpoints_numerically_verified']
        assert {'mean', 'median', 'sum', 'sd', 'meanSE', 'coeffOfVariation'} <= set(check['numeric_fields_verified'])
    assert result['offline_parameter_edit']['status'] == 'file_edit_requires_native_recalculation'
    assert not result['offline_parameter_edit']['native_roundtrip_verified']
    assert (checkpoint / 'analysis-settings-edit.json').is_file()
    assert any(commands[1:3] == ['RecalcAll', f'Save {runtime.quote(checkpoint / "working.prism")}'] for commands in native['commands'])
    assert len(result['native_summary_verification']) == 2


@pytest.mark.parametrize('settings', [{}, {'confidence_level': 99}, {'sum': 1}, {'mean': False}, {'significant_digit_count': 6.0}])
def test_invalid_settings_fail_without_native_dispatch(backend, source, settings):
    original = source.read_bytes()
    with pytest.raises(PrismError) as error:
        backend.configure_analysis(str(source), identity(source), settings)
    assert error.value.code == 'INVALID_ARGUMENT'
    assert source.read_bytes() == original


def test_wrong_analysis_uuid_and_stale_revision_are_rejected(backend, source):
    for analysis_id, fingerprint, code in [('00000000-0000-4000-8000-000000000001', None, 'OBJECT_NOT_FOUND'),
                                          (identity(source), '0' * 64, 'DOCUMENT_CHANGED')]:
        with pytest.raises(PrismError) as error:
            backend.configure_analysis(str(source), analysis_id, DescriptiveSettings(sum=True), fingerprint)
        assert error.value.code == code


def test_other_analysis_class_is_rejected_before_dispatch(backend, source):
    analysis_id = identity(source)
    for member in (f'analyses/{analysis_id}/sheet.json', f'analyses/{analysis_id}/parameters.json'):
        change_json(source, member, lambda value: value.update(analysisClass='TEST_ONLY_UNSUPPORTED_CLASS'))
    original = source.read_bytes()
    with pytest.raises(PrismError) as error:
        backend.configure_analysis(str(source), analysis_id, DescriptiveSettings(sum=True))
    assert error.value.code == 'UNSUPPORTED_ANALYSIS_SETTINGS'
    assert source.read_bytes() == original


@pytest.mark.parametrize('edited', [True, None])
def test_dirty_or_unknown_target_is_refused_without_checkpoint_edit(backend, source, monkeypatch, edited):
    monkeypatch.setattr(module, 'windows', lambda: [{'path': str(source), 'title': 'test source', 'edited': edited, 'modal': False}])
    with pytest.raises(PrismError) as error:
        backend.configure_analysis(str(source), identity(source), DescriptiveSettings(sum=True))
    assert error.value.code == 'UNSAVED_CHANGES'
    checkpoint = Path(error.value.details['checkpoint'])
    assert not (checkpoint / 'analysis-settings.prism').exists()
    assert not (checkpoint / 'input.prism').exists()


def test_open_owned_checkpoint_is_refused_before_offline_edit(backend, source, monkeypatch):
    def windows():
        candidates = list((backend.runtime.state / 'runs').glob('*/input.prism'))
        return [] if not candidates else [{'path': str(candidates[0]), 'title': 'test checkpoint', 'edited': False, 'modal': False}]
    monkeypatch.setattr(module, 'windows', windows)
    with pytest.raises(PrismError) as error:
        backend.configure_analysis(str(source), identity(source), DescriptiveSettings(sum=True))
    assert error.value.code == 'DOCUMENT_OPEN'
    assert not (Path(error.value.details['checkpoint']) / 'analysis-settings.prism').exists()


def test_replay_does_not_edit_or_recalculate_twice_and_conflicting_request_fails(backend, source, tmp_path, monkeypatch):
    native = fake_native(monkeypatch, tmp_path)
    fingerprint = runtime.digest(source)
    arguments = (str(source), identity(source), DescriptiveSettings(sum=True), fingerprint, 'same-request')
    first = backend.configure_analysis(*arguments)
    commands = copy.deepcopy(native['commands'])
    second = backend.configure_analysis(*arguments)
    assert second['idempotent_replay'] and first['run_id'] == second['run_id']
    assert native['commands'] == commands
    with pytest.raises(PrismError) as error:
        backend.configure_analysis(str(source), identity(source), DescriptiveSettings(sum=False), fingerprint, 'same-request')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    assert native['commands'] == commands


@pytest.mark.parametrize('fault', ['native_failure', 'observations', 'settings', 'boolean_type', 'native_mean', 'native_cv', 'stale_ci_level', 'ci_bounds', 'checkpoint'])
def test_failed_native_roundtrip_never_publishes(backend, source, tmp_path, monkeypatch, fault):
    original, analysis_id = source.read_bytes(), identity(source)

    def on_save(operation, working):
        parameters = f'analyses/{analysis_id}/parameters.json'
        results = f'analyses/{analysis_id}/results.json'
        if fault == 'observations':
            table = read_project(working)['tables'][0]
            member = f'data/tables/{table["table_id"]}/data.csv'
            rewrite(working, {member: lambda raw: raw.replace(b'1.23456789012345', b'9.23456789012345')})
        elif fault in ('settings', 'boolean_type'):
            change_json(working, parameters, lambda value: value['content']['calc'].update(sum=False if fault == 'settings' else 1))
        elif fault in ('native_mean', 'native_cv', 'ci_bounds'):
            def break_result(value):
                sample = next(iter(value['content']['samples'].values()))['value']
                if fault == 'native_mean': sample['mean'] += 1
                elif fault == 'native_cv': sample['coeffOfVariation'] += 1
                else: sample['meanCI']['lower'] += 1
            change_json(working, results, break_result)
        elif fault == 'checkpoint':
            (operation.directory / 'input.prism').write_bytes(b'unexpected checkpoint writer')

    fake_native(monkeypatch, tmp_path, on_save=on_save, fail=fault == 'native_failure', recalculate=fault != 'stale_ci_level')
    with pytest.raises(PrismError) as error:
        backend.configure_analysis(str(source), analysis_id, DescriptiveSettings(sum=True, mean_ci=True, confidence_level=90))
    wanted = {'native_failure': 'PRISM_TIMEOUT', 'observations': 'UNEXPECTED_DATA_CHANGE',
              'settings': 'ANALYSIS_SETTINGS_MISMATCH', 'boolean_type': 'ANALYSIS_SETTINGS_MISMATCH',
              'checkpoint': 'CHECKPOINT_CHANGED'}
    assert error.value.code == wanted.get(fault, 'STALE_NATIVE_RESULTS')
    assert source.read_bytes() == original
    checkpoint = Path(error.value.details['checkpoint'])
    assert (checkpoint / 'original.prism').read_bytes() == original
    assert (checkpoint / 'analysis-settings-edit.json').is_file()
    assert backend.runtime.status(error.value.details['run_id'])['status'] == 'failed'


def test_unknown_parameter_precision_is_checked_without_float_rounding(backend, source, tmp_path, monkeypatch):
    member = parameters_member(source)
    precise = b'0.1234567890123456789012345678901234'
    rewrite(source, {member: lambda raw: raw.rstrip()[:-1] + b', "unknownPreciseParameter": ' + precise + b'}'})
    original = source.read_bytes()

    def on_save(operation, working):
        rewrite(working, {member: lambda raw: raw.replace(precise, b'0.1234567890123456789012345678901235')})

    fake_native(monkeypatch, tmp_path, on_save=on_save)
    with pytest.raises(PrismError) as error:
        backend.configure_analysis(str(source), identity(source), DescriptiveSettings(sum=True))
    assert error.value.code == 'ANALYSIS_SETTINGS_MISMATCH'
    assert source.read_bytes() == original


@pytest.mark.parametrize('interruption', ['concurrent_revision', 'cancellation'])
def test_publication_preserves_concurrent_revision_or_honors_cancellation(backend, source, tmp_path, monkeypatch, interruption):
    original = source.read_bytes()
    later_revision = b'independent later source revision'

    def on_save(operation, working):
        if interruption == 'concurrent_revision': source.write_bytes(later_revision)
        else: (operation.directory / 'cancel.request').touch()

    fake_native(monkeypatch, tmp_path, on_save=on_save)
    with pytest.raises(PrismError) as error:
        backend.configure_analysis(str(source), identity(source), DescriptiveSettings(sum=True))
    assert error.value.code == ('DOCUMENT_CHANGED' if interruption == 'concurrent_revision' else 'CANCELLED')
    assert source.read_bytes() == (later_revision if interruption == 'concurrent_revision' else original)


def test_capability_remains_experimental(backend, monkeypatch):
    monkeypatch.setattr(backend, '_configured', lambda: False)
    capability = backend.capabilities()['experimental']['analysis_configuration']
    assert capability['tool'] == 'prism_configure_analysis'
    assert capability['status'] == 'native_differential_verified_on_pinned_build'
    assert capability['settings']['confidence_level'] == [90, 95]
    assert capability['settings']['significant_digit_count'] == [6, 10]


def test_server_wrapper_delegates_typed_settings_and_returns_structured_failures(backend, monkeypatch):
    server = importlib.import_module('prism_mcp.server')
    monkeypatch.setattr(server, 'backend', backend)
    seen = []

    def configure(*arguments):
        seen.append(arguments)
        return {'ok': True, 'test_only': True}

    monkeypatch.setattr(backend, 'configure_analysis', configure)
    settings = DescriptiveSettings(sum=True)
    result = server.prism_configure_analysis('/test.prism', 'test-uuid', settings, 'a' * 64, 'request')
    assert isinstance(result, mcp_types.CallToolResult)
    assert not result.is_error and result.structured_content == {'ok': True, 'test_only': True}
    assert seen == [('/test.prism', 'test-uuid', settings, 'a' * 64, 'request')]

    def failure(*arguments):
        raise PrismError('ANALYSIS_SETTINGS_MISMATCH', 'Test-only settings rejection')

    monkeypatch.setattr(backend, 'configure_analysis', failure)
    result = server.prism_configure_analysis('/test.prism', 'test-uuid', settings)
    assert result.is_error and result.structured_content['error']['code'] == 'ANALYSIS_SETTINGS_MISMATCH'
