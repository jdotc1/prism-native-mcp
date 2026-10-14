"""Semantic native operations, separated from MCP and dataset importers."""
from __future__ import annotations

import csv
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import shutil
import statistics
import time
import zipfile

from pydantic import ValidationError

from .analysis_settings import ADAPTER_STATUS, ANALYSIS_CLASS, DescriptiveSettings, write_descriptive_settings
from .archive import read_project
from .errors import PrismError
from .models import (AnnotationInput, GraphFormat, LayoutInput, NativeGraphCreation,
                     NativeGraphPlacement, NativeGraphStyle, NativeLayoutAlignment,
                     NativeOverlayComposition, NativeOverlayPanel, TableInput)
from .runtime import Runtime, application_info, digest, quote, windows, write_json, decode_log

TEMPLATE = Path(__file__).parent / 'templates/column_violin_summary.prism'


def project(path: str | Path, *, include_values=True):
    try:
        return read_project(Path(path).expanduser().resolve(), include_values=include_values)
    except (ValueError, OSError) as exc:
        raise PrismError('INVALID_DOCUMENT', str(exc)) from exc


def path_arg(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise PrismError('INVALID_PATH', 'An absolute filesystem path is required.')
    quote(p)
    return p.resolve()


def lookup(doc: dict, collection: str, identity: str) -> dict:
    rows = [x for x in doc[collection] if x['id'] == identity]
    if len(rows) != 1:
        raise PrismError('OBJECT_NOT_FOUND', f'No unique {collection} object has the specified UUID.', id=identity)
    return rows[0]


def _native_graph_adapter(name: str):
    """Keep transport/inspection usable while experimental UI adapters are absent."""
    from importlib import import_module
    try:
        adapter = getattr(import_module('.native_style', __package__), name)
    except (ImportError, AttributeError) as exc:
        raise PrismError('NATIVE_ADAPTER_UNAVAILABLE', 'The requested native graph adapter is unavailable.', adapter=name) from exc
    if not callable(adapter):
        raise PrismError('NATIVE_ADAPTER_UNAVAILABLE', 'The requested native graph adapter is not callable.', adapter=name)
    return adapter


def _analysis_parameter_signature(path: Path, analysis_id: str):
    """Compare JSON semantics without float rounding or Boolean/number coercion."""
    with zipfile.ZipFile(path) as archive:
        value = json.loads(archive.read(f'analyses/{analysis_id}/parameters.json'), parse_float=Decimal)

    def signature(item):
        if isinstance(item, dict):
            return 'object', tuple((key, signature(value)) for key, value in sorted(item.items()))
        if isinstance(item, list):
            return 'array', tuple(signature(value) for value in item)
        if type(item) in (int, Decimal):
            return 'number', Decimal(item)
        return type(item).__name__, item

    return signature(value)


class Backend:
    def __init__(self, state_dir=None):
        self.runtime = Runtime(state_dir)

    def capabilities(self):
        app = application_info()
        try:
            ws = windows()
            control = {'accessible': True, 'windows': ws}
        except PrismError as exc:
            control = {'accessible': False, 'error': exc.as_dict()}
        live = self.runtime.state / 'last_verified.json'
        schemes = []
        plist = Path(app['path']) / 'Contents/Resources/ColorSchemes/ColorSchemeIdentifiers.plist'
        if plist.exists():
            names = plistlib.loads(plist.read_bytes())
            schemes = [x['FileName'].lstrip('*') for x in sorted(names, key=lambda x: x.get('Order', 0))] if isinstance(names, list) else list(names)
        return {'application': app, 'control': control, 'transport': 'stdio', 'sdk': 'mcp==2.1.1',
                'configured': self._configured(), 'connected': None,
                'live_verified': json.loads(live.read_text()) if live.exists() else None,
                'template': {'path': str(TEMPLATE), 'sha256': digest(TEMPLATE),
                             'kind': 'Prism-created native two-column violin and descriptive analysis'},
                'supported': {'document_format': '1-6-1', 'graph_types': ['column_violin_with_points', 'column_median_iqr'],
                              'analyses': ['DESCRIPTIVE_STATISTICS'], 'group_count': 2,
                              'layout': 'native grid and two-panel violin/median-IQR overlays with linked graphs',
                              'annotations': 'editable native static text; labels require separate updates after data changes',
                              'graph_formatting': ['title', 'axis_titles', 'y_limits', 'y_interval', 'color_scheme'],
                              'export': ['pdf', 'svg', 'png'],
                              'cancellation': 'before native dispatch; timeout quarantines subsequent mutations'},
                'experimental': {'analysis_configuration': {
                    'tool': 'prism_configure_analysis', 'analysis_class': ANALYSIS_CLASS,
                    'status': 'native_differential_verified_on_pinned_build', 'offline_adapter_status': ADAPTER_STATUS,
                    'settings': {'mean_ci': 'boolean', 'sum': 'boolean', 'coefficient_of_variation': 'boolean',
                                 'confidence_level': [90, 95], 'significant_digit_count': [6, 10]},
                    'limits': ['Only the observed descriptive parameter schema is accepted.',
                               'Native acceptance verified confidence 90/95 and saved result text at 6/10 significant digits.',
                               'Routine calls check CI structure and symmetry; independent exact endpoints and displayed formatting are separate acceptance checks.']},
                    'native_graph_controls': {
                        'status': 'partially_native_verified_on_pinned_build',
                        'tools': ['prism_format_native_graph', 'prism_create_graph', 'prism_place_graph', 'prism_align_layout'],
                        'verified_mcp_tools': ['prism_format_native_graph', 'prism_create_graph'],
                        'component_verified_tools_requiring_standalone_acceptance': ['prism_place_graph', 'prism_align_layout'],
                        'creation_kinds': ['median_iqr'], 'style_kinds': ['violin', 'median_iqr'],
                        'limits': ['Requires native UI adapters and foreground Accessibility control.',
                                   'Styles require a classic graph bound to one two-group Column observation table.',
                                   'Placement refuses a graph already linked to the selected layout.',
                                   'Axis alignment requires exactly two distinct graph links and no layout media.',
                                   'Saved data and JSON bindings are checked; exact appearance and private drawing coordinates require live verification.']},
                    'native_overlay_composition': {
                        'status': 'native_data_driven_acceptance_verified_visual_review_required', 'tool': 'prism_compose_overlay_layout',
                        'scope': 'Replaces all selected layout contents, including annotations, with two grouped native graph pairs.',
                        'limits': ['Requires four distinct classic graphs; each pair must use identical original dataset UUIDs.',
                                   'Visible selection headers locate native objects only; they do not supply data or statistics.',
                                   'Source data, analysis settings, graph bindings, unrelated layouts, and declared media are checked.',
                                   'The recorded acceptance verifies native creation, data-driven updates/restoration, axis alignment, grouping, and appearance for its tested settings.',
                                   'New figures still require visual review; private graph/layout binaries remain undecoded.']}},
                'limits': ['New families use a bundled, verified native seed.',
                           'Classic graph and layout binaries are preserved; arbitrary binary editing is not exposed.',
                           'Cell exclusion flags and arbitrary analysis classes are not yet supported for writes.',
                           'Existing dirty documents are refused; clean target documents may be closed and reopened.'],
                'color_schemes': schemes, 'state_directory': str(self.runtime.state)}

    @staticmethod
    def _configured():
        try:
            import tomllib
            config = tomllib.loads((Path.home() / '.codex/config.toml').read_text())
            return any('prism' in n.lower() and ('prism' in str(v).lower()) for n, v in config.get('mcp_servers', {}).items())
        except (OSError, ValueError):
            return False

    def discover(self, directory: str | None = None):
        docs = []
        if directory:
            root = path_arg(directory)
            if not root.is_dir():
                raise PrismError('INVALID_PATH', 'Discovery directory does not exist.')
            for path in sorted(root.glob('*.prism'))[:100]:
                try:
                    d = project(path, include_values=False)
                    docs.append({'path': str(path), 'sha256': d['sha256'], 'table_count': len(d['tables']),
                                 'graph_count': len(d['graphs']), 'layout_count': len(d['layouts'])})
                except PrismError as exc:
                    docs.append({'path': str(path), 'error': exc.as_dict()})
        return {'windows': windows(), 'documents': docs}

    def _perform(self, kind, inputs, action, request_id=None):
        with self.runtime.locked():
            key = hashlib.sha256((kind + ':' + request_id).encode()).hexdigest() if request_id else None
            index = self.runtime.state / 'requests' / f'{key}.json' if key else None
            fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if index and index.exists():
                old = self.runtime.status(json.loads(index.read_text())['run_id'])
                if old['input_fingerprint'] != fingerprint:
                    raise PrismError('IDEMPOTENCY_CONFLICT', 'This request ID was used with different inputs.')
                if old['status'] == 'succeeded':
                    return {**old['result'], 'idempotent_replay': True}
                raise PrismError('PREVIOUS_OPERATION_FAILED', 'This request already ran; inspect its checkpoint before retrying with a new ID.',
                                 run_id=old['run_id'], status=old['status'], error=old.get('error'))
            op = self.runtime.operation(kind, inputs, request_id)
            if index:
                write_json(index, {'run_id': op.run_id})
            try:
                modal = [w for w in windows() if w['modal']]
                if modal:
                    raise PrismError('PRISM_MODAL', 'Prism has an existing modal dialog; no operation was dispatched.', titles=[w['title'] for w in modal])
                result = action(op)
                result = {'ok': True, 'run_id': op.run_id, **result}
                op.finish(result)
                if op.record['steps']:
                    write_json(self.runtime.state / 'last_verified.json', {'run_id': op.run_id, 'at': time.time(), 'kind': kind,
                                                                          'version': application_info()})
                return result
            except Exception as exc:
                error = exc if isinstance(exc, PrismError) else PrismError('OPERATION_FAILED', str(exc))
                error.details.setdefault('run_id', op.run_id)
                error.details.setdefault('checkpoint', str(op.directory))
                op.fail(error)
                raise error

    def _prepare(self, op, path: Path, *, creating=False, expected_sha256=None):
        ws = [w for w in windows() if w['path'] == str(path)]
        if any(w['edited'] is not False for w in ws):
            raise PrismError('UNSAVED_CHANGES', 'Target has unsaved changes in Prism; preserve or save them before an automated update.', path=str(path))
        if creating:
            if path.exists():
                raise PrismError('TARGET_EXISTS', 'Creation never overwrites an existing project.', path=str(path))
            source = TEMPLATE
            previous_hash = None
        else:
            project(path)
            source = path
            previous_hash = digest(path)
            if expected_sha256 and expected_sha256 != previous_hash:
                raise PrismError('DOCUMENT_CHANGED', 'Document fingerprint differs from the expected revision.')
        if ws:
            op.script([f'Open {quote(path)}', 'Close'])
        snapshot = op.directory / 'input.prism'
        shutil.copy2(source, snapshot)
        # Prism may save edits to a currently open path before Save As. Retain
        # a second checkpoint that is never opened by the native application.
        shutil.copy2(snapshot, op.directory / 'original.prism')
        if previous_hash and digest(snapshot) != previous_hash:
            raise PrismError('DOCUMENT_CHANGED', 'Source changed while its checkpoint was copied.')
        op.record['input_document_sha256'] = digest(snapshot)
        op.record['template_sha256'] = digest(TEMPLATE) if creating else None
        op.record['previous_target_sha256'] = previous_hash
        op.persist()
        return snapshot, op.directory / 'working.prism', previous_hash, bool(ws)

    @staticmethod
    def _data_csv(op, table: TableInput, index: int):
        p = op.directory / f'observations-{index}.csv'
        with p.open('w', newline='') as stream:
            writer = csv.writer(stream, lineterminator='\n')
            writer.writerow([c.label for c in table.columns])
            for row in range(max(len(c.values) for c in table.columns)):
                writer.writerow(['' if row >= len(c.values) or c.values[row] is None else repr(c.values[row]) for c in table.columns])
        write_json(op.directory / f'provenance-{index}.json', {'name': table.name, 'units': table.units,
                   'columns': [{'label': c.label, 'study_ids': c.study_ids} for c in table.columns], 'input_sha256': digest(p)})
        return p

    @staticmethod
    def _format(index: int, fmt: GraphFormat):
        commands = [f'GoTo G, {index}']
        if fmt.title is not None:
            commands.append(f'SetGraphTitle {quote(fmt.title or " ")}')
        for axis, title in [('X', fmt.x_title), ('Y', fmt.y_title)]:
            if title is not None:
                commands.append(f'SetAxisTitle {axis} {quote(title or " ")}')
        for name, value in [('bottom', fmt.y_min), ('top', fmt.y_max), ('interval', fmt.y_interval)]:
            if value is not None:
                commands.append(f'SetAxisLimits Y {name} {value:.17g}')
        if fmt.color_scheme:
            commands.append(f'ApplyColorScheme {quote(fmt.color_scheme)}')
        return commands

    @staticmethod
    def _table_format(table: TableInput):
        fmt = table.graph.model_copy()
        values = [v for c in table.columns for v in c.values if v is not None]
        lower, upper = min(values), max(values)
        span = upper-lower or max(abs(upper), 1)
        step = 10 ** math.floor(math.log10(span))
        if span/step < 2: step /= 5
        elif span/step < 5: step /= 2
        if fmt.y_min is None: fmt.y_min = math.floor((lower-span*.25)/step)*step
        if fmt.y_max is None: fmt.y_max = math.ceil((upper+span*.25)/step)*step
        if fmt.y_interval is None: fmt.y_interval = step
        if fmt.y_min >= fmt.y_max:
            raise PrismError('INVALID_AXIS_RANGE', 'Specified axis limit is incompatible with the observation-derived other limit.')
        return fmt

    @staticmethod
    def _refresh(doc):
        return ['RecalcAll'] + [f'GoTo L, {x["index"]}' for x in doc['layouts']]

    @staticmethod
    def _verify_table(actual: dict, expected: TableInput):
        precision = []
        if actual['title'] != expected.name:
            raise PrismError('WRONG_TABLE', 'Native table title does not match the intended target.')
        if len(actual['columns']) != len(expected.columns):
            raise PrismError('COLUMN_MISMATCH', 'Native data-set count changed unexpectedly.')
        for got, want in zip(actual['columns'], expected.columns):
            values = list(got['values'])
            desired = list(want.values)
            while values and values[-1] is None:
                values.pop()
            while desired and desired[-1] is None:
                desired.pop()
            errors = []
            if len(values) == len(desired):
                for v, d in zip(values, desired):
                    errors.append(0.0 if v == d else (abs(v-d) if v is not None and d is not None else math.inf))
            maximum = max(errors, default=math.inf)
            tolerance = expected.measurement_resolution * 1e-8 if expected.measurement_resolution else 0.0
            if len(values) != len(desired) or maximum > tolerance:
                raise PrismError('PRECISION_OR_DATA_MISMATCH', 'Saved observations differ from input beyond the declared measurement precision.',
                                 dataset_id=got['id'], max_absolute_error=maximum if math.isfinite(maximum) else None,
                                 allowed_error=tolerance)
            if got['title'] != want.label:
                raise PrismError('GROUP_LABEL_MISMATCH', 'Saved group label differs from intended label.', dataset_id=got['id'])
            precision.append({'dataset_id': got['id'], 'exact_numeric_match': maximum == 0,
                              'max_absolute_error': maximum, 'measurement_resolution': expected.measurement_resolution,
                              'allowed_error': tolerance})
        return precision

    @staticmethod
    def _verify_summaries(doc):
        sets = {c['id']: c for t in doc['tables'] for c in t['columns']}
        checked = []
        for a in doc['analyses']:
            if a['analysis_class'] != 'DESCRIPTIVE_STATISTICS':
                continue
            if not a['summaries'] or {x['dataset_id'] for x in a['summaries']} != set(a['input_dataset_ids']):
                raise PrismError('MISSING_NATIVE_RESULTS', 'Native descriptive analysis does not cover every linked dataset.')
            if a['results'].get('content', {}).get('status', {}).get('error') != 'VALID':
                raise PrismError('INVALID_NATIVE_RESULTS', 'Prism reports invalid descriptive results.')
            for summary in a['summaries']:
                c = sets.get(summary['dataset_id'])
                if c is None:
                    raise PrismError('BROKEN_ANALYSIS_LINK', 'Native descriptive result refers to an unknown dataset.')
                values = [v for v in c['values'] if v is not None]
                import statistics
                if (summary.get('n') != len(values) or
                    not math.isclose(summary['mean'], statistics.mean(values), rel_tol=2e-14, abs_tol=2e-14) or
                    not math.isclose(summary['median'], statistics.median(values), rel_tol=2e-14, abs_tol=2e-14)):
                    raise PrismError('STALE_NATIVE_RESULTS', 'Native counts, mean or median do not agree with saved observations.', dataset_id=c['id'])
                checked.append({'dataset_id': c['id'], 'n': summary['n'], 'mean': summary['mean'], 'median': summary['median']})
        if set(sets) != {x['dataset_id'] for x in checked}:
            raise PrismError('MISSING_NATIVE_RESULTS', 'Every observation dataset must have verified native descriptive coverage.')
        return checked

    def _publish(self, op, working, target, previous_hash, was_open):
        checked = project(working)
        summaries = self._verify_summaries(checked)
        if any(w['path'] == str(target) for w in windows()):
            raise PrismError('DOCUMENT_OPEN', 'Target was opened during the operation; publishing stopped to avoid an active writer.')
        if previous_hash is None:
            if target.exists():
                raise PrismError('TARGET_EXISTS', 'Target was created by another writer during this operation.')
        elif not target.exists() or digest(target) != previous_hash:
            raise PrismError('DOCUMENT_CHANGED', 'Source changed during the native operation; publishing stopped.')
        if (op.directory / 'cancel.request').exists():
            raise PrismError('CANCELLED', 'Cancelled before publishing; native working checkpoint remains intact.')
        target.parent.mkdir(parents=True, exist_ok=True)
        # Stage on the destination filesystem; keep the native checkpoint available for recovery.
        temp = target.parent / f'.{target.name}.{op.run_id}.tmp'
        shutil.copy2(working, temp)
        if previous_hash is None:
            try:
                os.link(temp, target)
            except FileExistsError as exc:
                raise PrismError('TARGET_EXISTS', 'Target appeared during publication.') from exc
            finally:
                temp.unlink(missing_ok=True)
        else:
            if not target.exists() or digest(target) != previous_hash:
                temp.unlink(missing_ok=True)
                raise PrismError('DOCUMENT_CHANGED', 'Target changed while the replacement was staged.')
            if any(w['path'] == str(target) for w in windows()):
                temp.unlink(missing_ok=True)
                raise PrismError('DOCUMENT_OPEN', 'Target was reopened while the replacement was staged.')
            os.replace(temp, target)
        published = project(target, include_values=False)
        registry_key = hashlib.sha256(str(target).encode()).hexdigest()
        write_json(self.runtime.state / 'documents' / f'{registry_key}.json', {'run_id': op.run_id, 'path': str(target), 'document_sha256': published['sha256'],
                   'input_fingerprint': op.record['input_fingerprint'], 'checkpoint': str(op.directory)})
        if was_open:
            op.script([f'Open {quote(target)}'])
        return {'document': published, 'native_summary_verification': summaries}

    def create_document(self, path: str, tables: list[TableInput], layout=True, request_id=None):
        target = path_arg(path)
        if target.suffix.lower() != '.prism' or not tables or len(tables) > 16:
            raise PrismError('INVALID_ARGUMENT', 'Use a .prism target and 1–16 observation tables.')
        if len({t.name for t in tables}) != len(tables):
            raise PrismError('INVALID_ARGUMENT', 'Table names must be unique.')
        inputs = {'path': str(target), 'tables': [x.model_dump() for x in tables], 'layout': layout}
        def action(op):
            source, working, old, opened = self._prepare(op, target, creating=True)
            commands = [f'Open {quote(source)}', f'Save {quote(working)}']
            for i, table in enumerate(tables, 1):
                if i > 1:
                    commands += ['GoTo D, 1', f'DuplicateFamily {quote("Additional ")}, clear']
                data = self._data_csv(op, table, i)
                commands += [f'GoTo D, {i}', 'ClearTable', f'Import {quote(data)}, 1, 1', f'SetSheetTitle {quote(table.name)}']
                fmt = self._table_format(table)
                if fmt.title is None: fmt.title = table.name
                if fmt.y_title is None: fmt.y_title = f'Time ({table.units})' if table.units else table.name
                if fmt.x_title is None: fmt.x_title = ''
                commands += self._format(i, fmt)
            commands += ['RecalcAll']
            if layout:
                commands += [f'CreateLayout {math.ceil(len(tables)/2)}, {min(2,len(tables))}, landscape, 1, {len(tables)}',
                             'GoTo L, 1', 'SetSheetTitle "Overview"']
            commands += [f'Save {quote(working)}', 'Close']
            op.script(commands, expected=[working])
            doc = project(working)
            if len(doc['tables']) != len(tables) or len(doc['graphs']) != len(tables) or len(doc['analyses']) != len(tables):
                raise PrismError('UNEXPECTED_OBJECT_COUNT', 'Native family creation did not yield the expected objects.')
            precision = [p for actual, expected in zip(doc['tables'], tables) for p in self._verify_table(actual, expected)]
            return {**self._publish(op, working, target, old, opened), 'observation_precision': precision}
        return self._perform('create_document', inputs, action, request_id)

    def update_table(self, path: str, table_id: str, table: TableInput, expected_sha256=None, request_id=None):
        target = path_arg(path)
        inputs = {'path': str(target), 'table_id': table_id, 'table': table.model_dump(), 'expected_sha256': expected_sha256}
        def action(op):
            before = project(target)
            source, working, old, opened = self._prepare(op, target, expected_sha256=expected_sha256 or before['sha256'])
            before = project(source)
            selected = lookup(before, 'tables', table_id)
            data = self._data_csv(op, table, selected['index'])
            commands = [f'Open {quote(source)}', f'GoTo D, {selected["index"]}', 'ClearTable',
                        f'Import {quote(data)}, 1, 1', f'SetSheetTitle {quote(table.name)}']
            for graph in before['graphs']:
                if set(graph['input_dataset_ids']) == set(selected['dataset_ids']):
                    # Import can rename automatically named companion sheets
                    # from the temporary CSV basename. Retain every existing
                    # graph's navigator title independently of visible titles.
                    commands += [f'GoTo G, {graph["index"]}', f'SetSheetTitle {quote(graph["title"])}']
                    commands += self._format(graph['index'], self._table_format(table))
            commands += self._refresh(before) + [f'Save {quote(working)}', 'Close']
            op.script(commands, expected=[working])
            after = project(working)
            precision = self._verify_table(lookup(after, 'tables', table_id), table)
            for untouched in before['tables']:
                if untouched['id'] != table_id and lookup(after, 'tables', untouched['id'])['columns'] != untouched['columns']:
                    raise PrismError('WRONG_TABLE_CHANGED', 'An unrelated observation table changed.')
            self._verify_existing_graphs(before, after)
            if before['layouts'] != after['layouts']:
                raise PrismError('LAYOUT_LINK_MISMATCH', 'Updating observations changed native layout identities or graph links.')
            return {**self._publish(op, working, target, old, opened), 'observation_precision': precision}
        return self._perform('update_table', inputs, action, request_id)

    def format_graph(self, path: str, graph_id: str, formatting: GraphFormat, request_id=None):
        target = path_arg(path)
        inputs = {'path': str(target), 'graph_id': graph_id, 'formatting': formatting.model_dump()}
        def action(op):
            before = project(target)
            source, working, old, opened = self._prepare(op, target, expected_sha256=before['sha256'])
            before = project(source)
            graph = lookup(before, 'graphs', graph_id)
            commands = [f'Open {quote(source)}'] + self._format(graph['index'], formatting)
            commands += self._refresh(before) + [f'Save {quote(working)}', 'Close']
            op.script(commands, expected=[working])
            after = project(working)
            if before['tables'] != after['tables']:
                raise PrismError('UNEXPECTED_DATA_CHANGE', 'Formatting unexpectedly changed observations.')
            return self._publish(op, working, target, old, opened)
        return self._perform('format_graph', inputs, action, request_id)

    def export(self, path: str, output_directory: str, formats=('pdf', 'svg'), request_id=None):
        target, output = path_arg(path), path_arg(output_directory)
        if not formats or any(f not in ('pdf', 'svg', 'png') for f in formats) or len(set(formats)) != len(formats):
            raise PrismError('INVALID_ARGUMENT', 'Choose distinct supported export formats: pdf, svg, png.')
        inputs = {'path': str(target), 'output_directory': str(output), 'formats': list(formats)}
        def action(op):
            if output.exists():
                raise PrismError('OUTPUT_EXISTS', 'Export requires a fresh directory; existing files cannot count as success.')
            before = project(target)
            source, working, _, opened = self._prepare(op, target)
            staging = op.directory / 'exports'
            staging.mkdir()
            commands = [f'Open {quote(source)}'] + self._refresh(before)
            files = []
            for collection, section, prefix in [('graphs', 'G', 'graph'), ('layouts', 'L', 'layout')]:
                for obj in before[collection]:
                    commands.append(f'GoTo {section}, {obj["index"]}')
                    for fmt in formats:
                        dest = staging / f'{prefix}-{obj["index"]}.{fmt}'
                        files.append(dest)
                        commands.append(f'Export{fmt.upper()} {quote(dest)}')
            commands += [f'Save {quote(working)}', 'Close']
            op.script(commands, expected=[working, *files])
            for file in files:
                data = file.read_bytes()
                if file.suffix == '.pdf' and not data.startswith(b'%PDF-'):
                    raise PrismError('INVALID_EXPORT', 'Export is not a PDF.', path=str(file))
                if file.suffix == '.svg' and b'<svg' not in data[:5000]:
                    raise PrismError('INVALID_EXPORT', 'Export is not SVG.', path=str(file))
                if file.suffix == '.png' and not data.startswith(b'\x89PNG\r\n\x1a\n'):
                    raise PrismError('INVALID_EXPORT', 'Export is not PNG.', path=str(file))
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staging, output)
            if opened: op.script([f'Open {quote(target)}'])
            return {'exports': [{'path': str(output / f.name), 'sha256': digest(f), 'bytes': f.stat().st_size} for f in files],
                    'source_sha256': before['sha256'], 'fresh_output_directory': str(output)}
        return self._perform('export', inputs, action, request_id)

    def open_document(self, path: str, request_id=None):
        target = path_arg(path)
        def action(op):
            doc = project(target, include_values=False)
            op.script([f'Open {quote(target)}'])
            matches = [w for w in windows() if w['path'] == str(target)]
            if len(matches) != 1:
                raise PrismError('OPEN_NOT_VERIFIED', 'Prism did not expose exactly one target document window.')
            return {'document': doc, 'window': matches[0]}
        return self._perform('open_document', {'path': str(target)}, action, request_id)

    def close_document(self, path: str, request_id=None):
        target = path_arg(path)
        def action(op):
            matches = [w for w in windows() if w['path'] == str(target)]
            if any(w['edited'] is not False for w in matches):
                raise PrismError('UNSAVED_CHANGES', 'Controlled closing refuses unsaved changes.')
            if matches: op.script([f'Open {quote(target)}', 'Close'])
            if any(w['path'] == str(target) for w in windows()):
                raise PrismError('CLOSE_NOT_VERIFIED', 'Target window remains open.')
            return {'path': str(target), 'closed': True, 'already_closed': not matches}
        return self._perform('close_document', {'path': str(target)}, action, request_id)

    def save_document(self, path: str, save_as: str | None = None, request_id=None):
        source = path_arg(path)
        target = path_arg(save_as) if save_as else source
        if target.suffix.lower() != '.prism':
            raise PrismError('INVALID_PATH', 'Native save target must end in .prism.')
        def action(op):
            before = project(source)
            if target != source and target.exists():
                raise PrismError('TARGET_EXISTS', 'Save As never overwrites an existing destination.')
            if target != source:
                snapshot, working, prior_hash, was_open = self._prepare(op, source, expected_sha256=before['sha256'])
                op.script([f'Open {quote(snapshot)}', *self._refresh(before), f'Save {quote(working)}', 'Close'], expected=[working])
                if digest(source) != prior_hash:
                    raise PrismError('DOCUMENT_CHANGED', 'The original changed while its separate native copy was saved.')
                result = self._publish(op, working, target, None, False)
                op.script([f'Open {quote(target)}'])
                return {**result, 'save_as_source_sha256': prior_hash, 'source_preserved': True,
                        'source_window_was_open': was_open, 'window': [w for w in windows() if w['path'] == str(target)]}
            shutil.copy2(source, op.directory / 'before-save.prism')
            target.parent.mkdir(parents=True, exist_ok=True)
            op.script([f'Open {quote(source)}', 'RecalcAll', *[f'GoTo L, {x["index"]}' for x in before['layouts']],
                       f'Save {quote(target)}'], expected=[target])
            after = project(target)
            return {'document': project(target, include_values=False),
                    'native_summary_verification': self._verify_summaries(after),
                    'saved_current_native_state': True, 'prior_disk_sha256': before['sha256'],
                    'window': [w for w in windows() if w['path'] == str(target)]}
        return self._perform('save_document', {'path': str(source), 'save_as': str(target)}, action, request_id)

    def add_table(self, path: str, table: TableInput, request_id=None):
        """Create an observation/violin/descriptive family with genuine native bindings."""
        target = path_arg(path)
        def action(op):
            before = project(target)
            if any(t['title'] == table.name for t in before['tables']):
                raise PrismError('DUPLICATE_TABLE_NAME', 'Use a distinct name for the new observation table.')
            if not before['tables'] or len(before['tables']) != len(before['graphs']) or len(before['tables']) != len(before['analyses']):
                raise PrismError('UNSUPPORTED_STRUCTURE', 'Adding a family currently requires one native graph and descriptive analysis per observation table.')
            self._verify_summaries(before)
            source, working, old, opened = self._prepare(op, target, expected_sha256=before['sha256'])
            index = len(before['tables']) + 1
            data = self._data_csv(op, table, index)
            fmt = self._table_format(table)
            if fmt.title is None: fmt.title = table.name
            if fmt.y_title is None: fmt.y_title = f'Duration ({table.units})' if table.units else table.name
            if fmt.x_title is None: fmt.x_title = ''
            commands = [f'Open {quote(source)}', 'GoTo D, 1', 'DuplicateFamily "Additional ", clear',
                        f'GoTo D, {index}', 'ClearTable', f'Import {quote(data)}, 1, 1', f'SetSheetTitle {quote(table.name)}']
            commands += self._format(index, fmt) + self._refresh(before) + [f'Save {quote(working)}', 'Close']
            op.script(commands, expected=[working])
            after = project(working)
            added = [t for t in after['tables'] if t['id'] not in {t['id'] for t in before['tables']}]
            if len(added) != 1:
                raise PrismError('UNEXPECTED_OBJECT_COUNT', 'Native duplication did not create exactly one table.')
            precision = self._verify_table(added[0], table)
            for t in before['tables']:
                if lookup(after, 'tables', t['id'])['columns'] != t['columns']:
                    raise PrismError('WRONG_TABLE_CHANGED', 'An existing observation table changed while adding a family.')
            return {**self._publish(op, working, target, old, opened), 'added_table_id': added[0]['id'],
                    'observation_precision': precision,
                    'created_graph_ids': [g['id'] for g in after['graphs'] if g['id'] not in {g['id'] for g in before['graphs']}],
                    'created_analysis_ids': [a['id'] for a in after['analyses'] if a['id'] not in {a['id'] for a in before['analyses']}]}
        return self._perform('add_table', {'path': str(target), 'table': table.model_dump()}, action, request_id)

    def create_layout(self, path: str, graph_ids: list[str], layout: LayoutInput, request_id=None):
        target = path_arg(path)
        def action(op):
            before = project(target)
            graphs = [lookup(before, 'graphs', identity) for identity in graph_ids]
            indices = [g['index'] for g in graphs]
            if not graphs or len(set(graph_ids)) != len(graph_ids) or indices != list(range(indices[0], indices[0]+len(graphs))):
                raise PrismError('UNSUPPORTED_LAYOUT_ORDER', 'Native grid placement currently requires distinct graphs in consecutive document order.')
            if len(graphs) != layout.rows*layout.columns:
                raise PrismError('INVALID_LAYOUT_GRID', 'Provide exactly one graph per grid cell; blank placeholders are not silently added.')
            source, working, old, opened = self._prepare(op, target, expected_sha256=before['sha256'])
            op.script([f'Open {quote(source)}', f'CreateLayout {layout.rows}, {layout.columns}, {layout.orientation}, {indices[0]}, {len(graphs)}',
                       f'GoTo L, {len(before["layouts"])+1}', f'SetSheetTitle {quote(layout.title)}',
                       'RecalcAll', f'Save {quote(working)}', 'Close'], expected=[working])
            after = project(working)
            added = [l for l in after['layouts'] if l['id'] not in {l['id'] for l in before['layouts']}]
            if len(added) != 1 or added[0]['linked_graph_ids'] != graph_ids or before['tables'] != after['tables']:
                raise PrismError('LAYOUT_LINK_MISMATCH', 'Native grid did not preserve the requested graph bindings and source observations.')
            return {**self._publish(op, working, target, old, opened), 'created_layout': added[0],
                    'placement': {'rows': layout.rows, 'columns': layout.columns, 'alignment': 'native grid'}}
        return self._perform('create_layout', {'path': str(target), 'graph_ids': graph_ids, 'layout': layout.model_dump()}, action, request_id)

    def recalculate(self, path: str, request_id=None):
        target = path_arg(path)
        def action(op):
            before = project(target)
            source, working, old, opened = self._prepare(op, target, expected_sha256=before['sha256'])
            op.script([f'Open {quote(source)}', *self._refresh(before), f'Save {quote(working)}', 'Close'], expected=[working])
            after = project(working)
            if before['tables'] != after['tables']:
                raise PrismError('UNEXPECTED_DATA_CHANGE', 'Recalculation changed an original observation table.')
            return self._publish(op, working, target, old, opened)
        return self._perform('recalculate', {'path': str(target)}, action, request_id)

    @staticmethod
    def _verify_configured_descriptives(doc, analysis):
        """Check saved descriptive numbers; CI endpoint accuracy is a separate experiment."""
        columns = {column['id']: column for table in doc['tables'] for column in table['columns']}
        parameters = analysis['parameters']['content']
        checks = []
        for summary in analysis['summaries']:
            identity = summary['dataset_id']
            values = [value for value in columns[identity]['values'] if value is not None]
            if not values:
                raise PrismError('UNSUPPORTED_ANALYSIS_SETTINGS', 'Descriptive configuration requires nonempty observed datasets.', dataset_id=identity)
            mean = statistics.mean(values)
            expected = {'mean': mean, 'median': statistics.median(values), 'sum': math.fsum(values),
                        'minimum': min(values), 'maximum': max(values)}
            if len(values) > 1:
                sd = statistics.stdev(values)
                expected.update(sd=sd, meanSE=sd / math.sqrt(len(values)))
                if mean != 0:
                    expected['coeffOfVariation'] = 100 * sd / mean
                elif parameters['calc']['coefficientOfVariation']:
                    raise PrismError('UNSUPPORTED_ANALYSIS_SETTINGS', 'A zero-mean dataset has no finite coefficient of variation.', dataset_id=identity)
            elif parameters['calc']['meanCI'] or parameters['calc']['coefficientOfVariation']:
                raise PrismError('UNSUPPORTED_ANALYSIS_SETTINGS', 'Mean CI and coefficient of variation verification require at least two observations.', dataset_id=identity)
            if type(summary.get('n')) is not int or summary['n'] != len(values):
                raise PrismError('STALE_NATIVE_RESULTS', 'Native descriptive count disagrees with original observations.', dataset_id=identity)
            for field, wanted in expected.items():
                actual = summary.get(field)
                if (type(actual) not in (int, float) or not math.isfinite(actual) or
                        not math.isclose(actual, wanted, rel_tol=2e-12, abs_tol=2e-12)):
                    raise PrismError('STALE_NATIVE_RESULTS', 'A saved native descriptive value disagrees with original observations.',
                                     dataset_id=identity, result_field=field)
            check = {'dataset_id': identity, 'n': len(values), 'numeric_fields_verified': sorted(expected),
                     'mean_ci_endpoints_numerically_verified': False}
            if parameters['calc']['meanCI']:
                ci = summary.get('meanCI')
                if (not isinstance(ci, dict) or
                        any(type(ci.get(field)) not in (int, float) or not math.isfinite(ci[field]) for field in ('lower', 'upper', 'level')) or
                        ci['level'] != parameters['options']['confidenceLevel'] or not ci['lower'] <= mean <= ci['upper'] or
                        not math.isclose(mean - ci['lower'], ci['upper'] - mean, rel_tol=2e-12, abs_tol=2e-12)):
                    raise PrismError('STALE_NATIVE_RESULTS', 'Native mean CI must report the requested confidence level and finite bounds symmetric about the observed mean.',
                                     dataset_id=identity, result_field='meanCI')
                check['mean_ci_level_and_bounds_structure_verified'] = True
            checks.append(check)
        return checks

    def configure_analysis(self, path: str, analysis_id: str, settings: DescriptiveSettings,
                           expected_sha256: str | None = None, request_id=None):
        """Experimental bounded descriptive settings followed by native recalculation and verification."""
        target = path_arg(path)
        try:
            requested = DescriptiveSettings.model_validate(
                settings.model_dump(exclude_unset=True, warnings=False) if isinstance(settings, DescriptiveSettings) else settings)
        except ValidationError as exc:
            raise PrismError('INVALID_ARGUMENT', 'Unsupported descriptive settings request.',
                             validation_errors=exc.errors(include_url=False, include_input=False, include_context=False)) from exc
        inputs = {'path': str(target), 'analysis_id': analysis_id, 'settings': requested.model_dump(exclude_unset=True),
                  'expected_sha256': expected_sha256}

        def action(op):
            before = project(target)
            selected = lookup(before, 'analyses', analysis_id)
            if selected['analysis_class'] != ANALYSIS_CLASS:
                raise PrismError('UNSUPPORTED_ANALYSIS_SETTINGS', 'Configuration supports only the observed descriptive-statistics analysis class.')
            source, working, old, opened = self._prepare(op, target, expected_sha256=expected_sha256 or before['sha256'])
            edited = op.directory / 'analysis-settings.prism'
            backup = op.directory / 'original.prism'
            checkpoints = {str(source), str(edited), str(working), str(backup)}
            if any(window['path'] in checkpoints for window in windows()):
                raise PrismError('DOCUMENT_OPEN', 'An owned analysis-settings checkpoint unexpectedly has an open Prism writer.')
            edit = write_descriptive_settings(source, edited, analysis_id, requested, expected_source_sha256=digest(source))
            write_json(op.directory / 'analysis-settings-edit.json', edit)
            intended = project(edited)
            parameter_states = {item['id']: _analysis_parameter_signature(edited, item['id']) for item in intended['analyses']}
            if any(window['path'] in checkpoints for window in windows()):
                raise PrismError('DOCUMENT_OPEN', 'An owned analysis-settings checkpoint was opened before native dispatch.')
            op.script([f'Open {quote(edited)}', *self._refresh(before), f'Save {quote(working)}', 'Close'], expected=[working])
            after = project(working)
            if before['tables'] != after['tables']:
                raise PrismError('UNEXPECTED_DATA_CHANGE', 'Analysis configuration changed an original observation table.')
            if before['graphs'] != after['graphs'] or before['layouts'] != after['layouts']:
                raise PrismError('UNEXPECTED_OBJECT_CHANGE', 'Analysis configuration changed graph/layout identities, titles, or bindings.')
            if [item['id'] for item in before['analyses']] != [item['id'] for item in after['analyses']]:
                raise PrismError('UNEXPECTED_ANALYSIS_CHANGE', 'Analysis configuration changed the native analysis identities or order.')
            for expected in intended['analyses']:
                actual = lookup(after, 'analyses', expected['id'])
                for key in ('analysis_class', 'title', 'input_dataset_ids', 'input_sheet_ids'):
                    if actual[key] != expected[key]:
                        raise PrismError('ANALYSIS_SETTINGS_MISMATCH', 'Native saving did not preserve the intended analysis settings and bindings.',
                                         analysis_id=expected['id'], field=key)
                if _analysis_parameter_signature(working, expected['id']) != parameter_states[expected['id']]:
                    raise PrismError('ANALYSIS_SETTINGS_MISMATCH', 'Saved native parameters differ from the intended settings, including retained fields.',
                                     analysis_id=expected['id'], field='parameters')
            configured = lookup(after, 'analyses', analysis_id)
            checks = self._verify_configured_descriptives(after, configured)
            if digest(source) != op.record['input_document_sha256'] or digest(backup) != op.record['input_document_sha256']:
                raise PrismError('CHECKPOINT_CHANGED', 'An unopened original observation checkpoint changed during native configuration.')
            result = self._publish(op, working, target, old, opened)
            return {**result, 'configured_analysis_id': analysis_id, 'analysis': configured,
                    'requested_settings': inputs['settings'], 'offline_parameter_edit': edit,
                    'native_settings_verification': {'parameters_roundtrip_verified': True,
                        'original_observations_unchanged': True, 'object_identities_and_bindings_preserved': True,
                        'descriptive_results': checks, 'displayed_significant_digits_verified': False,
                        'limitations': ['Exact t-based mean CI endpoints and displayed significant digits require separate native differential verification.']}}

        return self._perform('configure_analysis', inputs, action, request_id)

    def export_table(self, path: str, table_id: str, output_path: str, request_id=None):
        target, output = path_arg(path), path_arg(output_path)
        def action(op):
            before = project(target)
            table = lookup(before, 'tables', table_id)
            if output.exists():
                raise PrismError('OUTPUT_EXISTS', 'Table export requires a new file.')
            import zipfile
            with zipfile.ZipFile(target) as archive:
                payload = archive.read(f'data/tables/{table["table_id"]}/data.csv')
            if digest(target) != before['sha256']:
                raise PrismError('DOCUMENT_CHANGED', 'Source changed during table export.')
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('xb') as stream: stream.write(payload)
            return {'path': str(output), 'sha256': digest(output), 'table_id': table_id,
                    'source_sha256': before['sha256'], 'adapter': 'exact_saved_native_csv',
                    'precision': 'Saved native CSV bytes copied without numeric conversion.'}
        return self._perform('export_table', {'path': str(target), 'table_id': table_id, 'output_path': str(output)}, action, request_id)

    @staticmethod
    def _native_column_table(table):
        columns = table['columns']
        if (table['format'] != 'column' or table['data_format'] != 'y_single'
                or len(columns) != 2 or [c['id'] for c in columns] != table['dataset_ids']
                or len({c['title'] for c in columns}) != 2
                or any(not c['title'] or sum(v is not None for v in c['values']) < 3 for c in columns)):
            raise PrismError('UNSUPPORTED_NATIVE_GRAPH', 'Native graph controls require two distinct groups with at least three original Column observations each.')
        return table

    @classmethod
    def _native_graph_table(cls, document, graph):
        if graph.get('serialization') != 'PCFFGRA4':
            raise PrismError('UNSUPPORTED_NATIVE_GRAPH', 'The native style adapter accepts only the observed classic graph serialization.')
        matches = [table for table in document['tables'] if table['dataset_ids'] == graph['input_dataset_ids']]
        if len(matches) != 1:
            raise PrismError('UNSUPPORTED_NATIVE_GRAPH', 'The selected graph must use exactly one original two-group table in dataset order.')
        return cls._native_column_table(matches[0])

    @staticmethod
    def _verify_native_sources(before, after):
        if before['tables'] != after['tables']:
            raise PrismError('UNEXPECTED_DATA_CHANGE', 'Native graph editing changed an original observation table.')
        keys = ('id', 'index', 'title', 'analysis_class', 'input_dataset_ids', 'input_sheet_ids', 'parameters')
        original = [{key: analysis[key] for key in keys} for analysis in before['analyses']]
        saved = [{key: analysis[key] for key in keys} for analysis in after['analyses']]
        if original != saved:
            raise PrismError('UNEXPECTED_ANALYSIS_CHANGE', 'Native graph editing changed analysis identities, inputs, or settings.')

    @staticmethod
    def _verify_existing_graphs(before, after, *, added_id=None):
        saved = [graph for graph in after['graphs'] if graph['id'] != added_id]
        if [graph['id'] for graph in saved] != [graph['id'] for graph in before['graphs']]:
            raise PrismError('UNEXPECTED_GRAPH_CHANGE', 'Native graph editing changed existing graph identities or their relative order.')
        for expected, actual in zip(before['graphs'], saved, strict=True):
            # A newly inserted graph can shift existing document indices.
            ignored = {'index'} if added_id else set()
            if ({key: value for key, value in expected.items() if key not in ignored}
                    != {key: value for key, value in actual.items() if key not in ignored}):
                raise PrismError('GRAPH_LINK_MISMATCH', 'Native graph editing changed an existing graph title or binding.', graph_id=expected['id'])

    @staticmethod
    def _native_ui_boundary(op):
        if (op.directory / 'cancel.request').exists():
            raise PrismError('CANCELLED', 'Cancelled before native UI control; owned checkpoints remain intact.')

    @staticmethod
    def _verify_native_backup(op, working):
        original = op.directory / 'original.prism'
        if digest(original) != op.record['input_document_sha256']:
            raise PrismError('CHECKPOINT_CHANGED', 'The unopened original checkpoint changed during native graph editing.')
        for analysis in project(original, include_values=False)['analyses']:
            if _analysis_parameter_signature(original, analysis['id']) != _analysis_parameter_signature(working, analysis['id']):
                raise PrismError('UNEXPECTED_ANALYSIS_CHANGE', 'Native graph editing changed exact analysis parameter values.', analysis_id=analysis['id'])

    @staticmethod
    def _verify_overlay_archive(original, working, untouched_layouts):
        """Check untouched layout bytes and known picture/media archive assets.

        This does not decode private drawing objects or establish native visual
        acceptance; those require a separate Prism verification.
        """
        picture_suffixes = {'.png', '.jpg', '.jpeg', '.gif', '.tif', '.tiff', '.bmp', '.webp',
                            '.heic', '.svg', '.pdf', '.eps', '.emf', '.wmf', '.pict', '.pct'}
        def picture_or_media(name):
            member = Path(name.lower())
            return member.suffix in picture_suffixes or any(part in {'media', 'images', 'pictures', 'embeddings'} for part in member.parts)
        with zipfile.ZipFile(original) as old, zipfile.ZipFile(working) as new:
            old_names = {name for name in old.namelist() if not name.endswith('/')}
            new_names = {name for name in new.namelist() if not name.endswith('/')}
            for identity in untouched_layouts:
                prefix = f'layouts/{identity}/'
                old_members = {name for name in old_names if name.startswith(prefix)}
                new_members = {name for name in new_names if name.startswith(prefix)}
                if old_members != new_members or any(old.read(name) != new.read(name) for name in old_members):
                    raise PrismError('UNEXPECTED_LAYOUT_CHANGE', 'Composition changed an unselected layout record or drawing.', layout_id=identity)
            changed_assets = sorted(name for name in new_names if picture_or_media(name)
                                    and (name not in old_names or old.read(name) != new.read(name)))
            if changed_assets:
                raise PrismError('UNEXPECTED_MEDIA_CHANGE', 'Composition introduced or replaced a picture/media archive member.', members=changed_assets)

    def _save_after_native_ui(self, op, working, before):
        # Closing the last document inside a script after native UI edits can
        # leave Prism's controller busy after its COMPLETE log. Finish the save
        # and sender cleanup first, then use the normal exact-window close.
        from .native_ui import close_saved_document
        op.script([f'Open {quote(working)}', *self._refresh(before), f'Save {quote(working)}'], expected=[working])
        self._native_ui_boundary(op)
        closed = close_saved_document(working)
        write_json(op.directory / 'native-checkpoint-close.json', closed)
        op.record['native_checkpoint_close'] = closed
        op.persist()

    def format_native_graph(self, path: str, graph_id: str, style: NativeGraphStyle,
                            expected_sha256=None, request_id=None):
        target = path_arg(path)
        try:
            requested = NativeGraphStyle.model_validate(style.model_dump() if isinstance(style, NativeGraphStyle) else style)
        except ValidationError as exc:
            raise PrismError('INVALID_ARGUMENT', 'Native graph style is invalid.', validation=exc.errors(include_url=False, include_input=False, include_context=False)) from exc
        inputs = {'path': str(target), 'graph_id': graph_id, 'style': requested.model_dump(), 'expected_sha256': expected_sha256}

        def action(op):
            before = project(target)
            selected = lookup(before, 'graphs', graph_id)
            table = self._native_graph_table(before, selected)
            adapter = _native_graph_adapter('format_classic')
            source, working, old, opened = self._prepare(op, target, expected_sha256=expected_sha256 or before['sha256'])
            op.script([f'Open {quote(source)}', f'Save {quote(working)}', f'GoTo G, {selected["index"]}'], expected=[working])
            self._native_ui_boundary(op)
            result = adapter(working, [column['title'] for column in table['columns']], requested.model_dump())
            write_json(op.directory / 'native-ui.json', result)
            self._save_after_native_ui(op, working, before)
            after = project(working)
            self._verify_native_sources(before, after)
            self._verify_existing_graphs(before, after)
            if before['layouts'] != after['layouts']:
                raise PrismError('LAYOUT_LINK_MISMATCH', 'Native graph styling changed layout identities, media, or graph links.')
            self._verify_native_backup(op, working)
            return {**self._publish(op, working, target, old, opened), 'formatted_graph_id': graph_id,
                    'requested_style': requested.model_dump(), 'native_graph_style': result,
                    'native_style_verification': {'original_observations_unchanged': True,
                        'object_identities_and_bindings_preserved': True, 'saved_appearance_decoded': False,
                        'acceptance_status': 'native_controls_and_bindings_verified_visual_review_required'}}

        return self._perform('format_native_graph', inputs, action, request_id)

    def create_graph(self, path: str, table_id: str, title: str, kind: str = 'median_iqr',
                     formatting: GraphFormat | None = None, style: NativeGraphStyle | None = None,
                     expected_sha256=None, request_id=None):
        target = path_arg(path)
        try:
            requested = NativeGraphCreation.model_validate({'title': title, 'kind': kind,
                'formatting': formatting.model_dump() if isinstance(formatting, GraphFormat) else ({} if formatting is None else formatting),
                'style': style.model_dump() if isinstance(style, NativeGraphStyle) else style})
        except ValidationError as exc:
            raise PrismError('INVALID_ARGUMENT', 'Native graph creation settings are invalid.', validation=exc.errors(include_url=False, include_input=False, include_context=False)) from exc
        inputs = {'path': str(target), 'table_id': table_id, **requested.model_dump(), 'expected_sha256': expected_sha256}

        def action(op):
            before = project(target)
            table = self._native_column_table(lookup(before, 'tables', table_id))
            if any(graph['title'] == requested.title for graph in before['graphs']):
                raise PrismError('GRAPH_TITLE_EXISTS', 'The new graph needs a distinct sheet title.')
            creator = _native_graph_adapter('create_summary')
            formatter = _native_graph_adapter('format_classic') if requested.style is not None else None
            source, working, old, opened = self._prepare(op, target, expected_sha256=expected_sha256 or before['sha256'])
            op.script([f'Open {quote(source)}', f'Save {quote(working)}', f'GoTo D, {table["index"]}'], expected=[working])
            self._native_ui_boundary(op)
            created_ui = creator(working)
            write_json(op.directory / 'native-create-graph.json', created_ui)
            op.script([f'Open {quote(working)}', f'Save {quote(working)}'], expected=[working])
            interim = project(working)
            existing = {graph['id'] for graph in before['graphs']}
            added = [graph for graph in interim['graphs'] if graph['id'] not in existing]
            if len(added) != 1 or len(interim['graphs']) != len(before['graphs']) + 1:
                raise PrismError('UNEXPECTED_GRAPH_COUNT', 'Native creation did not add exactly one distinct graph.')
            created = added[0]
            self._verify_native_sources(before, interim)
            self._verify_existing_graphs(before, interim, added_id=created['id'])
            if self._native_graph_table(interim, created)['id'] != table_id:
                raise PrismError('GRAPH_LINK_MISMATCH', 'The new graph is not bound to the selected original table.')
            if before['layouts'] != interim['layouts']:
                raise PrismError('LAYOUT_LINK_MISMATCH', 'Native graph creation changed an existing layout.')
            commands = [f'Open {quote(working)}', f'GoTo G, {created["index"]}', f'SetSheetTitle {quote(requested.title)}']
            commands += self._format(created['index'], requested.formatting)
            op.script(commands)
            style_ui = None
            if formatter is not None:
                self._native_ui_boundary(op)
                style_ui = formatter(working, [column['title'] for column in table['columns']], requested.style.model_dump())
                write_json(op.directory / 'native-style-graph.json', style_ui)
            self._save_after_native_ui(op, working, before)
            after = project(working)
            self._verify_native_sources(before, after)
            self._verify_existing_graphs(before, after, added_id=created['id'])
            final_graph = lookup(after, 'graphs', created['id'])
            if (len(after['graphs']) != len(before['graphs']) + 1 or final_graph['title'] != requested.title
                    or self._native_graph_table(after, final_graph)['id'] != table_id):
                raise PrismError('GRAPH_LINK_MISMATCH', 'The saved new graph does not retain its requested identity, title, and original inputs.')
            if before['layouts'] != after['layouts']:
                raise PrismError('LAYOUT_LINK_MISMATCH', 'Native graph creation changed layout identities, media, or graph links.')
            self._verify_native_backup(op, working)
            return {**self._publish(op, working, target, old, opened), 'created_graph': final_graph,
                    'requested_kind': requested.kind, 'native_graph_creation': created_ui, 'native_graph_style': style_ui,
                    'native_graph_verification': {'original_observations_unchanged': True, 'original_dataset_bindings_verified': True,
                        'saved_graph_type_decoded': False, 'acceptance_status': 'native_controls_and_bindings_verified_visual_review_required'}}

        return self._perform('create_graph', inputs, action, request_id)

    def place_graph(self, path: str, layout_id: str, graph_id: str, x_inches: float, y_inches: float,
                    scale_percent: int = 100, expected_sha256=None, request_id=None):
        target = path_arg(path)
        try:
            placement = NativeGraphPlacement.model_validate({'x_inches': x_inches, 'y_inches': y_inches, 'scale_percent': scale_percent})
        except ValidationError as exc:
            raise PrismError('INVALID_ARGUMENT', 'Native graph placement is invalid.', validation=exc.errors(include_url=False, include_input=False, include_context=False)) from exc
        inputs = {'path': str(target), 'layout_id': layout_id, 'graph_id': graph_id,
                  **placement.model_dump(), 'expected_sha256': expected_sha256}

        def action(op):
            before = project(target)
            selected = lookup(before, 'layouts', layout_id)
            graph = lookup(before, 'graphs', graph_id)
            self._native_graph_table(before, graph)
            if graph_id in selected['linked_graph_ids']:
                raise PrismError('GRAPH_ALREADY_PLACED', 'The layout already links this graph; duplicate instances are not supported by this adapter.')
            adapter = _native_graph_adapter('place_graph')
            source, working, old, opened = self._prepare(op, target, expected_sha256=expected_sha256 or before['sha256'])
            op.script([f'Open {quote(source)}', f'Save {quote(working)}', f'GoTo L, {selected["index"]}'], expected=[working])
            self._native_ui_boundary(op)
            result = adapter(working, graph['index'], graph['title'], placement.x_inches, placement.y_inches, placement.scale_percent)
            write_json(op.directory / 'native-ui.json', result)
            self._save_after_native_ui(op, working, before)
            after = project(working)
            self._verify_native_sources(before, after)
            self._verify_existing_graphs(before, after)
            if [layout['id'] for layout in before['layouts']] != [layout['id'] for layout in after['layouts']]:
                raise PrismError('LAYOUT_LINK_MISMATCH', 'Graph placement changed layout identities or order.')
            for expected in before['layouts']:
                actual = lookup(after, 'layouts', expected['id'])
                if expected['id'] != layout_id:
                    valid = actual == expected
                else:
                    expected_links = set(expected['linked_graph_ids']) | {graph_id}
                    valid = (len(actual['linked_graph_ids']) == len(expected_links)
                        and set(actual['linked_graph_ids']) == expected_links
                        and {key: value for key, value in actual.items() if key != 'linked_graph_ids'}
                            == {key: value for key, value in expected.items() if key != 'linked_graph_ids'})
                if not valid:
                    raise PrismError('LAYOUT_LINK_MISMATCH', 'Placement must retain every original layout link and add only the selected native graph.', layout_id=expected['id'])
            self._verify_native_backup(op, working)
            return {**self._publish(op, working, target, old, opened), 'layout': lookup(after, 'layouts', layout_id),
                    'placed_graph_id': graph_id, 'requested_placement': placement.model_dump(), 'native_graph_placement': result,
                    'native_placement_verification': {'original_observations_unchanged': True, 'native_graph_link_added': True,
                        'saved_coordinates_decoded': False, 'acceptance_status': 'native_validation_pending'}}

        return self._perform('place_graph', inputs, action, request_id)

    def align_layout(self, path: str, layout_id: str, axes: list[str] | None = None,
                     expected_sha256=None, request_id=None):
        """Experimental alignment; link/media metadata cannot exclude native text.

        The native adapter must verify a graph-only selection. For composition,
        align the two graphs before adding any native text annotations.
        """
        target = path_arg(path)
        try:
            alignment = NativeLayoutAlignment.model_validate({} if axes is None else {'axes': axes})
        except ValidationError as exc:
            raise PrismError('INVALID_ARGUMENT', 'Native layout alignment is invalid.', validation=exc.errors(include_url=False, include_input=False, include_context=False)) from exc
        inputs = {'path': str(target), 'layout_id': layout_id, **alignment.model_dump(), 'expected_sha256': expected_sha256}

        def action(op):
            before = project(target)
            selected = lookup(before, 'layouts', layout_id)
            if len(selected['linked_graph_ids']) != 2 or len(set(selected['linked_graph_ids'])) != 2 or selected['media_count'] != 0:
                raise PrismError('UNSUPPORTED_LAYOUT_ALIGNMENT', 'Alignment requires exactly two distinct native graph links and no layout media.')
            for identity in selected['linked_graph_ids']:
                self._native_graph_table(before, lookup(before, 'graphs', identity))
            adapter = _native_graph_adapter('align_all_graphs')
            source, working, old, opened = self._prepare(op, target, expected_sha256=expected_sha256 or before['sha256'])
            op.script([f'Open {quote(source)}', f'Save {quote(working)}', f'GoTo L, {selected["index"]}'], expected=[working])
            self._native_ui_boundary(op)
            result = adapter(working, list(alignment.axes))
            write_json(op.directory / 'native-ui.json', result)
            self._save_after_native_ui(op, working, before)
            after = project(working)
            self._verify_native_sources(before, after)
            self._verify_existing_graphs(before, after)
            if before['layouts'] != after['layouts']:
                raise PrismError('LAYOUT_LINK_MISMATCH', 'Axis alignment changed layout identities, media, or graph links.')
            self._verify_native_backup(op, working)
            return {**self._publish(op, working, target, old, opened), 'aligned_layout_id': layout_id,
                    'requested_axes': list(alignment.axes), 'native_layout_alignment': result,
                    'native_alignment_verification': {'original_observations_unchanged': True,
                        'object_identities_and_bindings_preserved': True, 'saved_coordinates_decoded': False,
                        'acceptance_status': 'native_validation_pending'}}

        return self._perform('align_layout', inputs, action, request_id)

    def compose_overlay_layout(self, path: str, layout_id: str, panels: list[NativeOverlayPanel],
                               expected_sha256=None, request_id=None):
        """Replace all selected layout contents, including annotations, with two
        grouped native graph pairs. Selection headers identify UI objects only;
        graph types, private drawing positions, and grouping remain experimental.
        """
        target = path_arg(path)
        try:
            values = [panel.model_dump() if isinstance(panel, NativeOverlayPanel) else panel for panel in panels] if isinstance(panels, list) else panels
            requested = NativeOverlayComposition.model_validate({'panels': values})
        except ValidationError as exc:
            raise PrismError('INVALID_ARGUMENT', 'Native overlay composition is invalid.', validation=exc.errors(include_url=False, include_input=False, include_context=False)) from exc
        inputs = {'path': str(target), 'layout_id': layout_id, **requested.model_dump(), 'expected_sha256': expected_sha256}

        def action(op):
            before = project(target)
            selected = lookup(before, 'layouts', layout_id)
            native_panels, requested_ids = [], []
            with zipfile.ZipFile(target) as archive:
                if json.loads(archive.read(f'layouts/{layout_id}/sheet.json')).get('@class') != 'FENLayoutSheet':
                    raise PrismError('UNSUPPORTED_NATIVE_LAYOUT', 'Overlay composition requires the observed classic native layout class.')
                for panel in requested.panels:
                    pair = {}
                    for role in ('violin', 'summary'):
                        identity = getattr(panel, role + '_id')
                        matches = [graph for graph in before['graphs'] if graph['id'].lower() == identity.lower()]
                        if len(matches) != 1:
                            raise PrismError('OBJECT_NOT_FOUND', 'No unique native graph has the requested overlay UUID.', id=identity)
                        graph = matches[0]
                        if json.loads(archive.read(f'graphs/{graph["id"]}/sheet.json')).get('@class') != 'FENGraphSheet':
                            raise PrismError('UNSUPPORTED_NATIVE_GRAPH', 'Overlay composition requires classic FEN graph sheets.', graph_id=graph['id'])
                        self._native_graph_table(before, graph)
                        pair[role] = graph
                        requested_ids.append(graph['id'])
                    if pair['violin']['input_dataset_ids'] != pair['summary']['input_dataset_ids']:
                        raise PrismError('GRAPH_LINK_MISMATCH', 'Both graphs in an overlay panel must use the same ordered original dataset UUIDs.')
                    native_panels.append({
                        'violin_index': pair['violin']['index'], 'violin_sheet_title': pair['violin']['title'],
                        'summary_index': pair['summary']['index'], 'summary_sheet_title': pair['summary']['title'],
                        'selection_label': panel.selection_label, 'x_inches': panel.x_inches,
                        'y_inches': panel.y_inches, 'scale_percent': panel.scale_percent})
            adapter = _native_graph_adapter('compose_overlay_layout')
            source, working, old, opened = self._prepare(op, target, expected_sha256=expected_sha256 or before['sha256'])
            op.script([f'Open {quote(source)}', f'Save {quote(working)}', f'GoTo L, {selected["index"]}'], expected=[working])
            self._native_ui_boundary(op)
            result = adapter(working, native_panels, op.directory)
            write_json(op.directory / 'native-ui.json', result)
            self._save_after_native_ui(op, working, before)
            after = project(working)
            self._verify_native_sources(before, after)
            self._verify_existing_graphs(before, after)
            if [layout['id'] for layout in before['layouts']] != [layout['id'] for layout in after['layouts']]:
                raise PrismError('LAYOUT_LINK_MISMATCH', 'Composition changed layout identities or order.')
            for expected in before['layouts']:
                actual = lookup(after, 'layouts', expected['id'])
                if expected['id'] != layout_id:
                    valid = actual == expected
                else:
                    ignored = {'linked_graph_ids', 'media_count'}
                    valid = (len(actual['linked_graph_ids']) == 4 and set(actual['linked_graph_ids']) == set(requested_ids)
                        and actual['media_count'] == 0
                        and {key: value for key, value in expected.items() if key not in ignored}
                            == {key: value for key, value in actual.items() if key not in ignored})
                if not valid:
                    raise PrismError('LAYOUT_LINK_MISMATCH', 'Composition must retain every unselected layout and rebuild only the selected layout with the four requested graph links and no declared media.', layout_id=expected['id'])
            self._verify_native_backup(op, working)
            self._verify_overlay_archive(op.directory / 'original.prism', working,
                                         [layout['id'] for layout in before['layouts'] if layout['id'] != layout_id])
            return {**self._publish(op, working, target, old, opened), 'layout': lookup(after, 'layouts', layout_id),
                    'requested_panels': requested.model_dump()['panels'], 'native_overlay_composition': result,
                    'replaced_layout_contents': True,
                    'native_overlay_verification': {'original_observations_unchanged': True,
                        'original_dataset_bindings_verified': True, 'requested_graph_ids': requested_ids,
                        'unselected_layouts_unchanged': True, 'declared_layout_media_count': 0,
                        'no_new_picture_or_media_members': True, 'saved_graph_types_decoded': False,
                        'saved_coordinates_and_grouping_decoded': False, 'acceptance_status': 'native_controls_and_bindings_verified_visual_review_required'}}

        return self._perform('compose_overlay_layout', inputs, action, request_id)

    def resize_object(self, path: str, object_id: str, kind: str, width_inches: float, scale_text=True, request_id=None):
        from .native_ui import resize
        target = path_arg(path)
        if kind not in ('graph', 'layout') or not math.isfinite(width_inches) or not .5 <= width_inches <= 20:
            raise PrismError('INVALID_ARGUMENT', 'Resize supports a graph/layout width of 0.5–20 inches.')
        collection, section = ('graphs', 'G') if kind == 'graph' else ('layouts', 'L')
        def action(op):
            before = project(target)
            selected = lookup(before, collection, object_id)
            source, working, old, opened = self._prepare(op, target, expected_sha256=before['sha256'])
            op.script([f'Open {quote(source)}', f'Save {quote(working)}', f'GoTo {section}, {selected["index"]}'], expected=[working])
            result = resize(working, kind, width_inches, scale_text)
            write_json(op.directory/'native-ui.json', result)
            self._save_after_native_ui(op, working, before)
            after = project(working)
            if before['tables'] != after['tables']:
                raise PrismError('UNEXPECTED_DATA_CHANGE', 'Native resizing changed observations.')
            return {**self._publish(op, working, target, old, opened), 'native_resize': result}
        return self._perform('resize_object', {'path': str(target), 'object_id': object_id, 'kind': kind,
                             'width_inches': width_inches, 'scale_text': scale_text}, action, request_id)

    def annotate_layout(self, path: str, layout_id: str, annotation: AnnotationInput, request_id=None):
        from .native_ui import annotate
        import zipfile
        target = path_arg(path)
        try:
            annotation = AnnotationInput.model_validate(annotation.model_dump() if isinstance(annotation, AnnotationInput) else annotation)
        except ValidationError as exc:
            raise PrismError('INVALID_ARGUMENT', 'Native annotation settings are invalid.', validation=exc.errors(include_url=False, include_input=False, include_context=False)) from exc
        def action(op):
            before = project(target)
            selected = lookup(before, 'layouts', layout_id)
            source, working, old, opened = self._prepare(op, target, expected_sha256=before['sha256'])
            op.script([f'Open {quote(source)}', f'Save {quote(working)}', f'GoTo L, {selected["index"]}'], expected=[working])
            self._native_ui_boundary(op)
            result = annotate(working, annotation.text, annotation.x_inches, annotation.y_inches, op.directory,
                              font_size=annotation.font_size, color=annotation.color, bold=annotation.bold,
                              italic=annotation.italic, horizontal_anchor=annotation.horizontal_anchor)
            write_json(op.directory/'native-ui.json', result)
            self._save_after_native_ui(op, working, before)
            after = project(working)
            self._verify_native_sources(before, after)
            self._verify_existing_graphs(before, after)
            if [layout['id'] for layout in before['layouts']] != [layout['id'] for layout in after['layouts']]:
                raise PrismError('LAYOUT_LINK_MISMATCH', 'Text insertion changed layout identities or order.')
            for expected in before['layouts']:
                actual = lookup(after, 'layouts', expected['id'])
                ignored = {'media_count'} if expected['id'] == layout_id else set()
                if ({key: value for key, value in expected.items() if key not in ignored}
                        != {key: value for key, value in actual.items() if key not in ignored}):
                    raise PrismError('LAYOUT_LINK_MISMATCH', 'Text insertion changed an existing layout title or native graph link.')
            token = result.get('selection_token') if isinstance(result, dict) else None
            if not isinstance(token, str) or not token:
                raise PrismError('ANNOTATION_NOT_VERIFIED', 'Native text insertion did not report its temporary selection token.')
            with zipfile.ZipFile(working) as archive:
                binary = archive.read(f'layouts/{layout_id}/data.bin')
                if token.encode('utf-16-le') in binary or token.encode('utf-8') in binary:
                    raise PrismError('ANNOTATION_NOT_VERIFIED', 'Saved native layout still contains the temporary selection text.')
                if annotation.text.encode('utf-16-le') not in binary and annotation.text.encode('utf-8') not in binary:
                    raise PrismError('ANNOTATION_NOT_VERIFIED', 'Saved native layout does not contain the expected text.')
            self._verify_native_backup(op, working)
            return {**self._publish(op, working, target, old, opened), 'native_annotation': result,
                    'annotation_semantics': 'Editable static text; native analyses remain authoritative for statistics.'}
        return self._perform('annotate_layout', {'path': str(target), 'layout_id': layout_id,
                             'annotation': annotation.model_dump()}, action, request_id)
