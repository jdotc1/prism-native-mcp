"""Synthetic 95%/10-digit -> 90%/6-digit -> restored analysis acceptance.

Only the parent may run live MCP/Prism. --prepare-only never starts a client.
Native result-view CSV bytes are extracted read-only from the saved archive;
this is not a native CSV export command or an on-screen rendering check.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import csv
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import uuid
import zipfile

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from scipy.stats import t

from prism_mcp.analysis_settings import DescriptiveSettings
from prism_mcp.models import TableInput


REQUIRED_TOOLS = {'prism_capabilities', 'prism_create_document', 'prism_inspect', 'prism_read_analysis',
                  'prism_configure_analysis', 'prism_operation_status', 'prism_save_document',
                  'prism_open_document', 'prism_close_document', 'prism_export_table'}
SETTINGS = {
    'baseline': {'mean_ci': True, 'sum': True, 'coefficient_of_variation': True,
                 'confidence_level': 95, 'significant_digit_count': 10},
    'changed': {'mean_ci': True, 'sum': True, 'coefficient_of_variation': True,
                'confidence_level': 90, 'significant_digit_count': 6},
}
RESULT_ROWS = {'Mean': 'mean', 'Std. Deviation': 'sd', 'Std. Error of Mean': 'meanSE'}
ABS_TOL, REL_TOL = 2e-10, 2e-11


class AcceptanceError(RuntimeError):
    """Only messages written by this runner are emitted as diagnostics."""


def require(condition, message):
    if not condition:
        raise AcceptanceError(message)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def failure(error):
    if isinstance(error, BaseExceptionGroup):
        return {'type': type(error).__name__, 'causes': [failure(item) for item in error.exceptions]}
    return {'type': type(error).__name__, 'message': str(error) if isinstance(error, AcceptanceError)
            else 'Inspect the retained operation evidence; no unverified native action was retried'}


def table_input():
    # Invented unequal-length samples, all exact binary fractions. No clinical
    # input, density coordinates, or intentionally rounded observations appear.
    return TableInput.model_validate({'name': 'Synthetic CI differential', 'units': 'arbitrary units',
        'measurement_resolution': None, 'columns': [
            {'label': 'Synthetic A', 'values': [2.25, 3.5, 5.125, 7.75, 11.875]},
            {'label': 'Synthetic B', 'values': [1.0625, 2.3125, 3.6875, 6.4375, 9.5625, 14.1875]},
        ]}).model_dump()


def independent_statistics(values, confidence):
    require(len(values) >= 3 and confidence in (90, 95), 'Unexpected synthetic CI definition')
    n, mean, sd = len(values), statistics.mean(values), statistics.stdev(values)
    sem = sd / math.sqrt(n)
    critical = float(t.ppf((1 + confidence / 100) / 2, n - 1))
    return {'n': n, 'mean': mean, 'sd': sd, 'meanSE': sem, 'sum': math.fsum(values),
            'median': statistics.median(values), 'minimum': min(values), 'maximum': max(values),
            'coeffOfVariation': 100 * sd / mean, 'lower': mean - critical * sem,
            'upper': mean + critical * sem, 'level': confidence, 't_critical': critical, 'df': n - 1}


def typed(value):
    """Compare JSON scalars without treating Booleans as numeric values."""
    if isinstance(value, dict):
        return ('object', tuple((key, typed(item)) for key, item in sorted(value.items())))
    if isinstance(value, list):
        return ('array', tuple(map(typed, value)))
    if type(value) in (int, float, Decimal):
        number = Decimal(str(value))
        require(number.is_finite(), 'Nonfinite saved JSON number')
        return ('number', number)
    return (type(value).__name__, value)


def parameter_settings(parameters):
    require(parameters.get('analysisClass') == 'DESCRIPTIVE_STATISTICS' and
            parameters.get('$id') == 'analyses/descriptive_statistics/parameters.schema.json',
            'Unexpected saved descriptive parameter schema')
    content = parameters['content']
    flags, options, output = content['calc'], content['options'], content['output']
    require(content['data']['handleReplicatesAs'] == 'AVERAGE' and
            all(flags.get(name) is True for name in ('mean', 'median', 'minMaxRange')),
            'Required native descriptive settings changed')
    result = {'mean_ci': flags['meanCI'], 'sum': flags['sum'], 'coefficient_of_variation': flags['coefficientOfVariation'],
              'confidence_level': options['confidenceLevel'], 'significant_digit_count': output['significantDigitCount']}
    require(all(type(result[name]) is bool for name in ('mean_ci', 'sum', 'coefficient_of_variation')) and
            type(result['confidence_level']) in (int, float, Decimal) and
            type(result['significant_digit_count']) is int, 'Saved descriptive setting has the wrong scalar type')
    return result


def retained_parameters(parameters):
    result = deepcopy(parameters)
    for group, keys in {'calc': ('meanCI', 'sum', 'coefficientOfVariation'),
                        'options': ('confidenceLevel',), 'output': ('significantDigitCount',)}.items():
        for key in keys:
            del result['content'][group][key]
    return typed(result)


def preserved_state(document):
    keys = ('id', 'index', 'title', 'analysis_class', 'input_dataset_ids', 'input_sheet_ids')
    return {'tables': document['tables'], 'graphs': document['graphs'], 'layouts': document['layouts'],
            'analyses': [{key: analysis[key] for key in keys} for analysis in document['analyses']]}


def verify_observations(document):
    require(document.get('format_version') == '1-6-1' and document.get('relationships_valid') is True and
            len(document['tables']) == len(document['graphs']) == len(document['analyses']) == 1 and
            document['layouts'] == [], 'Expected one intact synthetic native family without a layout')
    source, table = table_input(), document['tables'][0]
    require(table['title'] == source['name'] and table['dataset_ids'] == [column['id'] for column in table['columns']] and
            len(set(table['dataset_ids'])) == len(table['columns']) == 2, 'Synthetic source labels or dataset identities differ')
    for column, expected in zip(table['columns'], source['columns']):
        require(column['title'] == expected['label'], 'Synthetic group order differs')
        raw, values = list(column['raw_values']), list(column['values'])
        require(len(raw) == len(values), 'Saved observation representations have different lengths')
        while raw and raw[-1] == '' and values[-1] is None:
            raw.pop()
            values.pop()
        require(values == expected['values'] and all(type(value) in (int, float) for value in values) and
                [Decimal(token) for token in raw] == [Decimal(str(value)) for value in expected['values']],
                'An original synthetic observation changed or lost precision')
    analysis = document['analyses'][0]
    require(document['graphs'][0]['input_dataset_ids'] == analysis['input_dataset_ids'] == table['dataset_ids'] and
            analysis['input_sheet_ids'] == [table['id']] and analysis['analysis_class'] == 'DESCRIPTIVE_STATISTICS',
            'Native graph/analysis does not bind the ordered original observations')
    return analysis


def verify_analysis(document, analysis, settings, *, original=None):
    expected_analysis = verify_observations(document)
    require(typed(analysis) == typed(expected_analysis), 'Read-analysis response differs from the saved document inspection')
    if original is not None:
        require(typed(preserved_state(document)) == typed(preserved_state(original)), 'Native observations or object IDs/bindings changed')
        require(retained_parameters(analysis['parameters']) == retained_parameters(original['analyses'][0]['parameters']),
                'An unrequested native parameter changed')
    require(typed(parameter_settings(analysis['parameters'])) == typed(settings), 'Saved native parameters do not match the requested settings')
    require(analysis['results'].get('content', {}).get('status', {}).get('error') == 'VALID', 'Native analysis results are not valid')
    summaries = analysis['summaries']
    require(len(summaries) == 2 and {item['dataset_id'] for item in summaries} == set(analysis['input_dataset_ids']),
            'Native summary coverage is incomplete')
    saved_samples = [{'dataset_id': item['id']['col']['uid'], **item['value']}
                     for item in analysis['results']['content']['samples'].values()]
    require(len(saved_samples) == 2 and typed(sorted(saved_samples, key=lambda item: item['dataset_id'])) ==
            typed(sorted(summaries, key=lambda item: item['dataset_id'])), 'Extracted summaries disagree with native saved result samples')
    checks = []
    for column, dataset_id in zip(table_input()['columns'], analysis['input_dataset_ids']):
        summary = next(item for item in summaries if item['dataset_id'] == dataset_id)
        expected = independent_statistics(column['values'], settings['confidence_level'])
        require(type(summary.get('n')) is int and summary['n'] == expected['n'], 'Native sample count differs')
        ci = summary.get('meanCI', {})
        require(type(ci.get('level')) in (int, float) and ci['level'] == settings['confidence_level'], 'Native CI level is stale')
        errors = {}
        for field in ('mean', 'sd', 'meanSE', 'sum', 'median', 'minimum', 'maximum', 'coeffOfVariation', 'lower', 'upper'):
            actual = ci.get(field) if field in ('lower', 'upper') else summary.get(field)
            require(type(actual) in (int, float) and math.isfinite(actual) and
                    math.isclose(actual, expected[field], abs_tol=ABS_TOL, rel_tol=REL_TOL),
                    f'Native {field} differs from the independent original-observation calculation')
            errors[field] = actual - expected[field]
        checks.append({'dataset_id': dataset_id, 'group': column['label'], 'expected': expected,
                       'native': {key: summary[key] for key in ('n', *RESULT_ROWS.values())} | {'lower': ci['lower'], 'upper': ci['upper']},
                       'numeric_errors': errors})
    return checks


def canonical_uuid(value):
    require(isinstance(value, str) and str(uuid.UUID(value)) == value.lower(), 'Unexpected native result-view UUID')
    return value


def saved_result_view(path, analysis):
    """Follow native result-view links and return the original CSV bytes."""
    analysis_id = canonical_uuid(analysis['id'])
    with zipfile.ZipFile(path) as archive:
        def read(name):
            return json.loads(archive.read(name), parse_float=Decimal)
        for name in ('parameters', 'results'):
            require(typed(read(f'analyses/{analysis_id}/{name}.json')) == typed(analysis[name]),
                    f'MCP {name} differ from the saved native archive')
        sheet = read(f'analyses/{analysis_id}/sheet.json')
        view_id = canonical_uuid(sheet['currentResultSheet'])
        require(view_id in [item['uid'] for item in sheet['resultSheets']], 'Native current result view is not linked to the analysis')
        view = read(f'analyses/{analysis_id}/result_sheets/{view_id}.json')
        require(view['uid'] == view_id and view.get('isVisible') is True and view.get('isInternal') is False,
                'Expected the visible native descriptive result view')
        data_id = canonical_uuid(view['dataSheet'])
        data = read(f'data/sheets/{data_id}/sheet.json')
        table = data['table']
        require(data['uid'] == data_id and 'ANALYSIS_VIEW' in data.get('flags', []) and
                table['format'] == 'column' and table['dataFormat'] == 'text', 'Native result view is not the observed text table')
        labels = [read(f'data/sets/{canonical_uuid(identity)}.json')['title'] for identity in table['dataSets']]
        require(len(labels) == len(set(labels)) == 2, 'Native result CSV needs two distinct group labels')
        member = f'data/tables/{canonical_uuid(table["uid"])}/data.csv'
        return archive.read(member), {'analysis_id': analysis_id, 'result_view_id': view_id, 'data_sheet_id': data_id,
                                      'table_id': table['uid'], 'archive_member': member, 'group_labels': labels}


def verify_result_text(payload, checks, significant_digits):
    """Check native saved Mean/SD/SEM strings, including significant trailing zeros."""
    rows = {}
    for row in csv.reader(io.StringIO(payload.decode('utf-8-sig'))):
        if not row or not any(row):
            continue
        require(len(row) == 3, 'Native result CSV has an unexpected number of columns')
        label = row[0].strip()
        require(label not in rows, 'Native result CSV has duplicate row labels')
        rows[label] = row[1:]
    require(all(label in rows for label in ('Number of values', *RESULT_ROWS)), 'Native result CSV omits a required descriptive row')
    require(rows['Number of values'] == [str(item['expected']['n']) for item in checks], 'Native result CSV counts are stale')
    tokens = []
    for label, field in RESULT_ROWS.items():
        for text, check in zip(rows[label], checks):
            require(re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', text) is not None,
                    'Native result CSV contains an unrecognized numeric text token')
            value, expected = Decimal(text), Decimal(str(check['expected'][field]))
            unit = Decimal(1).scaleb(expected.adjusted() - significant_digits + 1)
            require(value == expected.quantize(unit, rounding=ROUND_HALF_UP), 'Native result text does not round the independently calculated statistic')
            require(len(value.as_tuple().digits) == significant_digits, 'Native result text has the wrong significant-digit count')
            tokens.append({'group': check['group'], 'row': label, 'native_text': text, 'significant_digits': significant_digits})
    return tokens


def compare_intervals(baseline, changed):
    require([item['dataset_id'] for item in baseline] == [item['dataset_id'] for item in changed], 'Differential CI dataset order changed')
    for before, after in zip(baseline, changed):
        require(before['native']['lower'] < after['native']['lower'] < after['native']['mean'] <
                after['native']['upper'] < before['native']['upper'], 'The native 90% interval did not narrow inside the native 95% interval')
        require(all(before['native'][key] == after['native'][key] for key in ('n', *RESULT_ROWS.values())),
                'Changing CI level or display digits changed an underlying sample statistic')


class Recorder:
    def __init__(self, client, directory, evidence):
        self.client, self.directory, self.evidence = client, directory, evidence
        self.native_uncertain, self.run_ids = False, set()

    def persist(self):
        self.evidence['native_completion_uncertain'] = self.native_uncertain
        write_json(self.directory / 'verification.json', self.evidence)

    async def call(self, name, arguments, *, native=False):
        require(not native or not self.native_uncertain, 'An uncertain native operation blocks further automatic mutation')
        record = {'tool': name, 'arguments': arguments, 'native': native, 'status': 'requested'}
        self.evidence['mcp_calls'].append(record)
        if native:
            self.native_uncertain = self.evidence['native_execution_attempted'] = True
        self.persist()
        try:
            response = await self.client.call_tool(name, arguments)
            payload = response.structured_content
            require(isinstance(payload, dict), f'{name}: structured MCP result is missing')
            if response.is_error:
                record['error_code'] = payload.get('error', {}).get('code', 'MCP_TOOL_ERROR')
                raise AcceptanceError(f'{name}: MCP operation failed; inspect its retained checkpoint')
            texts = [item.text for item in response.content if getattr(item, 'type', None) == 'text']
            require(len(texts) == 1 and typed(json.loads(texts[0])) == typed(payload), f'{name}: MCP text and structured results disagree')
            if native:
                require(payload.get('idempotent_replay') is not True, f'{name}: a replay cannot count as fresh native execution')
                run_id = payload.get('run_id')
                require(isinstance(run_id, str) and re.fullmatch('[0-9a-f]{32}', run_id) is not None and run_id not in self.run_ids,
                        f'{name}: fresh native run ID is missing')
                record['run_id'] = run_id
                status = await self.call('prism_operation_status', {'run_id': run_id})
                require(status.get('run_id') == run_id and status.get('request_id') == arguments['request_id'] and
                        status.get('status') == 'succeeded' and status.get('steps') and
                        all(step.get('status') == 'succeeded' and step.get('completion_token') and
                            step.get('native_completion_verified') is True and
                            step.get('completion_evidence', {}).get('log_sha256') and
                            step.get('completion_evidence', {}).get('done_sha256') for step in status['steps']),
                        f'{name}: current native completion evidence is missing')
                record['native_steps'] = [{key: step.get(key) for key in ('number', 'completion_token', 'script_sha256',
                                           'completion_evidence', 'output_sha256')} for step in status['steps']]
                self.run_ids.add(run_id)
                self.native_uncertain = False
                self.evidence['native_execution_performed'] = True
                record['completion_verified'] = True
            record['status'] = 'succeeded'
            self.persist()
            print(json.dumps({'tool': name, 'status': 'succeeded', 'run_id': payload.get('run_id')}), flush=True)
            return payload
        except BaseException as error:
            record.update(status='failed', failure=failure(error))
            self.persist()
            raise


async def execute(args, directory, evidence):
    project = args.project.resolve()
    launcher = project / 'bin/prism-mcp'
    require(launcher.is_file() and os.access(launcher, os.X_OK), 'The absolute MCP launcher is unavailable')
    parameters = StdioServerParameters(command=str(launcher), cwd=str(project),
        env={**os.environ, 'PRISM_MCP_STATE_DIR': str(directory / 'server-state')})
    primary, copy = directory / 'baseline-95-10.prism', directory / 'differential-copy.prism'
    request = lambda label: evidence['acceptance_id'] + ':' + label
    evidence['server_launcher'] = {'path': str(launcher), 'sha256': sha256(launcher)}
    async with Client(parameters, read_timeout_seconds=args.timeout_seconds) as client:
        recorder = Recorder(client, directory, evidence)
        available = await client.list_tools()
        names = {tool.name for tool in available.tools}
        require(REQUIRED_TOOLS <= names, 'Required analysis differential MCP tools are unavailable')
        evidence['discovered_tools'] = sorted(names)
        capabilities = await recorder.call('prism_capabilities', {})
        require(capabilities.get('connected', {}).get('tool_invocation_verified') is True and
                capabilities.get('control', {}).get('accessible') is True, 'Live MCP/native control is unavailable')
        require(Path(capabilities['state_directory']).resolve() == directory / 'server-state',
                'The MCP server did not use the isolated experiment state directory')
        template = project / 'src/prism_mcp/templates/column_violin_summary.prism'
        require(Path(capabilities['template']['path']).resolve() == template.resolve() and
                capabilities['template']['sha256'] == sha256(template), 'The MCP server uses an unexpected native seed')
        evidence['protected_template'] = {'path': str(template), 'sha256': sha256(template)}
        recorder.persist()

        async def inspect(path):
            document = await recorder.call('prism_inspect', {'path': str(path), 'include_values': True})
            require(document['sha256'] == sha256(path), 'MCP inspection hash differs from the current saved file')
            verify_observations(document)
            return document

        async def stage(label, path, settings, original):
            document = await inspect(path)
            analysis = await recorder.call('prism_read_analysis', {'path': str(path), 'analysis_id': document['analyses'][0]['id']})
            write_json(directory / f'{label}-inspection.json', document)
            write_json(directory / f'{label}-analysis.json', analysis)
            result = {'status': 'captured_pending_checks', 'settings': settings, 'document_sha256': document['sha256']}
            evidence['stages'][label] = result
            recorder.persist()
            payload, view = saved_result_view(path, analysis)
            csv_path = directory / f'{label}-native-result-view.csv'
            with csv_path.open('xb') as stream:
                stream.write(payload)
            result['native_result_csv'] = {**view, 'path': str(csv_path), 'sha256': sha256(csv_path),
                'method': 'exact saved native result-view CSV bytes; read-only archive extraction, not an MCP CSV-export command'}
            recorder.persist()
            require(view['group_labels'] == [column['label'] for column in table_input()['columns']], 'Native result CSV group order differs')
            checks = verify_analysis(document, analysis, settings, original=original)
            tokens = verify_result_text(payload, checks, settings['significant_digit_count'])
            require(sha256(path) == document['sha256'], 'Reading native results changed the saved document')
            result.update(status='verified', checks=checks, saved_result_text=tokens)
            recorder.persist()
            return document, result

        async def configure(path, document, settings, label):
            result = await recorder.call('prism_configure_analysis', {'path': str(path), 'analysis_id': document['analyses'][0]['id'],
                'settings': DescriptiveSettings.model_validate(settings).model_dump(exclude_unset=True),
                'expected_sha256': document['sha256'], 'request_id': request(label)}, native=True)
            verification = result.get('native_settings_verification', {})
            require(result.get('configured_analysis_id') == document['analyses'][0]['id'] and
                    all(verification.get(key) is True for key in ('parameters_roundtrip_verified', 'original_observations_unchanged',
                                                                'object_identities_and_bindings_preserved')),
                    'Native configuration lacks parameter/source-preservation receipts')

        await recorder.call('prism_create_document', {'path': str(primary), 'tables': [table_input()], 'layout': False,
                            'request_id': request('create-synthetic')}, native=True)
        original = await inspect(primary)
        await configure(primary, original, SETTINGS['baseline'], 'configure-baseline')
        baseline_doc, baseline = await stage('baseline-95-10', primary, SETTINGS['baseline'], original)
        primary_hash = sha256(primary)
        evidence['protected_primary'] = {'path': str(primary), 'sha256': primary_hash}
        source_csv = directory / 'original-observations.csv'
        exported = await recorder.call('prism_export_table', {'path': str(primary), 'table_id': baseline_doc['tables'][0]['id'],
            'output_path': str(source_csv), 'request_id': request('export-original-observations')})
        require(exported.get('adapter') == 'exact_saved_native_csv' and exported['source_sha256'] == primary_hash and
                exported['sha256'] == sha256(source_csv) and exported['path'] == str(source_csv) and
                exported['table_id'] == baseline_doc['tables'][0]['id'], 'MCP observation CSV export is not verified')
        with zipfile.ZipFile(primary) as archive:
            require(source_csv.read_bytes() == archive.read(f'data/tables/{baseline_doc["tables"][0]["table_id"]}/data.csv'),
                    'MCP observation CSV differs from the saved source bytes')
        evidence['protected_observation_csv'] = {'path': str(source_csv), 'sha256': sha256(source_csv)}
        recorder.persist()
        saved = await recorder.call('prism_save_document', {'path': str(primary), 'save_as': str(copy),
            'request_id': request('save-as-verification-copy')}, native=True)
        require(saved.get('source_preserved') is True and saved.get('save_as_source_sha256') == primary_hash and
                sha256(primary) == primary_hash, 'Native Save As changed the protected baseline')
        saved_copy_state = await recorder.call('prism_save_document', {'path': str(copy),
            'request_id': request('save-opened-verification-copy')}, native=True)
        require(saved_copy_state.get('saved_current_native_state') is True,
                'The opened verification copy state was not saved')
        await recorder.call('prism_close_document', {'path': str(copy), 'request_id': request('close-verification-copy')}, native=True)
        copy_doc, copy_before = await stage('copy-before-95-10', copy, SETTINGS['baseline'], baseline_doc)
        require(copy_before['checks'] == baseline['checks'] and copy_before['native_result_csv']['sha256'] == baseline['native_result_csv']['sha256'],
                'Native Save As changed the baseline analysis or result text')
        changed = False
        try:
            # A native success followed by a receipt/check failure still needs
            # restoration. Uncertain dispatch remains blocked by the recorder.
            changed = True
            await configure(copy, copy_doc, SETTINGS['changed'], 'configure-90-6')
            _, differential = await stage('changed-90-6', copy, SETTINGS['changed'], baseline_doc)
            compare_intervals(baseline['checks'], differential['checks'])
            require(differential['saved_result_text'] != baseline['saved_result_text'], 'Native significant-digit text did not change')
            await recorder.call('prism_open_document', {'path': str(copy), 'request_id': request('reopen-changed')}, native=True)
            reread = await recorder.call('prism_read_analysis', {'path': str(copy), 'analysis_id': baseline_doc['analyses'][0]['id']})
            require(typed(parameter_settings(reread['parameters'])) == typed(SETTINGS['changed']), 'Reopened analysis did not retain the changed parameters')
            saved = await recorder.call('prism_save_document', {'path': str(copy), 'request_id': request('save-reopened')}, native=True)
            require(saved.get('saved_current_native_state') is True, 'Reopened native document state was not explicitly saved')
            await recorder.call('prism_close_document', {'path': str(copy), 'request_id': request('close-reopened')}, native=True)
            _, reopened = await stage('changed-reopened-90-6', copy, SETTINGS['changed'], baseline_doc)
            require(reopened['checks'] == differential['checks'] and reopened['native_result_csv']['sha256'] == differential['native_result_csv']['sha256'],
                    'Reopening and saving changed the native 90% CI or six-digit result text')
        finally:
            if changed and not recorder.native_uncertain:
                evidence['restoration'] = {'status': 'attempting'}
                recorder.persist()
                try:
                    await configure(copy, await inspect(copy), SETTINGS['baseline'], 'restore-95-10')
                    _, restored = await stage('restored-95-10', copy, SETTINGS['baseline'], baseline_doc)
                    require(restored['checks'] == baseline['checks'] and restored['native_result_csv']['sha256'] == baseline['native_result_csv']['sha256'],
                            'Native original settings, statistics, or result text did not restore')
                    evidence['restoration'] = {'status': 'verified', 'path': str(copy)}
                except BaseException:
                    evidence['restoration'] = {'status': 'recovery_required' if recorder.native_uncertain else 'failed', 'path': str(copy)}
                    raise
                finally:
                    recorder.persist()
            elif recorder.native_uncertain:
                evidence['restoration'] = {'status': 'recovery_required', 'path': str(copy), 'automatic_retry_dispatched': False}
                recorder.persist()
        require(sha256(primary) == primary_hash and evidence['restoration']['status'] == 'verified', 'Protected baseline or restoration verification failed')
        comparison = directory / 'independent-ci-comparison.csv'
        with comparison.open('x', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['stage', 'group', 'n', 'confidence', 'native_mean', 'native_lower', 'native_upper', 'independent_lower', 'independent_upper'])
            for label, result in evidence['stages'].items():
                for item in result['checks']:
                    writer.writerow([label, item['group'], item['expected']['n'], result['settings']['confidence_level'], item['native']['mean'], item['native']['lower'],
                        item['native']['upper'], item['expected']['lower'], item['expected']['upper']])
        evidence.update(status='native_ci_and_saved_result_text_checks_passed', independent_ci_endpoints_verified=True,
            saved_result_text_significant_digits_verified=True, on_screen_rendering_verified=False,
            comparison_csv={'path': str(comparison), 'sha256': sha256(comparison), 'semantics': 'local comparison of native saved results and independent calculations'},
            limitation='Saved native result-table text was verified; on-screen presentation was not inspected by this runner')
        recorder.persist()


async def run(args):
    directory, project = args.directory.expanduser().resolve(), args.project.expanduser().resolve()
    require(not directory.exists(), 'Analysis differential acceptance requires a fresh output directory')
    require(any((project / name).resolve() in directory.parents for name in ('local', 'artifacts')),
            'Acceptance outputs must be under the selected project local/ or artifacts/ directory')
    args.project = project
    require(importlib.metadata.version('mcp') == '2.1.1', 'This runner requires the tested MCP SDK 2.1.1')
    directory.mkdir(parents=True, exist_ok=False)
    evidence = {'schema_version': 'native-analysis-differential/1', 'acceptance_id': uuid.uuid4().hex,
        'status': 'prepared_only' if args.prepare_only else 'running', 'synthetic_only': True,
        'started_at': datetime.now(timezone.utc).isoformat(), 'mcp_calls': [], 'stages': {},
        'native_execution_attempted': False, 'native_execution_performed': False, 'native_completion_uncertain': False,
        'independent_ci_endpoints_verified': False, 'saved_result_text_significant_digits_verified': False,
        'on_screen_rendering_verified': False, 'software': {'mcp': importlib.metadata.version('mcp'),
            'scipy': importlib.metadata.version('scipy'), 'runner_sha256': sha256(Path(__file__))}}
    code = 0
    try:
        write_json(directory / 'synthetic-plan.json', {'table': table_input(), 'settings': SETTINGS,
            'independent_targets': {name: [independent_statistics(column['values'], settings['confidence_level'])
                for column in table_input()['columns']] for name, settings in SETTINGS.items()},
            'restoration': 'Native Save As copy returns to 95% and ten digits; primary baseline remains unchanged'})
        write_json(directory / 'verification.json', evidence)
        if not args.prepare_only:
            await execute(args, directory, evidence)
    except BaseException as error:
        evidence.update(status='failed', failure=failure(error))
        code = 1
    finally:
        for protected in (evidence[key] for key in ('protected_primary', 'protected_template', 'protected_observation_csv') if key in evidence):
            try:
                protected['sha256_after'] = sha256(protected['path'])
            except OSError:
                protected['sha256_after'] = None
            protected['unchanged'] = protected['sha256_after'] == protected['sha256']
            if not protected['unchanged']:
                evidence.update(status='failed', source_integrity_failure=True)
                code = 1
        evidence['finished_at'] = datetime.now(timezone.utc).isoformat()
        write_json(directory / 'verification.json', evidence)
    print(json.dumps({'status': evidence['status'], 'native_execution_performed': evidence['native_execution_performed'],
                      'evidence': str(directory / 'verification.json')}), flush=True)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--timeout-seconds', type=float, default=180)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout_seconds) or not 10 <= args.timeout_seconds <= 600:
        parser.error('--timeout-seconds must be between 10 and 600')
    try:
        return asyncio.run(run(args))
    except (AcceptanceError, OSError):
        print(json.dumps({'status': 'failed', 'message': 'A fresh permitted output directory or required local dependency is unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
