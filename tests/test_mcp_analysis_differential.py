"""Synthetic parser/numeric/transport tests only; no live MCP or native control."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from prism_mcp import runtime
from prism_mcp.archive import read_project
from prism_mcp.backend import TEMPLATE


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location('analysis_differential', ROOT / 'experiments/mcp_analysis_differential.py')
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.fixture(autouse=True)
def no_live_control(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('A differential parser test attempted live MCP or native control')
    monkeypatch.setattr(runner, 'Client', forbidden)
    monkeypatch.setattr(runtime.Operation, 'script', forbidden)
    monkeypatch.setattr(runtime, 'osa', forbidden)
    monkeypatch.setattr(runtime.subprocess, 'Popen', forbidden)


def uid(number):
    return str(uuid.UUID(int=number))


COMMON = [
    {'n': 5, 'mean': 6.1, 'sd': 3.8277359496182597, 'meanSE': 1.7118155566532276,
     'sum': 30.5, 'median': 5.125, 'minimum': 2.25, 'maximum': 11.875, 'coeffOfVariation': 62.74976966587311},
    {'n': 6, 'mean': 6.208333333333333, 'sd': 4.95947115796298, 'meanSE': 2.024695621843222,
     'sum': 37.25, 'median': 5.0625, 'minimum': 1.0625, 'maximum': 14.1875, 'coeffOfVariation': 79.88409918866546},
]
INTERVALS = {95: [(1.3472380767287095, 10.85276192327129), (1.0036875451307736, 11.412979121535892)],
             90: [(2.4506715067648517, 9.749328493235147), (2.128473714043655, 10.28819295262301)]}
CSV = {
    10: b'Number of values,5,6\n,,\nMean,6.100000000,6.208333333\nStd. Deviation,3.827735950,4.959471158\nStd. Error of Mean,1.711815557,2.024695622\n',
    6: b'Number of values,5,6\n,,\nMean,6.10000,6.20833\nStd. Deviation,3.82774,4.95947\nStd. Error of Mean,1.71182,2.02470\n',
}


def sync_samples(document):
    analysis = document['analyses'][0]
    analysis['results']['content']['samples'] = {uid(100 + index): {
        'id': {'col': {'uid': item['dataset_id']}},
        'value': {key: value for key, value in item.items() if key != 'dataset_id'},
    } for index, item in enumerate(analysis['summaries'])}


def document(level=95, digits=10):
    source = runner.table_input()
    columns = [{'id': uid(10 + index), 'title': column['label'], 'values': column['values'],
                'raw_values': list(map(str, column['values']))} for index, column in enumerate(source['columns'])]
    datasets = [column['id'] for column in columns]
    parameters = {'analysisClass': 'DESCRIPTIVE_STATISTICS', '$id': 'analyses/descriptive_statistics/parameters.schema.json',
        'content': {'data': {'handleReplicatesAs': 'AVERAGE'}, 'calc': {'mean': True, 'median': True, 'minMaxRange': True,
            'meanCI': True, 'sum': True, 'coefficientOfVariation': True, 'unknownRetainedFlag': False},
            'options': {'confidenceLevel': float(level), 'percentileLevel': 90.0},
            'output': {'significantDigitCount': digits}}}
    analysis = {'id': uid(30), 'index': 1, 'title': 'Synthetic descriptive analysis', 'analysis_class': 'DESCRIPTIVE_STATISTICS',
        'input_dataset_ids': datasets, 'input_sheet_ids': [uid(1)], 'parameters': parameters,
        'results': {'content': {'status': {'error': 'VALID'}}},
        'summaries': [{'dataset_id': identity, **deepcopy(values),
            'meanCI': {'level': float(level), 'lower': interval[0], 'upper': interval[1]}}
            for identity, values, interval in zip(datasets, COMMON, INTERVALS[level])]}
    result = {'path': '/synthetic-only/test.prism', 'format_version': '1-6-1', 'relationships_valid': True,
        'sha256': 'a' * 64, 'tables': [{'id': uid(1), 'index': 1, 'title': source['name'], 'table_id': uid(2),
                                     'dataset_ids': datasets, 'columns': columns}],
        'graphs': [{'id': uid(20), 'index': 1, 'title': 'Synthetic graph', 'input_dataset_ids': datasets}],
        'analyses': [analysis], 'layouts': []}
    sync_samples(result)
    return result


def checks(level=95, digits=10):
    doc = document(level, digits)
    return runner.verify_analysis(doc, doc['analyses'][0], runner.SETTINGS['baseline' if level == 95 else 'changed'], original=document())


def test_independent_t_calculation_uses_sample_sd_n_minus_one_and_two_sided_probability():
    expected = runner.independent_statistics([1, 2, 3, 4, 5], 95)
    assert expected['n'] == 5 and expected['df'] == 4 and expected['mean'] == 3
    assert expected['sd'] ** 2 == pytest.approx(2.5)
    assert expected['meanSE'] ** 2 == pytest.approx(.5)
    assert expected['t_critical'] == pytest.approx(2.7764451051977987, abs=2e-12)
    assert expected['lower'] == pytest.approx(1.036756838522439, abs=2e-12)
    assert expected['upper'] == pytest.approx(4.963243161477561, abs=2e-12)
    assert runner.table_input()['measurement_resolution'] is None


def test_both_saved_native_ci_levels_and_significant_digit_tokens_validate():
    baseline, changed = checks(), checks(90, 6)
    runner.compare_intervals(baseline, changed)
    for digits, values in [(10, baseline), (6, changed)]:
        tokens = runner.verify_result_text(CSV[digits], values, digits)
        assert len(tokens) == 6
        assert {item['significant_digits'] for item in tokens} == {digits}
    with pytest.raises(runner.AcceptanceError, match='did not narrow'):
        runner.compare_intervals(baseline, baseline)


@pytest.mark.parametrize('fault', ['stale_level', 'wrong_ci', 'normal_ci', 'rounded_results', 'boolean_n', 'boolean_level',
                                  'missing_group', 'unrequested_parameter', 'changed_identity', 'changed_observation', 'summary_disagreement'])
def test_saved_parameter_data_identity_and_ci_failures_are_not_accepted(fault):
    changed = document(90, 6)
    analysis, summary = changed['analyses'][0], changed['analyses'][0]['summaries'][0]
    if fault == 'stale_level':
        analysis['parameters']['content']['options']['confidenceLevel'] = 95
    elif fault == 'wrong_ci':
        summary['meanCI']['lower'] -= .01
    elif fault == 'normal_ci':
        summary['meanCI'].update(lower=summary['mean'] - 1.6448536269514722 * summary['meanSE'],
                                 upper=summary['mean'] + 1.6448536269514722 * summary['meanSE'])
    elif fault == 'rounded_results':
        summary['meanCI']['upper'] = round(summary['meanCI']['upper'], 5)
    elif fault == 'boolean_n':
        summary['n'] = True
    elif fault == 'boolean_level':
        summary['meanCI']['level'] = True
    elif fault == 'missing_group':
        analysis['summaries'].pop()
    elif fault == 'unrequested_parameter':
        analysis['parameters']['content']['options']['percentileLevel'] = 80
    elif fault == 'changed_identity':
        changed['graphs'][0]['id'] = uid(999)
    elif fault == 'changed_observation':
        changed['tables'][0]['columns'][0]['raw_values'][0] = '2.2500000001'
    else:
        analysis['results']['content']['samples'][uid(100)]['value']['meanCI']['upper'] += 1
    if fault != 'summary_disagreement':
        sync_samples(changed)
    with pytest.raises(runner.AcceptanceError):
        runner.verify_analysis(changed, analysis, runner.SETTINGS['changed'], original=document())


@pytest.mark.parametrize('fault', ['old_precision', 'stripped_zeros', 'wrong_value', 'missing_row', 'duplicate_row', 'extra_column', 'wrong_n', 'nonnumeric'])
def test_native_result_csv_must_have_correct_values_digits_rows_and_groups(fault):
    payload = CSV[6]
    if fault == 'old_precision':
        payload = CSV[10]
    elif fault == 'stripped_zeros':
        payload = payload.replace(b'6.10000', b'6.1')
    elif fault == 'wrong_value':
        payload = payload.replace(b'3.82774', b'3.82773')
    elif fault == 'missing_row':
        payload = payload.replace(b'Mean,6.10000,6.20833\n', b'')
    elif fault == 'duplicate_row':
        payload += b'Mean,6.10000,6.20833\n'
    elif fault == 'extra_column':
        payload = payload.replace(b'6.20833\n', b'6.20833,7.00000\n')
    elif fault == 'wrong_n':
        payload = payload.replace(b'values,5,6', b'values,6,5')
    else:
        payload = payload.replace(b'6.10000', b'NaN')
    with pytest.raises(runner.AcceptanceError):
        runner.verify_result_text(payload, checks(90, 6), 6)


def test_csv_reader_retains_quoted_tokens_and_scientific_notation():
    payload = CSV[6].replace(b'Mean,6.10000,6.20833', b'"Mean","6.10000e0","6.20833E+0"')
    assert len(runner.verify_result_text(b'\xef\xbb\xbf' + payload, checks(90, 6), 6)) == 6


def test_typed_parameters_reject_boolean_numbers_and_hidden_precision_loss():
    assert runner.typed(1) == runner.typed(1.0) == runner.typed(Decimal('1.00'))
    assert runner.typed(True) != runner.typed(1)
    exact = Decimal('0.12345678901234567890123456789')
    assert runner.typed(exact) != runner.typed(float(exact))
    bad = document()['analyses'][0]['parameters']
    bad['content']['calc']['meanCI'] = 1
    with pytest.raises(runner.AcceptanceError, match='scalar type'):
        runner.parameter_settings(bad)


def test_actual_bundled_native_result_view_csv_is_read_only_and_has_ten_digits():
    before = runner.sha256(TEMPLATE)
    native = read_project(TEMPLATE)
    analysis = native['analyses'][0]
    payload, mapping = runner.saved_result_view(TEMPLATE, analysis)
    expected = [{'group': column['title'], 'expected': runner.independent_statistics(
        [value for value in column['values'] if value is not None], 95)} for column in native['tables'][0]['columns']]
    assert mapping['group_labels'] == ['Group A', 'Group B']
    assert mapping['archive_member'].startswith('data/tables/')
    assert len(runner.verify_result_text(payload, expected, 10)) == 6
    assert runner.sha256(TEMPLATE) == before


def test_saved_archive_and_mcp_parameters_must_agree():
    analysis = deepcopy(read_project(TEMPLATE)['analyses'][0])
    analysis['parameters']['content']['output']['significantDigitCount'] = 6
    with pytest.raises(runner.AcceptanceError, match='parameters differ'):
        runner.saved_result_view(TEMPLATE, analysis)


def response(payload, *, error=False):
    return SimpleNamespace(is_error=error, structured_content=payload,
                           content=[SimpleNamespace(type='text', text=json.dumps(payload))])


@pytest.mark.parametrize('fault', [None, 'wrong_run', 'missing_log', 'unverified', 'empty_steps', 'replay', 'transport'])
def test_native_completion_gate_requires_current_evidence_and_never_retries(tmp_path, fault):
    calls = []
    run_id = 'a' * 32
    step = {'status': 'succeeded', 'completion_token': 'unique-test-token', 'native_completion_verified': True,
            'completion_evidence': {'log_sha256': 'b' * 64, 'done_sha256': 'c' * 64}}
    operation = {'run_id': run_id, 'request_id': 'synthetic:configure', 'status': 'succeeded', 'steps': [step]}
    if fault == 'wrong_run':
        operation['run_id'] = 'd' * 32
    elif fault == 'missing_log':
        del step['completion_evidence']['log_sha256']
    elif fault == 'unverified':
        step['native_completion_verified'] = False
    elif fault == 'empty_steps':
        operation['steps'] = []

    class FakeClient:
        async def call_tool(self, name, arguments):
            calls.append(name)
            if fault == 'transport':
                raise OSError('unrelated private text must not enter diagnostics')
            return response(operation if name == 'prism_operation_status' else
                            {'run_id': run_id, 'idempotent_replay': fault == 'replay'})

    evidence = {'mcp_calls': []}
    recorder = runner.Recorder(FakeClient(), tmp_path, evidence)
    action = recorder.call('prism_configure_analysis', {'request_id': 'synthetic:configure'}, native=True)
    if fault:
        with pytest.raises((runner.AcceptanceError, OSError)):
            asyncio.run(action)
        assert recorder.native_uncertain
        count = len(calls)
        with pytest.raises(runner.AcceptanceError, match='uncertain'):
            asyncio.run(recorder.call('prism_configure_analysis', {'request_id': 'another'}, native=True))
        assert len(calls) == count
        assert 'unrelated private text' not in (tmp_path / 'verification.json').read_text()
    else:
        asyncio.run(action)
        assert not recorder.native_uncertain and evidence['native_execution_performed'] is True
        assert evidence['mcp_calls'][0]['completion_verified']
        assert calls == ['prism_configure_analysis', 'prism_operation_status']


def test_prepare_only_writes_synthetic_targets_without_mcp_or_native_execution(tmp_path):
    directory = tmp_path / 'artifacts' / 'prepared'
    args = SimpleNamespace(directory=directory, project=tmp_path, prepare_only=True, timeout_seconds=180)
    assert asyncio.run(runner.run(args)) == 0
    evidence = json.loads((directory / 'verification.json').read_text())
    assert evidence['status'] == 'prepared_only' and evidence['mcp_calls'] == []
    assert not evidence['native_execution_attempted'] and not evidence['native_execution_performed']
    assert not evidence['independent_ci_endpoints_verified'] and not evidence['saved_result_text_significant_digits_verified']
    plan = json.loads((directory / 'synthetic-plan.json').read_text())
    assert [target['n'] for target in plan['independent_targets']['baseline']] == [5, 6]
    assert sorted(path.name for path in directory.iterdir()) == ['synthetic-plan.json', 'verification.json']
    with pytest.raises(runner.AcceptanceError, match='fresh'):
        asyncio.run(runner.run(args))
