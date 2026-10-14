"""Pure failure-runner checks; no MCP server or native application is started."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from typing import Literal
import zipfile

from mcp import types
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools.base import Tool
import pytest

from prism_mcp import runtime
from prism_mcp.errors import PrismError
from prism_mcp.models import TableInput


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location('mcp_failure_acceptance', ROOT / 'experiments/mcp_failure_acceptance.py')
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.fixture(autouse=True)
def forbid_native(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail('A pure failure-acceptance test attempted live MCP/native control')

    monkeypatch.setattr(runner, 'Client', forbidden)
    monkeypatch.setattr(runtime, 'CONTROL_ROOT', tmp_path / 'global-control')
    monkeypatch.setattr(runtime, 'osa', forbidden)
    monkeypatch.setattr(runtime.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(runtime, 'verify_running_application', forbidden)


@pytest.fixture
def project(tmp_path):
    project = tmp_path / 'project'
    template = project / 'src/prism_mcp/templates/column_violin_summary.prism'
    template.parent.mkdir(parents=True)
    shutil.copy2(ROOT / 'src/prism_mcp/templates/column_violin_summary.prism', template)
    return project


@pytest.fixture
def prepared(project):
    return runner.prepare(project / 'local/failures', project)


def response(payload=None, *, error=True, text=None):
    return types.CallToolResult(
        content=[types.TextContent(type='text', text=text if text is not None else json.dumps(payload))],
        structured_content=payload, is_error=error,
    )


def backend_failure(code='OBJECT_NOT_FOUND', run_id=None):
    return {'ok': False, 'error': {'code': code, 'message': 'Synthetic error',
                                  'details': {'run_id': run_id} if run_id else {}}}


def case(name='synthetic', code='OBJECT_NOT_FOUND', *, native=False):
    return {'name': name, 'tool': 'prism_read_table', 'expected_code': code,
            'validation_fields': [], 'expects_completed_native_step': native}


def test_preparation_preserves_native_seed_and_changes_only_format_field(prepared):
    runner.verify_protected(prepared)
    assert runner.sha256(Path(prepared['seed'])) == prepared['template']['sha256']
    directory = Path(prepared['directory'])
    with zipfile.ZipFile(prepared['seed']) as source, zipfile.ZipFile(directory / 'fixtures/incompatible-format.prism') as copy:
        assert source.namelist() == copy.namelist()
        assert json.loads(copy.read('document.json'))['formatVersion'] == '999-0-0'
        assert all(source.read(name) == copy.read(name) for name in source.namelist() if name != 'document.json')
    assert not Path(prepared['state_directory']).exists()
    precise = json.loads((directory / 'fixtures/precision-input.json').read_text())
    assert precise['measurement_resolution'] is None
    assert precise['columns'][0]['values'][0] == 1.2345678901234567


def test_existing_and_outside_output_paths_are_refused_without_overwrite(prepared, tmp_path):
    directory, project = Path(prepared['directory']), Path(prepared['project'])
    before = runner.snapshot([directory])
    with pytest.raises(runner.AcceptanceError, match='fresh'):
        runner.prepare(directory, project)
    assert runner.snapshot([directory]) == before
    with pytest.raises(runner.AcceptanceError, match='inside'):
        runner.prepare(tmp_path / 'outside', project)
    assert not (tmp_path / 'outside').exists()


def test_symlink_destination_outside_project_is_refused(project, tmp_path):
    (project / 'local').mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (project / 'local/redirect').symlink_to(outside, target_is_directory=True)
    with pytest.raises(runner.AcceptanceError, match='inside'):
        runner.prepare(project / 'local/redirect/run', project)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('change', ['content', 'add', 'remove', 'empty_directory', 'symlink', 'unexpected_publication'])
def test_integrity_guard_detects_changed_sources_or_destinations(prepared, change):
    directory = Path(prepared['directory'])
    sentinel = directory / 'existing-export/sentinel.txt'
    if change == 'content':
        sentinel.write_text('changed')
    elif change == 'add':
        (sentinel.parent / 'extra.svg').write_text('new')
    elif change == 'remove':
        sentinel.unlink()
    elif change == 'empty_directory':
        (sentinel.parent / 'new-empty').mkdir()
    elif change == 'symlink':
        sentinel.unlink()
        sentinel.symlink_to(directory / 'fixtures/synthetic-seed.prism')
    else:
        (directory / 'rejected-targets/precision-loss.prism').write_text('unexpected native success')
    with pytest.raises(runner.AcceptanceError, match='protected'):
        runner.verify_protected(prepared)


def test_prepare_only_records_zero_transport_and_native_execution(project):
    args = SimpleNamespace(directory=project / 'artifacts/prepare-only', project=project,
                           prepare_only=True, include_backend_diagnostics=True, timeout_seconds=180)
    assert asyncio.run(runner.run(args)) == 0
    evidence = json.loads((args.directory / 'verification.json').read_text())
    assert evidence['status'] == 'prepared_only'
    assert evidence['mcp_calls'] == evidence['cases'] == []
    assert not evidence['native_execution_attempted']
    assert not evidence['native_execution_performed']
    assert evidence['protected_paths_unchanged']
    assert evidence['backend_diagnostics']['status'] == 'not_attempted'
    assert not (args.directory / 'server-state').exists()


def test_exception_cannot_hide_integrity_failure(project, monkeypatch):
    async def corrupt_and_fail(args, fixtures, evidence):
        Path(fixtures['seed']).write_bytes(b'changed')
        raise RuntimeError('unrelated private detail')

    monkeypatch.setattr(runner, 'execute', corrupt_and_fail)
    args = SimpleNamespace(directory=project / 'local/final-guard', project=project,
                           prepare_only=False, include_backend_diagnostics=False, timeout_seconds=180)
    assert asyncio.run(runner.run(args)) == 1
    text = (args.directory / 'verification.json').read_text()
    evidence = json.loads(text)
    assert evidence['status'] == 'failed'
    assert evidence['protected_paths_unchanged'] is False
    assert 'integrity_failure' in evidence
    assert 'unrelated private detail' not in text


def schema_tools():
    def prism_inspect(path: str, include_values: bool = False):
        pytest.fail('Malformed schema unexpectedly reached the callable')

    def prism_create_document(path: str, tables: list[TableInput], layout: bool = True,
                              request_id: str | None = None):
        pytest.fail('Malformed schema unexpectedly reached the callable')

    def prism_export(path: str, output_directory: str, formats: list[Literal['pdf', 'svg', 'png']],
                     request_id: str | None = None):
        pytest.fail('Malformed schema unexpectedly reached the callable')

    return {function.__name__: Tool.from_function(function)
            for function in (prism_inspect, prism_create_document, prism_export)}


def test_every_malformed_case_matches_real_official_sdk_validation(prepared):
    tools = schema_tools()
    selected = [item for item in runner.cases(prepared, 'test') if item['validation_fields']]
    assert len(selected) == 7
    for item in selected:
        with pytest.raises(ToolError) as raised:
            asyncio.run(tools[item['tool']].run(item['arguments'], context=None))
        verdict = runner.verify_error(response(text=str(raised.value)), item)
        assert verdict == {'error_surface': 'mcp_sdk_argument_validation', 'error_code': None}


def test_failure_inventory_keeps_precision_exact_and_uuid_kinds_distinct(prepared):
    inventory = runner.cases(prepared, 'one-run')
    assert len(inventory) == len({item['name'] for item in inventory}) == 17
    by_name = {item['name']: item for item in inventory}
    assert by_name['wrong-kind-table-uuid']['arguments']['table_id'] == prepared['graph_id']
    assert by_name['nonexistent-table-uuid']['arguments']['table_id'] != prepared['table_id']
    precision = by_name['native-precision-loss']
    assert precision['expects_completed_native_step']
    assert precision['arguments']['tables'][0]['measurement_resolution'] is None
    assert all(item['arguments'].get('request_id', 'one-run:').startswith('one-run:') for item in inventory)
    assert all(not item['expects_completed_native_step'] for item in inventory[:-1])


def test_expected_structured_error_must_agree_with_text():
    payload = backend_failure()
    assert runner.verify_error(response(payload), case())['error_code'] == 'OBJECT_NOT_FOUND'
    for actual in [response(payload, error=False), response(backend_failure('INVALID_DOCUMENT')),
                   response(payload, text='not json'), response(payload, text=json.dumps(backend_failure('OTHER'))),
                   response(text='Generic internal error')]:
        with pytest.raises(runner.AcceptanceError):
            runner.verify_error(actual, case())


@pytest.mark.parametrize('text', [
    'Error executing tool prism_read_table: internal error',
    'Error executing tool another_tool: 1 validation error\npath [type=missing,',
    'Error executing tool prism_read_table: 1 validation error\nanother_field [type=missing,',
    "Error executing tool prism_read_table: 1 validation error\nanother_field\n [type=missing, input_value={'path': 'value'}]",
])
def test_unrelated_sdk_error_cannot_count_as_expected_validation(text):
    expected = {**case(), 'validation_fields': ['path', '[type=missing,']}
    with pytest.raises(runner.AcceptanceError):
        runner.verify_error(response(text=text), expected)


def test_backend_error_is_not_mislabeled_as_sdk_validation():
    expected = {**case(), 'validation_fields': ['path']}
    with pytest.raises(runner.AcceptanceError, match='SDK argument validation'):
        runner.verify_error(response(backend_failure()), expected)


def test_failure_verdict_requires_correct_terminal_operation_and_dispatch_state():
    operation = {'run_id': 'a' * 32, 'kind': 'update_table', 'status': 'failed',
                 'error': backend_failure()['error'], 'steps': []}
    assert runner.verify_failed_operation(operation, case())['steps'] == []
    operation['steps'] = [{'status': 'succeeded', 'completion_token': 'fresh',
                           'native_completion_verified': True, 'completion_evidence': {'done_sha256': 'b' * 64}}]
    with pytest.raises(runner.AcceptanceError, match='pre-dispatch'):
        runner.verify_failed_operation(operation, case())
    operation['error']['code'] = 'PRECISION_OR_DATA_MISMATCH'
    precision = case(code='PRECISION_OR_DATA_MISMATCH', native=True)
    assert runner.verify_failed_operation(operation, precision)['status'] == 'failed'
    for field, bad_value in [('status', 'running'), ('completion_token', None),
                             ('native_completion_verified', False), ('completion_evidence', {})]:
        changed = deepcopy(operation)
        changed['steps'][0][field] = bad_value
        with pytest.raises(runner.AcceptanceError, match='native completion'):
            runner.verify_failed_operation(changed, precision)
    operation['status'] = 'succeeded'
    with pytest.raises(runner.AcceptanceError, match='durable operation'):
        runner.verify_failed_operation(operation, precision)


def test_evidence_sanitizes_unrelated_titles_and_native_logs():
    value = {'run_id': 'a' * 32, 'status': 'failed', 'error': {'code': 'PRISM_TIMEOUT', 'message': 'private native error'},
             'steps': [{'status': 'failed', 'placement_events': [{'windows': [{'title': 'unrelated title'}]}]}]}
    encoded = json.dumps(runner.safe_response('prism_operation_status', response(value, error=False)))
    assert 'private native error' not in encoded
    assert 'unrelated title' not in encoded
    assert 'PRISM_TIMEOUT' in encoded


def test_transport_exception_is_recorded_as_uncertainty_not_expected_failure(tmp_path):
    class BrokenClient:
        async def call_tool(self, *args, **kwargs):
            raise RuntimeError('private transport detail')

    evidence = {'mcp_calls': []}
    client = runner.EvidenceClient(BrokenClient(), evidence, tmp_path)
    with pytest.raises(runner.AcceptanceError, match='transport failed'):
        asyncio.run(client.call('prism_read_table', {'path': 'synthetic'}))
    assert evidence['completion_uncertain']
    assert evidence['mcp_calls'][0]['status'] == 'transport_error'
    assert 'private transport detail' not in json.dumps(evidence)


@pytest.mark.parametrize('kind, expected, timeout', [('bad-script', 'PRISM_SCRIPT_ERROR', 8), ('timeout', 'PRISM_TIMEOUT', 0)])
def test_diagnostic_injection_is_fixed_and_has_no_document_commands(monkeypatch, tmp_path, kind, expected, timeout):
    monkeypatch.setattr(runtime, 'application_info', lambda: {'installed': True, 'version_compatible': True})
    monkeypatch.setattr(runtime, 'verify_running_application', lambda: '/synthetic/Prism.app')
    monkeypatch.setattr(runtime, 'windows', lambda: [])
    calls = []

    def script(self, commands, **kwargs):
        calls.append((commands, kwargs))
        raise PrismError(expected, 'Synthetic backend boundary')

    monkeypatch.setattr(runtime.Operation, 'script', script)
    result = runner.inject_diagnostic(tmp_path / 'isolated-state', kind)
    assert result['error']['code'] == expected
    assert calls == [([runner.BAD_COMMAND] if kind == 'bad-script' else [], {'timeout': timeout})]
    assert all(not command.startswith(('Open ', 'Save ', 'Close', 'Recalc', 'GoTo', 'Import')) for command in calls[0][0])
    operation = json.loads((Path(result['error']['details']['checkpoint']) / 'operation.json').read_text())
    assert operation['status'] == 'failed'
    assert operation['error']['code'] == expected


def test_nested_exception_diagnostics_exclude_uncontrolled_messages():
    error = ExceptionGroup('private group', [runner.AcceptanceError('Safe stage message'),
                                          RuntimeError('private exception')])
    encoded = json.dumps(runner.failure_record(error))
    assert 'Safe stage message' in encoded
    assert 'private group' not in encoded
    assert 'private exception' not in encoded


def test_precision_verdict_uses_actual_saved_tokens_and_rejects_false_loss(prepared):
    run_id = 'c' * 32
    checkpoint = Path(prepared['state_directory']) / 'runs' / run_id
    checkpoint.mkdir(parents=True)
    shutil.copy2(prepared['seed'], checkpoint / 'original.prism')
    shutil.copy2(prepared['seed'], checkpoint / 'working.prism')
    expected = runner.table_input(precise=True)
    table = {'title': expected['name'], 'columns': []}
    for group in expected['columns']:
        tokens = [format(value, '.15g') for value in group['values']]
        table['columns'].append({'title': group['label'], 'raw_values': tokens, 'values': list(map(float, tokens))})
    maximum = abs(expected['columns'][0]['values'][0] - table['columns'][0]['values'][0])
    document = {'tables': [table], 'sha256': runner.sha256(checkpoint / 'working.prism')}

    class SavedCheckpoint:
        async def success(self, name, arguments):
            assert name == 'prism_inspect'
            assert arguments == {'path': str(checkpoint / 'working.prism'), 'include_values': True}
            return document

    operation = {'run_id': run_id, 'error': {'details': {'allowed_error': 0, 'max_absolute_error': maximum}}}
    verdict = asyncio.run(runner.verify_precision(SavedCheckpoint(), prepared, operation))
    assert verdict['native_rounding_independently_verified']
    assert verdict['max_absolute_error'] == maximum > 0
    assert len(verdict['observations']) == 8
    table['columns'][0]['values'][0] = expected['columns'][0]['values'][0]
    table['columns'][0]['raw_values'][0] = repr(expected['columns'][0]['values'][0])
    with pytest.raises(runner.AcceptanceError, match='actual native rounding'):
        asyncio.run(runner.verify_precision(SavedCheckpoint(), prepared, operation))
