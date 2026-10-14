"""Sequential failure acceptance through the official MCP 2.1.1 stdio client.

Only the parent task may execute this runner against Prism. ``--prepare-only``
creates synthetic disposable fixtures without starting MCP or native control.
The optional diagnostics inject fixed, document-free scripts through the backend
runtime; their error injection is explicitly NOT claimed as MCP tool coverage.
Operation inspection, quarantine refusal, and recovery still use the real MCP.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
import uuid
import zipfile

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from prism_mcp.archive import read_project
from prism_mcp.models import TableInput


REQUIRED_TOOLS = {
    'prism_capabilities', 'prism_inspect', 'prism_read_table',
    'prism_create_document', 'prism_update_table', 'prism_export',
    'prism_operation_status', 'prism_recover',
    'prism_open_document', 'prism_close_document',
}
BAD_COMMAND = 'PrismMCPIntentionallyUnknownDiagnosticCommand'
UNCERTAIN_CODES = {'PRISM_TIMEOUT', 'CONTROL_UNAVAILABLE', 'RECOVERY_REQUIRED', 'PRISM_CLEANUP_TIMEOUT', 'PRISM_CLEANUP_FAILED'}


class AcceptanceError(RuntimeError):
    """Only runner-authored diagnostic text is emitted to the terminal."""


def require(condition, message):
    if not condition:
        raise AcceptanceError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value):
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def failure_record(error: BaseException) -> dict:
    leaves = []

    def visit(current):
        if isinstance(current, BaseExceptionGroup):
            for child in current.exceptions:
                visit(child)
        else:
            leaves.append({'type': type(current).__name__, 'message': str(current)
                           if isinstance(current, AcceptanceError)
                           else 'Inspect the preserved local evidence before resuming'})

    visit(error)
    return {'type': type(error).__name__, 'causes': leaves}


def snapshot(paths: list[Path]) -> dict:
    """Detect changed content, added files, removed files, or symlink replacement."""
    result = {}
    for root in paths:
        entries = [root]
        if root.is_dir() and not root.is_symlink():
            entries += sorted(root.rglob('*'))
        for path in entries:
            key = str(path)
            if path.is_symlink():
                result[key] = {'kind': 'symlink', 'target': os.readlink(path)}
            elif path.is_file():
                result[key] = {'kind': 'file', 'bytes': path.stat().st_size, 'sha256': sha256(path)}
            elif path.is_dir():
                result[key] = {'kind': 'directory'}
            elif not path.exists():
                result[key] = {'kind': 'absent'}
            else:
                result[key] = {'kind': 'other'}
    return result


def table_input(*, precise=False) -> dict:
    value = {
        'name': 'Synthetic failure acceptance', 'units': 'arbitrary units',
        'columns': [
            {'label': 'Synthetic A', 'values': [1.2345678901234567 if precise else 1.25, 2.5, 3.75, 5.0]},
            {'label': 'Synthetic B', 'values': [1.5, 3.0, 4.5, 6.0]},
        ],
        # None means exact numeric preservation; this experiment never relaxes
        # precision to force a successful publication.
        'measurement_resolution': None,
    }
    return TableInput.model_validate(value).model_dump()


def prepare(directory: Path, project: Path) -> dict:
    """All input files are a bundled synthetic seed or intentional invalid copies."""
    directory, project = directory.expanduser().resolve(), project.expanduser().resolve()
    require(not directory.exists(), 'Failure acceptance requires a fresh output directory')
    roots = {Path(__file__).resolve().parents[1], project}
    allowed = [(root / name).resolve() for root in roots for name in ('local', 'artifacts')]
    require(any(root in directory.parents for root in allowed),
            'Acceptance outputs must be inside a project local/ or artifacts/ directory')
    template = project / 'src/prism_mcp/templates/column_violin_summary.prism'
    before = sha256(template)
    model = read_project(template)
    require(len(model['tables']) == len(model['graphs']) == len(model['analyses']) == 1,
            'Expected the bundled single-family synthetic native seed')
    directory.mkdir(parents=True, exist_ok=False)
    fixtures = directory / 'fixtures'
    targets = directory / 'rejected-targets'
    stale = directory / 'existing-export'
    for path in (fixtures, targets, stale):
        path.mkdir()
    seed = fixtures / 'synthetic-seed.prism'
    shutil.copy2(template, seed)
    (fixtures / 'malformed.prism').write_bytes(b'Intentional synthetic non-archive failure fixture.\n')
    incompatible = fixtures / 'incompatible-format.prism'
    with zipfile.ZipFile(seed) as source, zipfile.ZipFile(incompatible, 'x') as output:
        output.comment = source.comment
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == 'document.json':
                value = json.loads(data)
                value['formatVersion'] = '999-0-0'
                data = json.dumps(value, allow_nan=False).encode('utf-8')
            output.writestr(info, data)
    (stale / 'graph-1.svg').write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><text>Intentional stale fixture</text></svg>\n')
    (stale / 'sentinel.txt').write_text('This existing destination must remain unchanged.\n')
    atomic_json(fixtures / 'precision-input.json', table_input(precise=True))
    require(sha256(template) == sha256(seed) == before, 'Bundled seed changed while fixtures were prepared')
    # The incompatible copy must differ only in the requested format field.
    with zipfile.ZipFile(seed) as source, zipfile.ZipFile(incompatible) as output:
        require(source.namelist() == output.namelist(), 'Incompatible fixture member order differs')
        for name in source.namelist():
            if name == 'document.json':
                left, right = json.loads(source.read(name)), json.loads(output.read(name))
                right['formatVersion'] = left['formatVersion']
                require(left == right, 'Incompatible fixture changed more than its format version')
            else:
                require(source.read(name) == output.read(name), 'Incompatible fixture changed another member')
    try:
        read_project(incompatible)
    except ValueError as error:
        require('Unsupported Prism format version' in str(error), 'Incompatible fixture fails for an unrelated reason')
    else:
        raise AcceptanceError('Incompatible fixture was unexpectedly accepted offline')
    protected = [fixtures, targets, stale, template]
    result = {
        'directory': str(directory), 'project': str(project), 'seed': str(seed),
        'state_directory': str(directory / 'server-state'),
        'template': {'path': str(template), 'sha256': before},
        'table_id': model['tables'][0]['id'], 'graph_id': model['graphs'][0]['id'],
        'protected_paths': list(map(str, protected)), 'protected_before': snapshot(protected),
        'synthetic_only': True, 'precision_policy': 'exact_numeric_match_required',
    }
    atomic_json(directory / 'fixtures.json', result)
    return result


def cases(fixtures: dict, acceptance_id: str) -> list[dict]:
    directory = Path(fixtures['directory'])
    seed, target = fixtures['seed'], directory / 'rejected-targets'
    zero = str(uuid.UUID(int=0))
    result = []

    def add(name, tool, arguments, code=None, *, fields=(), native=False):
        if tool in {'prism_create_document', 'prism_update_table', 'prism_export'}:
            arguments['request_id'] = f'{acceptance_id}:{name}'
        result.append({'name': name, 'tool': tool, 'arguments': arguments,
                       'expected_code': code, 'validation_fields': list(fields),
                       'expects_completed_native_step': native})

    add('missing-required-path', 'prism_inspect', {'include_values': True}, fields=('path', '[type=missing,'))
    for name, changed in (
        ('text-measurement', 'not-a-measurement'),
        ('boolean-measurement', True),
        ('integer-precision-loss', 2**53 + 1),
    ):
        table = table_input()
        table['columns'][0]['values'][0] = changed
        add(name, 'prism_create_document', {'path': str(target / f'{name}.prism'), 'tables': [table]},
            fields=('tables.0.columns.0.values',))
    table = table_input()
    table['unrecognized_option'] = True
    add('unknown-table-option', 'prism_create_document',
        {'path': str(target / 'unknown-option.prism'), 'tables': [table]},
        fields=('tables.0.unrecognized_option', '[type=extra_forbidden,'))
    table = table_input()
    table['columns'][0]['values'] = [1.0, 2.0]
    add('insufficient-observations', 'prism_create_document',
        {'path': str(target / 'insufficient.prism'), 'tables': [table]},
        fields=('tables.0', 'at least three observations'))
    add('invalid-export-format', 'prism_export',
        {'path': seed, 'output_directory': str(target / 'invalid-format'), 'formats': ['eps']},
        fields=('formats.0', '[type=literal_error,'))
    add('missing-document', 'prism_read_table',
        {'path': str(directory / 'fixtures/missing.prism'), 'table_id': fixtures['table_id']}, 'INVALID_DOCUMENT')
    for name, identity in [('nonexistent-table-uuid', zero), ('wrong-kind-table-uuid', fixtures['graph_id']),
                           ('malformed-table-uuid', 'not-a-uuid')]:
        add(name, 'prism_read_table', {'path': seed, 'table_id': identity}, 'OBJECT_NOT_FOUND')
    for name, filename in [('malformed-archive', 'malformed.prism'), ('incompatible-format', 'incompatible-format.prism')]:
        add(name, 'prism_inspect', {'path': str(directory / 'fixtures' / filename)}, 'INVALID_DOCUMENT')
    add('stale-export-destination', 'prism_export',
        {'path': seed, 'output_directory': str(directory / 'existing-export'), 'formats': ['svg']}, 'OUTPUT_EXISTS')
    add('wrong-kind-update-uuid', 'prism_update_table',
        {'path': seed, 'table_id': fixtures['graph_id'], 'table': table_input()}, 'OBJECT_NOT_FOUND')
    add('stale-document-revision', 'prism_update_table',
        {'path': seed, 'table_id': fixtures['table_id'], 'table': table_input(), 'expected_sha256': '0' * 64},
        'DOCUMENT_CHANGED')
    add('native-precision-loss', 'prism_create_document',
        {'path': str(target / 'precision-loss.prism'), 'tables': [table_input(precise=True)], 'layout': False},
        'PRECISION_OR_DATA_MISMATCH', native=True)
    return result


def response_text(response) -> str:
    return '\n'.join(item.text for item in response.content if getattr(item, 'type', None) == 'text')


def verify_error(response, case: dict) -> dict:
    require(response.is_error is True, f"{case['name']}: an expected failure returned success")
    payload = response.structured_content
    if case['validation_fields']:
        text = response_text(response)
        require(payload is None, f"{case['name']}: expected SDK argument validation, received a backend result")
        require(f"Error executing tool {case['tool']}:" in text and 'validation error' in text.lower(),
                f"{case['name']}: unrelated text-only tool error cannot establish schema validation")
        fields = case['validation_fields']
        require(fields[0] in {line.strip() for line in text.splitlines()} and all(field in text for field in fields[1:]),
                f"{case['name']}: validation error does not identify the intended malformed field")
        return {'error_surface': 'mcp_sdk_argument_validation', 'error_code': None}
    require(isinstance(payload, dict) and payload.get('ok') is False,
            f"{case['name']}: missing structured backend failure")
    error = payload.get('error')
    require(isinstance(error, dict) and error.get('code') == case['expected_code'],
            f"{case['name']}: backend error did not match {case['expected_code']}")
    try:
        text_payload = json.loads(response_text(response))
    except (ValueError, TypeError):
        raise AcceptanceError(f"{case['name']}: text and structured error surfaces disagree") from None
    require(text_payload == payload, f"{case['name']}: text and structured error surfaces disagree")
    return {'error_surface': 'mcp_backend', 'error_code': error['code'],
            'run_id': error.get('details', {}).get('run_id')}


def operation_summary(value: dict) -> dict:
    return {
        'run_id': value.get('run_id'), 'status': value.get('status'), 'kind': value.get('kind'),
        'error_code': value.get('error', {}).get('code'),
        'steps': [{key: step.get(key) for key in (
            'number', 'status', 'script', 'script_sha256', 'completion_token',
            'native_completion_verified', 'native_terminal_verified', 'native_terminal_outcome',
            'completion_evidence', 'output_sha256', 'recovery',
        )} for step in value.get('steps', [])],
        'recoveries': value.get('recoveries', []),
    }


def valid_run_id(value) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(char in '0123456789abcdef' for char in value)


def verify_failed_operation(value: dict, case: dict) -> dict:
    require(valid_run_id(value.get('run_id')), f"{case['name']}: durable operation has an invalid run ID")
    require(value.get('status') == 'failed' and value.get('error', {}).get('code') == case['expected_code'],
            f"{case['name']}: durable operation does not retain the expected failure")
    steps = value.get('steps', [])
    if case['expects_completed_native_step']:
        require(bool(steps) and all(step.get('status') == 'succeeded' and
                                   step.get('native_completion_verified') is True and
                                   step.get('completion_token') and step.get('completion_evidence')
                                   for step in steps),
                f"{case['name']}: native completion is not proven before semantic rejection")
    else:
        require(not steps, f"{case['name']}: a pre-dispatch failure unexpectedly ran native commands")
    return operation_summary(value)


def safe_response(tool, response) -> dict:
    """Keep machine verdicts without copying unrelated window titles or log text."""
    payload = response.structured_content
    text = response_text(response)
    result = {'is_error': response.is_error, 'has_structured_content': isinstance(payload, dict),
              'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'text_characters': len(text)}
    if isinstance(payload, dict):
        error = payload.get('error', {})
        result.update(ok=payload.get('ok'), run_id=payload.get('run_id') or error.get('details', {}).get('run_id'),
                      error_code=error.get('code'))
        if tool == 'prism_operation_status':
            result['operation'] = operation_summary(payload)
        elif tool == 'prism_recover':
            result['recovery'] = {key: payload.get(key) for key in (
                'status', 'quarantine_cleared', 'quarantined', 'native_outcome',
                'semantic_completion_verified', 'recovery_id', 'record_path', 'run_id',
            )}
    return result


class EvidenceClient:
    def __init__(self, client, evidence: dict, directory: Path):
        self.client, self.evidence, self.directory = client, evidence, directory

    def persist(self):
        atomic_json(self.directory / 'verification.json', self.evidence)

    async def call(self, name: str, arguments: dict):
        record = {'tool': name, 'arguments': deepcopy(arguments), 'started_at': datetime.now(timezone.utc).isoformat()}
        self.evidence['mcp_calls'].append(record)
        self.persist()
        started = time.monotonic()
        try:
            response = await self.client.call_tool(name, arguments)
        except Exception as error:
            record.update(status='transport_error', error_type=type(error).__name__, elapsed_seconds=time.monotonic() - started)
            self.evidence['completion_uncertain'] = True
            self.persist()
            raise AcceptanceError(f'{name}: transport failed; an exception cannot count as an expected tool rejection') from None
        record.update(status='returned', elapsed_seconds=time.monotonic() - started, result=safe_response(name, response))
        if record['result'].get('error_code') in UNCERTAIN_CODES:
            self.evidence['completion_uncertain'] = True
        self.persist()
        return response

    async def success(self, name: str, arguments: dict) -> dict:
        response = await self.call(name, arguments)
        require(response.is_error is False and isinstance(response.structured_content, dict),
                f'{name}: expected a successful structured MCP result')
        require(response.structured_content.get('idempotent_replay') is not True,
                f'{name}: replay cannot establish fresh native execution')
        return response.structured_content


def run_ids(state: Path) -> set[str]:
    root = state / 'runs'
    return {path.name for path in root.iterdir()} if root.is_dir() else set()


def verify_protected(fixtures: dict) -> None:
    require(snapshot(list(map(Path, fixtures['protected_paths']))) == fixtures['protected_before'],
            'A rejected request changed a protected source or destination')


async def verify_precision(transport: EvidenceClient, fixtures: dict, operation: dict) -> dict:
    directory = Path(fixtures['state_directory']) / 'runs' / operation['run_id']
    working = directory / 'working.prism'
    require(working.is_file(), 'Precision rejection has no saved native checkpoint to inspect')
    require(sha256(directory / 'original.prism') == fixtures['template']['sha256'],
            'Precision experiment altered its untouched original checkpoint')
    document = await transport.success('prism_inspect', {'path': str(working), 'include_values': True})
    wanted = table_input(precise=True)
    require(len(document.get('tables', [])) == 1, 'Precision checkpoint has unexpected source tables')
    table = document['tables'][0]
    require(table['title'] == wanted['name'] and len(table['columns']) == 2,
            'Precision checkpoint does not contain the intended synthetic table')
    comparisons = []
    for column, expected in zip(table['columns'], wanted['columns']):
        raw, numeric = list(column['raw_values']), list(column['values'])
        while numeric and numeric[-1] is None and raw[-1] == '':
            numeric.pop()
            raw.pop()
        require(column['title'] == expected['label'] and len(raw) == len(numeric) == len(expected['values']),
                'Precision checkpoint changed group identity or observation count')
        for index, (token, actual, original) in enumerate(zip(raw, numeric, expected['values']), 1):
            require(type(actual) in (int, float) and math.isfinite(actual) and float(token) == actual,
                    'Precision checkpoint has inconsistent saved numeric tokens')
            comparisons.append({'group': expected['label'], 'row_1based': index, 'requested': original,
                                'saved_token': token, 'saved_numeric': actual, 'absolute_error': abs(actual - original)})
    maximum = max(item['absolute_error'] for item in comparisons)
    require(maximum > 0, 'Precision rejection did not demonstrate actual native rounding')
    details = operation['error']['details']
    require(details.get('allowed_error') == 0 and details.get('max_absolute_error') == maximum,
            'Precision error does not match the independently inspected native rounding')
    return {'checkpoint': str(working), 'sha256': document['sha256'], 'observations': comparisons,
            'max_absolute_error': maximum, 'allowed_error': 0, 'published_target_absent': True,
            'native_rounding_independently_verified': True}


def inject_diagnostic(state: Path, kind: str) -> dict:
    """Fixed backend-only injections; neither command can modify a document."""
    from prism_mcp.errors import PrismError
    from prism_mcp.runtime import Runtime, windows

    require(kind in {'bad-script', 'timeout'}, 'Unknown native diagnostic kind')
    runtime = Runtime(state)
    with runtime.locked():
        require(not any(window['modal'] for window in windows()), 'Native diagnostic requires no existing modal dialog')
        commands = [BAD_COMMAND] if kind == 'bad-script' else []
        operation = runtime.operation(f'failure_acceptance_{kind}', {'document_mutation_commands': False})
        try:
            operation.script(commands, timeout=8 if kind == 'bad-script' else 0)
        except PrismError as error:
            error.details.setdefault('run_id', operation.run_id)
            error.details.setdefault('checkpoint', str(operation.directory))
            operation.fail(error)
            return {'ok': False, 'error': error.as_dict()}
        except Exception as error:
            operation.fail(PrismError('DIAGNOSTIC_RUNNER_ERROR', 'Unexpected diagnostic runtime failure',
                                      cause=type(error).__name__, run_id=operation.run_id))
            raise
        operation.finish({'ok': True, 'run_id': operation.run_id})
        raise AcceptanceError(f'{kind}: intentionally failing backend diagnostic unexpectedly succeeded')


async def native_success(transport: EvidenceClient, tool: str, arguments: dict) -> dict:
    transport.evidence['completion_uncertain'] = True
    transport.persist()
    result = await transport.success(tool, arguments)
    run_id = result.get('run_id')
    require(valid_run_id(run_id), f'{tool}: native operation ID is missing')
    status = await transport.success('prism_operation_status', {'run_id': run_id})
    require(status.get('run_id') == run_id and status.get('status') == 'succeeded' and bool(status.get('steps')) and
            all(step.get('status') == 'succeeded' and step.get('native_completion_verified') is True and
                step.get('completion_token') and step.get('completion_evidence') for step in status['steps']),
            f'{tool}: successful native completion is unverified')
    transport.evidence['completion_uncertain'] = False
    transport.persist()
    return operation_summary(status)


async def diagnostics(transport: EvidenceClient, fixtures: dict, evidence: dict) -> None:
    state = Path(fixtures['state_directory'])
    record = evidence['backend_diagnostics']
    record.update(status='running', injection_transport='direct_backend_runtime',
                  observation_and_recovery_transport='real_mcp_stdio',
                  raw_script_mcp_tool_exposed=False, document_mutation_commands=False)
    transport.persist()
    for kind, code in [('bad-script', 'PRISM_SCRIPT_ERROR'), ('timeout', 'PRISM_TIMEOUT')]:
        print(json.dumps({'backend_diagnostic': kind, 'status': 'starting'}), flush=True)
        evidence['completion_uncertain'] = True
        transport.persist()
        payload = await asyncio.to_thread(inject_diagnostic, state, kind)
        record[kind] = {'status': 'returned', 'error_code': payload.get('error', {}).get('code'),
                        'run_id': payload.get('error', {}).get('details', {}).get('run_id')}
        transport.persist()
        require(payload.get('error', {}).get('code') == code, f'{kind}: backend diagnostic returned an unrelated failure')
        run_id = payload['error']['details']['run_id']
        require(valid_run_id(run_id), f'{kind}: backend diagnostic has an invalid run ID')
        status = await transport.success('prism_operation_status', {'run_id': run_id})
        require(status.get('run_id') == run_id and status.get('status') == 'failed' and
                status.get('error', {}).get('code') == code and len(status['steps']) == 1,
                f'{kind}: durable failed diagnostic does not match its injection')
        step = status['steps'][0]
        require(step.get('status') == 'failed', f'{kind}: native failure step is not recorded')
        record[kind] = operation_summary(status)
        verify_protected(fixtures)
        if kind == 'bad-script':
            require(step.get('native_terminal_verified') is True and step.get('native_terminal_outcome') == 'failed',
                    'Bad-script diagnostic lacks terminal native error evidence')
            recovered = await transport.success('prism_recover', {})
            require(recovered.get('status') == 'not_needed' and recovered.get('quarantine_cleared') is False,
                    'A verified terminal native script error left unexpected quarantine')
            evidence['completion_uncertain'] = False
            record[kind]['quarantine_not_needed'] = True
        else:
            require(step.get('native_terminal_verified') is False,
                    'Timeout injection did not establish uncertain completion')
            before_runs = run_ids(state)
            response = await transport.call('prism_open_document', {
                'path': fixtures['seed'], 'request_id': f"{evidence['acceptance_id']}:quarantine-refusal",
            })
            verdict = verify_error(response, {'name': 'quarantine-refusal', 'tool': 'prism_open_document',
                                             'validation_fields': [], 'expected_code': 'RECOVERY_REQUIRED'})
            require(run_ids(state) == before_runs, 'Quarantine refusal incorrectly created a new native operation')
            record[kind]['mcp_quarantine_refusal'] = verdict
            # Recover only through the exposed tool and only from the runtime's
            # real native terminal evidence. Never fabricate or delete a gate.
            deadline = time.monotonic() + 30
            attempts = 0
            while True:
                attempts += 1
                response = await transport.call('prism_recover', {})
                if not response.is_error:
                    recovered = response.structured_content
                    require(isinstance(recovered, dict) and recovered.get('status') == 'quarantine_cleared' and
                            recovered.get('run_id') == run_id and recovered.get('quarantine_cleared') is True and
                            recovered.get('semantic_completion_verified') is False and recovered.get('native_outcome') == 'completed',
                            'Recovery did not verify termination of the exact timeout diagnostic')
                    break
                verify_error(response, {'name': 'pending-timeout-recovery', 'tool': 'prism_recover',
                                        'validation_fields': [], 'expected_code': 'RECOVERY_REQUIRED'})
                require(time.monotonic() < deadline, 'Timeout recovery remains uncertain; preserved quarantine requires inspection')
                await asyncio.sleep(min(2 ** min(attempts - 1, 2), max(0, deadline - time.monotonic())))
            status = await transport.success('prism_operation_status', {'run_id': run_id})
            require(status.get('run_id') == run_id and status.get('status') == 'failed' and
                    status.get('error', {}).get('code') == 'PRISM_TIMEOUT' and
                    status.get('recoveries') and status['recoveries'][-1].get('terminal_evidence_verified') is True and
                    status['recoveries'][-1].get('semantic_completion_verified') is False,
                    'Recovery incorrectly promoted an unverified failed operation into success')
            evidence['completion_uncertain'] = False
            record[kind].update(recovery_attempts=attempts, recovered_operation=operation_summary(status),
                                recovery_id=recovered['recovery_id'])
        transport.persist()
    # Prove the cleared gate allows a new MCP native operation on the disposable
    # seed, then close that exact source without saving or changing observations.
    record['post_recovery_open'] = await native_success(transport, 'prism_open_document', {
        'path': fixtures['seed'], 'request_id': f"{evidence['acceptance_id']}:post-recovery-open",
    })
    record['post_recovery_close'] = await native_success(transport, 'prism_close_document', {
        'path': fixtures['seed'], 'request_id': f"{evidence['acceptance_id']}:post-recovery-close",
    })
    verify_protected(fixtures)
    record['status'] = 'backend_injections_and_mcp_recovery_verified'
    transport.persist()


async def execute(args, fixtures: dict, evidence: dict) -> None:
    directory, project = Path(fixtures['directory']), Path(fixtures['project'])
    state = Path(fixtures['state_directory'])
    launcher = project / 'bin/prism-mcp'
    require(launcher.is_file() and os.access(launcher, os.X_OK), 'Absolute MCP launcher is unavailable')
    require(importlib.metadata.version('mcp') == '2.1.1', 'Failure runner requires official MCP SDK 2.1.1')
    evidence['server_launcher'] = {'path': str(launcher), 'sha256': sha256(launcher)}
    environment = dict(os.environ)
    environment['PRISM_MCP_STATE_DIR'] = str(state)
    parameters = StdioServerParameters(command=str(launcher), cwd=str(project), env=environment)
    async with Client(parameters, read_timeout_seconds=args.timeout_seconds) as client:
        transport = EvidenceClient(client, evidence, directory)
        discovered = await client.list_tools()
        evidence['discovered_tools'] = sorted(tool.name for tool in discovered.tools)
        require(REQUIRED_TOOLS <= set(evidence['discovered_tools']), 'MCP discovery lacks required failure-acceptance tools')
        capabilities = await transport.success('prism_capabilities', {})
        require(capabilities.get('connected', {}).get('tool_invocation_verified') is True and
                capabilities.get('control', {}).get('accessible') is True,
                'Real MCP invocation or native application control is unavailable')
        require(capabilities.get('state_directory') == str(state), 'MCP did not use the isolated acceptance state directory')
        require(all(capabilities.get('template', {}).get(key) == value for key, value in fixtures['template'].items()),
                'MCP template does not match the prepared synthetic fixture')
        require(not any(window.get('modal') for window in capabilities['control'].get('windows', [])),
                'Failure acceptance requires no existing native modal dialog')
        document = await transport.success('prism_inspect', {'path': fixtures['seed'], 'include_values': True})
        require(document.get('sha256') == fixtures['template']['sha256'] and document.get('relationships_valid') is True,
                'MCP inspection does not match the prepared native seed')
        table = await transport.success('prism_read_table', {'path': fixtures['seed'], 'table_id': fixtures['table_id']})
        require(table == document['tables'][0], 'Successful UUID retrieval disagrees with MCP inspection')
        evidence['positive_read_controls_passed'] = True
        for case in cases(fixtures, evidence['acceptance_id']):
            record = {'name': case['name'], 'status': 'running', 'expected_code': case['expected_code'],
                      'expects_completed_native_step': case['expects_completed_native_step']}
            evidence['cases'].append(record)
            before_runs = run_ids(state)
            if case['expects_completed_native_step']:
                evidence['native_execution_attempted'] = True
                evidence['completion_uncertain'] = True
            transport.persist()
            response = await transport.call(case['tool'], case['arguments'])
            verdict = verify_error(response, case)
            run_id = verdict.get('run_id')
            require(run_id is None or valid_run_id(run_id), f"{case['name']}: invalid operation ID in backend error")
            require(run_ids(state) == before_runs | ({run_id} if run_id else set()),
                    f"{case['name']}: operation records disagree with the returned error")
            if run_id:
                require(run_id not in before_runs, f"{case['name']}: a prior operation cannot count as fresh execution")
                status = await transport.success('prism_operation_status', {'run_id': run_id})
                require(status.get('run_id') == run_id, f"{case['name']}: operation status returned another run")
                record['operation'] = verify_failed_operation(status, case)
                if case['expects_completed_native_step']:
                    evidence['native_execution_performed'] = True
                    evidence['completion_uncertain'] = False
                    record['precision'] = await verify_precision(transport, fixtures, status)
            else:
                require(not case['expects_completed_native_step'], 'Native precision error has no operation checkpoint')
            verify_protected(fixtures)
            record.update(status='passed', protected_paths_unchanged=True, **verdict)
            transport.persist()
            print(json.dumps({'case': case['name'], 'status': 'passed', 'error_surface': verdict['error_surface'],
                              'error_code': verdict['error_code']}), flush=True)
        if args.include_backend_diagnostics:
            await diagnostics(transport, fixtures, evidence)
        require(evidence['completion_uncertain'] is False, 'Native completion remains uncertain after acceptance')


async def run(args) -> int:
    fixtures = prepare(args.directory, args.project)
    directory = Path(fixtures['directory'])
    evidence = {
        'schema_version': 'prism-mcp-failure-acceptance/1', 'acceptance_id': uuid.uuid4().hex,
        'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'prepared',
        'runner_sha256': sha256(Path(__file__).resolve()), 'mcp_sdk': importlib.metadata.version('mcp'),
        'synthetic_only': True, 'mcp_calls': [], 'cases': [],
        'native_execution_attempted': False, 'native_execution_performed': False,
        'completion_uncertain': False, 'positive_read_controls_passed': False,
        'backend_diagnostics': {'status': 'not_requested' if not args.include_backend_diagnostics else 'not_attempted',
                                'raw_script_mcp_tool_exposed': False},
        'limitations': [
            'No raw-script MCP tool is exposed; optional native error injections use the backend runtime.',
            'Forced timeout uses an empty script and a zero-second observation deadline; it does not simulate a hung engine.',
            'No dirty user document, application crash, or forced Prism termination is introduced.',
            'These synthetic failures do not establish scientific or appearance acceptance.',
        ],
    }
    atomic_json(directory / 'planned-cases.json', cases(fixtures, evidence['acceptance_id']))
    atomic_json(directory / 'verification.json', evidence)
    code = 1
    try:
        if args.prepare_only:
            evidence['status'] = 'prepared_only'
        else:
            await execute(args, fixtures, evidence)
            evidence['status'] = ('mcp_failures_and_backend_diagnostic_recovery_passed'
                                  if args.include_backend_diagnostics else 'mcp_failure_checks_passed')
        code = 0
    except Exception as error:
        evidence.update(status='failed', failure=failure_record(error))
    finally:
        try:
            verify_protected(fixtures)
            evidence['protected_paths_unchanged'] = True
        except Exception as error:
            evidence.update(status='failed', protected_paths_unchanged=False, integrity_failure=failure_record(error))
            code = 1
        evidence['finished_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(directory / 'verification.json', evidence)
    print(json.dumps({'status': evidence['status'], 'passed_cases': sum(item['status'] == 'passed' for item in evidence['cases']),
                      'native_execution_performed': evidence['native_execution_performed'],
                      'backend_diagnostics': evidence['backend_diagnostics']['status'],
                      'evidence': str(directory / 'verification.json')}), flush=True)
    if 'failure' in evidence:
        print(json.dumps(evidence['failure']), file=sys.stderr, flush=True)
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='Fresh ignored local/ or artifacts/ output directory')
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--prepare-only', action='store_true', help='Create synthetic fixtures; do not connect to MCP or Prism')
    parser.add_argument('--include-backend-diagnostics', action='store_true',
                        help='Also inject fixed document-free native failures via backend and verify recovery through MCP')
    parser.add_argument('--timeout-seconds', type=float, default=180,
                        help='MCP request timeout; does not cancel native execution')
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout_seconds) or not 10 <= args.timeout_seconds <= 600:
        parser.error('--timeout-seconds must be between 10 and 600')
    try:
        return asyncio.run(run(args))
    except (AcceptanceError, OSError, ValueError) as error:
        print(json.dumps({'status': 'failed', 'failure': failure_record(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
