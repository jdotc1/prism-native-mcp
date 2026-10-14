"""Semantic graph-control tests with test-only archive changes and no native UI."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import types
import zipfile

import pytest
from mcp import types as mcp_types
from pydantic import ValidationError

from prism_mcp import backend as module, placement, runtime
from prism_mcp.archive import read_project
from prism_mcp.backend import Backend
from prism_mcp.errors import PrismError
from prism_mcp.models import AnnotationInput, GraphFormat, NativeGraphStyle, NativeGraphPlacement, NativeLayoutAlignment


NEW_GRAPH = '00000000-0000-4000-8000-000000000031'
OTHER_GRAPH = '00000000-0000-4000-8000-000000000032'
LAYOUT = '00000000-0000-4000-8000-000000000033'
OTHER_LAYOUT = '00000000-0000-4000-8000-000000000034'


@pytest.fixture(autouse=True)
def no_native_control(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, 'CONTROL_ROOT', tmp_path / 'control')
    monkeypatch.setenv('PRISM_MCP_STATE_DIR', str(tmp_path / 'server-state'))
    monkeypatch.setattr(runtime, 'application_info', lambda: {
        'installed': True, 'version_compatible': True, 'path': '/test-only/Prism.app',
        'version': '11.1.0', 'build': '110218',
    })
    monkeypatch.setattr(module, 'application_info', runtime.application_info)
    monkeypatch.setattr(runtime, 'verify_running_application', lambda: '')
    monkeypatch.setattr(module, 'windows', lambda: [])

    def prohibited(*args, **kwargs):
        pytest.fail('Semantic graph test attempted actual native control')

    monkeypatch.setattr(runtime.Operation, 'script', prohibited)
    monkeypatch.setattr(runtime, 'osa', prohibited)
    monkeypatch.setattr(runtime.subprocess, 'Popen', prohibited)
    monkeypatch.setattr(placement, 'osa', prohibited)
    monkeypatch.setattr(placement, 'enforce', prohibited)
    adapter = types.ModuleType('prism_mcp.native_style')
    for name in ('format_classic', 'create_summary', 'place_graph', 'align_all_graphs'):
        setattr(adapter, name, prohibited)
    monkeypatch.setitem(sys.modules, 'prism_mcp.native_style', adapter)
    annotation_adapter = types.ModuleType('prism_mcp.native_ui')
    annotation_adapter.annotate = prohibited
    annotation_adapter.resize = prohibited
    annotation_adapter.close_saved_document = prohibited
    monkeypatch.setitem(sys.modules, 'prism_mcp.native_ui', annotation_adapter)
    return adapter


@pytest.fixture
def backend(tmp_path):
    return Backend(tmp_path / 'state')


@pytest.fixture
def source(tmp_path):
    target = tmp_path / 'source.prism'
    target.write_bytes(module.TEMPLATE.read_bytes())
    return target


def rewrite(path, transform):
    """Only test-owned archives are edited; these are not native graph outputs."""
    with zipfile.ZipFile(path) as archive:
        values = {name: archive.read(name) for name in archive.namelist()}
        comment = archive.comment
    transform(values)
    temporary = path.with_name(path.name + '.test-rewrite')
    with zipfile.ZipFile(temporary, 'w') as output:
        output.comment = comment
        for name, data in values.items():
            output.writestr(name, data)
    os.replace(temporary, path)


def change_json(path, member, transform):
    def change(values):
        obj = json.loads(values[member])
        transform(obj)
        values[member] = json.dumps(obj).encode()
    rewrite(path, change)


def add_graph(path, identity=NEW_GRAPH, *, insert_first=False, title='Native summary'):
    def change(values):
        document = json.loads(values['document.json'])
        original = document['sheets']['graphs'][0]
        graph = json.loads(values[f'graphs/{original}/sheet.json'])
        graph.update(uid=identity, title=title)
        values[f'graphs/{identity}/sheet.json'] = json.dumps(graph).encode()
        values[f'graphs/{identity}/data.bin'] = values[f'graphs/{original}/data.bin']
        if insert_first:
            document['sheets']['graphs'].insert(0, identity)
        else:
            document['sheets']['graphs'].append(identity)
        values['document.json'] = json.dumps(document).encode()
    rewrite(path, change)


def add_layout(path, graph_ids, *, identity=LAYOUT, media_count=0):
    def change(values):
        document = json.loads(values['document.json'])
        document['sheets'].setdefault('layouts', []).append(identity)
        values['document.json'] = json.dumps(document).encode()
        values[f'layouts/{identity}/sheet.json'] = json.dumps({
            '@class': 'FENLayoutSheet', 'uid': identity, 'title': 'Test layout',
            'linkedGraphSheets': list(graph_ids), 'mediaCollection': [{} for _ in range(media_count)],
        }).encode()
        values[f'layouts/{identity}/data.bin'] = b'PCFFGRA4 test-only layout payload'
    rewrite(path, change)


def ids(source):
    doc = read_project(source)
    return doc['tables'][0]['id'], doc['graphs'][0]['id']


class FakeNative:
    def __init__(self, monkeypatch, tmp_path, adapter, *, opened=None, insert_first=False,
                 mutate=None, on_save=None, on_script=None, on_close=None):
        self.current = opened
        self.selected = None
        self.commands = []
        self.calls = []
        self.opened = []
        self.saved_from = []
        self.closed = []
        self.saves = 0
        self.tmp_path = tmp_path
        self.insert_first = insert_first
        self.mutate = mutate
        self.on_save = on_save
        self.on_script = on_script
        self.on_close = on_close
        self.annotation_encoding = 'utf-8'
        self.selection_token = 'Prism label 012345abcdef'

        monkeypatch.setattr(module, 'windows', self.windows)
        monkeypatch.setattr(runtime.Operation, 'script', lambda op, commands, **kwargs: self.script(op, commands, **kwargs))
        monkeypatch.setattr(adapter, 'format_classic', self.format_classic)
        monkeypatch.setattr(adapter, 'create_summary', self.create_summary)
        monkeypatch.setattr(adapter, 'place_graph', self.place_graph)
        monkeypatch.setattr(adapter, 'align_all_graphs', self.align_all_graphs)
        monkeypatch.setattr(sys.modules['prism_mcp.native_ui'], 'annotate', self.annotate)
        monkeypatch.setattr(sys.modules['prism_mcp.native_ui'], 'resize', self.resize)
        monkeypatch.setattr(sys.modules['prism_mcp.native_ui'], 'close_saved_document', self.close_saved_document)

    def windows(self):
        return [] if self.current is None else [{
            'path': str(self.current), 'title': self.current.name, 'edited': False, 'modal': False,
        }]

    def argument(self, command):
        arg = command.split(' ', 1)[1]
        assert arg.startswith('"') and arg.endswith('"')
        path = Path(arg[1:-1].replace('%%', '%'))
        assert path.is_relative_to(self.tmp_path), 'Test-only writer escaped its directory'
        return path

    def script(self, operation, commands, *, expected=(), **kwargs):
        self.commands.append(list(commands))
        for command in commands:
            if command.startswith('Open '):
                self.current = self.argument(command)
                assert self.current.is_file()
                self.opened.append(self.current)
            elif command.startswith('Save '):
                destination = self.argument(command)
                self.saved_from.append(self.current)
                # Simulate autosaving the open input before saving another path.
                self.saves += 1
                with zipfile.ZipFile(self.current, 'a') as archive:
                    archive.comment = f'test-only native save {self.saves}'.encode()
                if destination != self.current:
                    shutil.copy2(self.current, destination)
                self.current = destination
                if self.on_save:
                    self.on_save(operation, destination, self.saves)
            elif command.startswith('GoTo '):
                section, index = command[5:].split(', ')
                self.selected = section, int(index)
            elif command.startswith('SetSheetTitle '):
                assert self.selected[0] == 'G'
                title = command.split(' ', 1)[1][1:-1].replace('%%', '%')
                graph = read_project(self.current)['graphs'][self.selected[1] - 1]
                change_json(self.current, f'graphs/{graph["id"]}/sheet.json', lambda obj: obj.update(title=title))
            elif command == 'Close':
                self.current = None
        if self.on_script:
            self.on_script(operation, commands)
        assert all(path.is_file() and path.stat().st_size for path in expected)
        return {'status': 'succeeded', 'test_only': True}

    def call(self, name, path, args):
        assert path == self.current and path.name == 'working.prism'
        self.calls.append((name, path, args, self.selected))
        if self.mutate:
            self.mutate(name, path)
        return {'test_only': True, 'adapter': name}

    def close_saved_document(self, path):
        assert self.current == path
        assert self.commands[-1][-1] == f'Save {runtime.quote(path)}'
        assert 'Close' not in self.commands[-1]
        self.closed.append(path)
        self.current = None
        if self.on_close:
            self.on_close(path)
        return {'test_only': True, 'path': str(path.resolve()),
                'saved_state_before_close': True, 'window_closed': True}

    def resize(self, path, kind, width_inches, scale_text):
        assert self.selected[0] == ('G' if kind == 'graph' else 'L')
        return self.call('resize', path, (kind, width_inches, scale_text))

    def format_classic(self, path, labels, style):
        assert self.selected[0] == 'G'
        return self.call('format_classic', path, (labels, style))

    def create_summary(self, path):
        assert self.selected[0] == 'D'
        table = read_project(path)['tables'][self.selected[1] - 1]
        add_graph(path, insert_first=self.insert_first)
        created = next(graph for graph in read_project(path)['graphs'] if graph['id'] == NEW_GRAPH)
        assert created['input_dataset_ids'] == table['dataset_ids']
        result = self.call('create_summary', path, ())
        self.selected = 'G', created['index']
        return result

    def place_graph(self, path, graph_index, graph_title, x_inches, y_inches, scale_percent=100):
        assert self.selected[0] == 'L'
        doc = read_project(path)
        graph = doc['graphs'][graph_index - 1]
        assert graph['title'] == graph_title
        layout = doc['layouts'][self.selected[1] - 1]
        change_json(path, f'layouts/{layout["id"]}/sheet.json', lambda obj: obj['linkedGraphSheets'].append(graph['id']))
        return self.call('place_graph', path, (graph_index, graph_title, x_inches, y_inches, scale_percent))

    def align_all_graphs(self, path, axes):
        assert self.selected[0] == 'L'
        return self.call('align_all_graphs', path, (axes,))

    def annotate(self, path, text, x_inches, y_inches, directory, **style):
        assert self.selected[0] == 'L' and directory == path.parent
        layout = read_project(path)['layouts'][self.selected[1] - 1]
        member = f'layouts/{layout["id"]}/data.bin'
        rewrite(path, lambda values: values.__setitem__(member, values[member] + b'\x00' + text.encode(self.annotation_encoding)))
        return {**self.call('annotate', path, (text, x_inches, y_inches, directory, style)),
                'selection_token': self.selection_token}


def test_style_defaults_and_independent_color_lists():
    first, second = NativeGraphStyle(kind='violin'), NativeGraphStyle(kind='violin')
    assert first.width_inches == 37 / 25.4 and first.height_inches == 62 / 25.4
    assert first.colors == ['#5B2D90', '#4A4A4A'] and first.fills == ['#E1D9EB', '#E6E6E6']
    assert first.point_opacity == .5 and first.point_size == 2 and first.median_size == 4
    assert (first.axis_font_size, first.title_font_size, first.y_title_font_size) == (6, 6, 7)
    assert first.smoothing == 'High' and first.truncate and first.show_title
    first.colors[0] = '#FFFFFF'
    assert second.colors[0] == '#5B2D90'


@pytest.mark.parametrize('field,value', [
    ('kind', 'xy'), ('width_inches', .09), ('height_inches', 20.1), ('width_inches', True),
    ('height_inches', float('nan')), ('point_opacity', float('inf')), ('point_opacity', -.1),
    ('point_opacity', 1.01), ('point_size', 0), ('median_size', '4'), ('axis_font_size', 6.0),
    ('title_font_size', True), ('y_title_font_size', 0), ('y_tick_origin', float('nan')),
    ('y_tick_origin', float('inf')), ('y_tick_origin', '30'), ('show_title', 1), ('truncate', 'true'),
    ('colors', ['#123456']), ('fills', ['#123456', '#ffffff', '#000000']),
    ('colors', ['purple', '#4A4A4A']), ('fills', ['#11223344', '#E6E6E6']),
    ('smoothing', 'Silverman'), ('unknown', 1),
])
def test_style_rejects_unsafe_or_unrecognized_values(field, value):
    with pytest.raises(ValidationError):
        NativeGraphStyle.model_validate({'kind': 'violin', field: value})


@pytest.mark.parametrize('values', [{'x_inches': -1, 'y_inches': 1}, {'x_inches': 1, 'y_inches': float('nan')},
                                  {'x_inches': True, 'y_inches': 1}, {'x_inches': 1, 'y_inches': 1, 'scale_percent': 100.0},
                                  {'x_inches': 1, 'y_inches': 1, 'scale_percent': False}])
def test_placement_rejects_invalid_coordinates_and_noninteger_scale(values):
    with pytest.raises(ValidationError):
        NativeGraphPlacement.model_validate(values)


@pytest.mark.parametrize('axes', [[], ['x', 'x'], ['x', 'z'], ['X'], ('x', 'y')])
def test_alignment_rejects_ambiguous_axes(axes):
    with pytest.raises(ValidationError):
        NativeLayoutAlignment(axes=axes)


@pytest.mark.parametrize('was_open', [False, True])
def test_style_preserves_original_data_and_reopens_only_previous_target(backend, source, tmp_path, monkeypatch, no_native_control, was_open):
    original, before = source.read_bytes(), read_project(source)
    native = FakeNative(monkeypatch, tmp_path, no_native_control, opened=source if was_open else None)
    publish = backend._publish

    def publish_after_verified_close(op, working, *args):
        receipt = json.loads((op.directory / 'native-checkpoint-close.json').read_text())
        assert receipt == op.record['native_checkpoint_close']
        assert receipt['path'] == str(working) and receipt['window_closed']
        assert native.closed == [working]
        return publish(op, working, *args)

    monkeypatch.setattr(backend, '_publish', publish_after_verified_close)
    style = NativeGraphStyle(kind='violin', truncate=False, y_tick_origin=30)
    result = backend.format_native_graph(str(source), before['graphs'][0]['id'], style,
                                         expected_sha256=before['sha256'], request_id='style-one')
    record = backend.runtime.status(result['run_id'])
    checkpoint = Path(record['directory'])
    assert (checkpoint / 'original.prism').read_bytes() == original
    assert checkpoint / 'original.prism' not in native.opened
    assert module.TEMPLATE not in native.opened and source not in native.saved_from
    assert native.current == (source if was_open else None)
    after = read_project(source)
    assert after['tables'] == before['tables'] and after['graphs'] == before['graphs'] and after['layouts'] == before['layouts']
    assert native.calls[0][3] == ('G', 1)
    assert native.calls[0][2] == ([column['title'] for column in before['tables'][0]['columns']], style.model_dump())
    assert result['native_style_verification']['object_identities_and_bindings_preserved']
    assert result['native_style_verification']['acceptance_status'] == 'native_controls_and_bindings_verified_visual_review_required'
    assert (checkpoint / 'native-ui.json').is_file()
    replay = backend.format_native_graph(str(source), before['graphs'][0]['id'], style,
                                         expected_sha256=before['sha256'], request_id='style-one')
    assert replay['idempotent_replay'] and replay['run_id'] == result['run_id'] and len(native.calls) == 1


@pytest.mark.parametrize('insert_first,styled', [(False, False), (True, True)])
def test_create_summary_selects_original_table_and_verifies_new_graph_before_styling(backend, source, tmp_path, monkeypatch, no_native_control, insert_first, styled):
    original, before = source.read_bytes(), read_project(source)
    table_id = before['tables'][0]['id']
    native = FakeNative(monkeypatch, tmp_path, no_native_control, insert_first=insert_first)
    style = NativeGraphStyle(kind='median_iqr') if styled else None
    result = backend.create_graph(str(source), table_id, 'Linked summary 50%', formatting=GraphFormat(y_min=0, y_max=30, y_interval=10), style=style)
    after = read_project(source)
    assert after['tables'] == before['tables'] and after['analyses'] == before['analyses']
    created = result['created_graph']
    assert created['id'] == NEW_GRAPH and created['title'] == 'Linked summary 50%'
    assert created['input_dataset_ids'] == before['tables'][0]['dataset_ids']
    assert native.calls[0][0] == 'create_summary' and native.calls[0][3] == ('D', 1)
    if styled:
        assert native.calls[1][0] == 'format_classic' and native.calls[1][3] == ('G', created['index'])
    flattened = [command for commands in native.commands for command in commands]
    assert 'SetSheetTitle "Linked summary 50%%"' in flattened
    assert 'SetAxisLimits Y bottom 0' in flattened and 'SetAxisLimits Y top 30' in flattened
    assert 'SetAxisLimits Y interval 10' in flattened
    checkpoint = Path(backend.runtime.status(result['run_id'])['directory'])
    assert (checkpoint / 'original.prism').read_bytes() == original
    assert result['native_graph_verification']['original_dataset_bindings_verified']
    assert not result['native_graph_verification']['saved_graph_type_decoded']


def test_place_adds_only_selected_native_graph_link(backend, source, tmp_path, monkeypatch, no_native_control):
    _, first_graph = ids(source)
    add_graph(source, title='Second graph')
    add_layout(source, [first_graph])
    add_layout(source, [first_graph], identity=OTHER_LAYOUT)
    before = read_project(source)
    native = FakeNative(monkeypatch, tmp_path, no_native_control)
    result = backend.place_graph(str(source), LAYOUT, NEW_GRAPH, 1.1, 2.2, 90)
    after = read_project(source)
    assert after['tables'] == before['tables'] and after['graphs'] == before['graphs']
    assert after['layouts'][0]['linked_graph_ids'] == [first_graph, NEW_GRAPH]
    assert after['layouts'][1] == before['layouts'][1]
    assert native.calls[0][2] == (2, 'Second graph', 1.1, 2.2, 90) and native.calls[0][3] == ('L', 1)
    assert result['placed_graph_id'] == NEW_GRAPH and result['native_placement_verification']['native_graph_link_added']


@pytest.mark.parametrize('axes', [None, ['y']])
def test_alignment_uses_exact_existing_pair_without_changing_links(backend, source, tmp_path, monkeypatch, no_native_control, axes):
    _, first_graph = ids(source)
    add_graph(source)
    add_layout(source, [first_graph, NEW_GRAPH])
    before = read_project(source)
    native = FakeNative(monkeypatch, tmp_path, no_native_control)
    result = backend.align_layout(str(source), LAYOUT, axes)
    after = read_project(source)
    assert after['tables'] == before['tables'] and after['graphs'] == before['graphs'] and after['layouts'] == before['layouts']
    assert native.calls[0][2] == ((['x', 'y'] if axes is None else axes),)
    assert native.calls[0][3] == ('L', 1) and result['aligned_layout_id'] == LAYOUT


@pytest.mark.parametrize('edited', [True, None])
def test_dirty_or_unknown_target_rejected_before_ui(backend, source, monkeypatch, edited):
    _, graph_id = ids(source)
    original = source.read_bytes()
    monkeypatch.setattr(module, 'windows', lambda: [{'path': str(source), 'title': source.name, 'edited': edited, 'modal': False}])
    with pytest.raises(PrismError) as error:
        backend.format_native_graph(str(source), graph_id, NativeGraphStyle(kind='violin'))
    assert error.value.code == 'UNSAVED_CHANGES' and source.read_bytes() == original


def test_wrong_identity_stale_revision_and_duplicate_placement_fail_before_native(backend, source):
    _, graph_id = ids(source)
    add_layout(source, [graph_id])
    for invoke, code in [
        (lambda: backend.format_native_graph(str(source), NEW_GRAPH, NativeGraphStyle(kind='violin')), 'OBJECT_NOT_FOUND'),
        (lambda: backend.format_native_graph(str(source), graph_id, NativeGraphStyle(kind='violin'), '0' * 64), 'DOCUMENT_CHANGED'),
        (lambda: backend.place_graph(str(source), LAYOUT, graph_id, 1, 2), 'GRAPH_ALREADY_PLACED'),
        (lambda: backend.create_graph(str(source), ids(source)[0], read_project(source)['graphs'][0]['title']), 'GRAPH_TITLE_EXISTS'),
    ]:
        with pytest.raises(PrismError) as error:
            invoke()
        assert error.value.code == code


@pytest.mark.parametrize('count,media', [(0, 0), (1, 0), (3, 0), (2, 1)])
def test_alignment_refuses_other_object_selections_before_ui(backend, source, count, media):
    _, first_graph = ids(source)
    add_graph(source)
    add_graph(source, identity=OTHER_GRAPH)
    add_layout(source, [first_graph, NEW_GRAPH, OTHER_GRAPH][:count], media_count=media)
    with pytest.raises(PrismError) as error:
        backend.align_layout(str(source), LAYOUT)
    assert error.value.code == 'UNSUPPORTED_LAYOUT_ALIGNMENT'


@pytest.mark.parametrize('operation', ['style', 'create', 'place', 'align', 'annotate'])
@pytest.mark.parametrize('fault', ['observations', 'analysis', 'unrelated_graph', 'native_failure', 'backup'])
def test_faults_preserve_source_and_fail_before_publication(backend, source, tmp_path, monkeypatch, no_native_control, operation, fault):
    table_id, graph_id = ids(source)
    if operation in ('place', 'align', 'annotate'):
        add_graph(source)
        add_layout(source, [graph_id, NEW_GRAPH] if operation == 'align' else [graph_id])
    original = source.read_bytes()
    before = read_project(source)

    def mutate(name, working):
        if fault == 'native_failure':
            raise PrismError('UI_TARGET_MISMATCH', 'Simulated native ownership failure')
        if fault == 'observations':
            member = f'data/tables/{before["tables"][0]["table_id"]}/data.csv'
            rewrite(working, lambda values: values.__setitem__(member, values[member].replace(b'1.23456789012345', b'99')))
        elif fault == 'analysis':
            member = f'analyses/{before["analyses"][0]["id"]}/parameters.json'
            change_json(working, member, lambda obj: obj['content']['calc'].update(sum=True))
        elif fault == 'unrelated_graph':
            change_json(working, f'graphs/{graph_id}/sheet.json', lambda obj: obj.update(title='Unexpected graph change'))
        elif fault == 'backup':
            (working.parent / 'original.prism').write_bytes(b'Unexpected checkpoint edit')

    FakeNative(monkeypatch, tmp_path, no_native_control, mutate=mutate)
    actions = {
        'style': lambda: backend.format_native_graph(str(source), graph_id, NativeGraphStyle(kind='violin')),
        'create': lambda: backend.create_graph(str(source), table_id, 'New summary'),
        'place': lambda: backend.place_graph(str(source), LAYOUT, NEW_GRAPH, 1, 2),
        'align': lambda: backend.align_layout(str(source), LAYOUT),
        'annotate': lambda: backend.annotate_layout(str(source), LAYOUT, AnnotationInput(text='a', x_inches=1, y_inches=2)),
    }
    with pytest.raises(PrismError) as error:
        actions[operation]()
    expected = {'observations': 'UNEXPECTED_DATA_CHANGE', 'analysis': 'UNEXPECTED_ANALYSIS_CHANGE',
                'unrelated_graph': 'GRAPH_LINK_MISMATCH', 'native_failure': 'UI_TARGET_MISMATCH', 'backup': 'CHECKPOINT_CHANGED'}
    assert error.value.code == expected[fault]
    assert source.read_bytes() == original
    record = backend.runtime.status(error.value.details['run_id'])
    assert record['status'] == 'failed'
    if fault != 'backup':
        assert (Path(record['directory']) / 'original.prism').read_bytes() == original


@pytest.mark.parametrize('fault', ['no_graph', 'two_graphs', 'wrong_binding'])
def test_creation_requires_one_new_graph_with_exact_original_inputs(backend, source, tmp_path, monkeypatch, no_native_control, fault):
    original = source.read_bytes()
    native = FakeNative(monkeypatch, tmp_path, no_native_control)

    def create(working):
        assert native.selected == ('D', 1)
        if fault != 'no_graph':
            add_graph(working)
        if fault == 'two_graphs':
            add_graph(working, identity=OTHER_GRAPH)
        elif fault == 'wrong_binding':
            change_json(working, f'graphs/{NEW_GRAPH}/sheet.json', lambda obj: obj['inputDataSets'].reverse())
        return {'test_only': True}

    monkeypatch.setattr(no_native_control, 'create_summary', create)
    with pytest.raises(PrismError) as error:
        backend.create_graph(str(source), ids(source)[0], 'New summary')
    assert error.value.code == ('UNSUPPORTED_NATIVE_GRAPH' if fault == 'wrong_binding' else 'UNEXPECTED_GRAPH_COUNT')
    assert source.read_bytes() == original
    assert not any('SetSheetTitle' in command for commands in native.commands for command in commands)


@pytest.mark.parametrize('fault', ['removed_link', 'added_media', 'other_layout'])
def test_placement_rejects_link_media_and_unselected_layout_changes(backend, source, tmp_path, monkeypatch, no_native_control, fault):
    _, first_graph = ids(source)
    add_graph(source)
    add_layout(source, [first_graph])
    add_layout(source, [first_graph], identity=OTHER_LAYOUT)
    original = source.read_bytes()

    def mutate(name, working):
        identity = OTHER_LAYOUT if fault == 'other_layout' else LAYOUT
        def change(obj):
            if fault == 'removed_link': obj['linkedGraphSheets'] = [NEW_GRAPH]
            elif fault == 'added_media': obj['mediaCollection'].append({})
            else: obj['title'] = 'Wrong layout'
        change_json(working, f'layouts/{identity}/sheet.json', change)

    FakeNative(monkeypatch, tmp_path, no_native_control, mutate=mutate)
    with pytest.raises(PrismError) as error:
        backend.place_graph(str(source), LAYOUT, NEW_GRAPH, 1, 2)
    assert error.value.code == 'LAYOUT_LINK_MISMATCH' and source.read_bytes() == original


@pytest.mark.parametrize('fault', ['cancel_before_ui', 'cancel_before_publish', 'external_revision'])
def test_cancellation_and_concurrent_revision_preserve_correct_source(backend, source, tmp_path, monkeypatch, no_native_control, fault):
    original = source.read_bytes()

    def on_script(op, commands):
        if fault == 'cancel_before_ui' and any(command.startswith('GoTo G, ') for command in commands):
            (op.directory / 'cancel.request').touch()

    def on_close(path):
        if fault == 'cancel_before_publish':
            (path.parent / 'cancel.request').touch()

    def mutate(name, working):
        if fault == 'external_revision': source.write_bytes(b'Concurrent independent revision')

    native = FakeNative(monkeypatch, tmp_path, no_native_control, on_script=on_script, mutate=mutate, on_close=on_close)
    with pytest.raises(PrismError) as error:
        backend.format_native_graph(str(source), ids(source)[1], NativeGraphStyle(kind='violin'))
    assert error.value.code == ('DOCUMENT_CHANGED' if fault == 'external_revision' else 'CANCELLED')
    assert source.read_bytes() == (b'Concurrent independent revision' if fault == 'external_revision' else original)
    assert len(native.calls) == (0 if fault == 'cancel_before_ui' else 1)


def test_missing_native_adapter_fails_structurally_without_dispatch(backend, source, monkeypatch):
    monkeypatch.delitem(sys.modules, 'prism_mcp.native_style')
    original_import = importlib.import_module

    def import_module(name, package=None):
        if name == '.native_style': raise ImportError('Test-only missing adapter')
        return original_import(name, package)

    monkeypatch.setattr(importlib, 'import_module', import_module)
    with pytest.raises(PrismError) as error:
        backend.format_native_graph(str(source), ids(source)[1], NativeGraphStyle(kind='violin'))
    assert error.value.code == 'NATIVE_ADAPTER_UNAVAILABLE'


def test_capabilities_distinguish_verified_tools_from_remaining_standalone_acceptance(backend, monkeypatch):
    monkeypatch.setattr(backend, '_configured', lambda: False)
    feature = backend.capabilities()['experimental']['native_graph_controls']
    assert feature['status'] == 'partially_native_verified_on_pinned_build'
    assert set(feature['tools']) == {'prism_format_native_graph', 'prism_create_graph', 'prism_place_graph', 'prism_align_layout'}


@pytest.mark.parametrize('name,args', [
    ('format_native_graph', ('/test.prism', 'graph', NativeGraphStyle(kind='violin'), 'a' * 64, 'request')),
    ('create_graph', ('/test.prism', 'table', 'New summary', 'median_iqr', GraphFormat(y_min=0), None, 'a' * 64, 'request')),
    ('place_graph', ('/test.prism', 'layout', 'graph', 1.0, 2.0, 100, 'a' * 64, 'request')),
    ('align_layout', ('/test.prism', 'layout', ['x', 'y'], 'a' * 64, 'request')),
])
def test_mcp_wrappers_forward_typed_inputs_and_structured_failure(backend, monkeypatch, name, args):
    server = importlib.import_module('prism_mcp.server')
    monkeypatch.setattr(server, 'backend', backend)
    received = []
    def called(*values):
        received.append(values)
        return {'ok': True, 'test_only': True}
    monkeypatch.setattr(backend, name, called)
    result = getattr(server, 'prism_' + name)(*args)
    assert isinstance(result, mcp_types.CallToolResult)
    assert received == [args] and not result.is_error and result.structured_content['test_only']
    def failed(*values): raise PrismError('UI_TARGET_MISMATCH', 'Test-only failure')
    monkeypatch.setattr(backend, name, failed)
    result = getattr(server, 'prism_' + name)(*args)
    assert result.is_error and result.structured_content['error']['code'] == 'UI_TARGET_MISMATCH'


def test_invalid_style_validation_is_json_serializable_through_mcp(backend, source, monkeypatch):
    server = importlib.import_module('prism_mcp.server')
    monkeypatch.setattr(server, 'backend', backend)
    result = server.prism_format_native_graph(str(source), ids(source)[1], {'kind': 'violin', 'colors': ['purple', '#4A4A4A']})
    assert result.is_error and result.structured_content['error']['code'] == 'INVALID_ARGUMENT'
    json.dumps(result.structured_content, allow_nan=False)


def test_creation_rejects_unsupported_kind_mismatched_style_and_unsafe_title(backend, source):
    table_id, _ = ids(source)
    for extra in [{'kind': 'violin'}, {'style': NativeGraphStyle(kind='violin')}, {'title': 'Bad" title'}, {'title': ''}]:
        values = {'title': 'Summary', **extra}
        with pytest.raises(PrismError) as error:
            backend.create_graph(str(source), table_id, **values)
        assert error.value.code == 'INVALID_ARGUMENT'


@pytest.mark.parametrize('field,value', [
    ('text', ''), ('text', 'x\ny'), ('font_size', 3.5), ('font_size', 72.5),
    ('font_size', 6.25), ('font_size', float('nan')), ('font_size', True),
    ('color', 'red'), ('color', '#1234'), ('bold', 1), ('italic', 'true'),
    ('horizontal_anchor', 'middle'), ('x_inches', float('inf')), ('y_inches', -1),
])
def test_annotation_rejects_invalid_text_style_and_coordinates(field, value):
    with pytest.raises(ValidationError):
        AnnotationInput.model_validate({'text': 'a', 'x_inches': 1, 'y_inches': 2, field: value})


@pytest.mark.parametrize('text,encoding', [('a', 'utf-8'), ('Δ elapsed time', 'utf-16-le')])
def test_annotation_forwards_style_and_verifies_final_text_without_media_requirement(backend, source, tmp_path, monkeypatch, no_native_control, text, encoding):
    _, graph_id = ids(source)
    add_layout(source, [graph_id])
    before, original = read_project(source), source.read_bytes()
    native = FakeNative(monkeypatch, tmp_path, no_native_control)
    native.annotation_encoding = encoding
    annotation = AnnotationInput(text=text, x_inches=1.25, y_inches=2.75,
        font_size=6.5, color='#aabbcc', bold=True, italic=True, horizontal_anchor='center')
    result = backend.annotate_layout(str(source), LAYOUT, annotation, request_id='label-one')
    after = read_project(source)
    assert after['tables'] == before['tables'] and after['graphs'] == before['graphs']
    assert after['layouts'] == before['layouts'] and after['layouts'][0]['media_count'] == 0
    call = native.calls[0]
    assert call[0] == 'annotate' and call[3] == ('L', 1)
    assert call[2][:3] == (text, 1.25, 2.75)
    assert call[2][4] == {'font_size': 6.5, 'color': '#aabbcc', 'bold': True, 'italic': True, 'horizontal_anchor': 'center'}
    directory = Path(backend.runtime.status(result['run_id'])['directory'])
    assert (directory / 'original.prism').read_bytes() == original
    assert result['native_annotation']['selection_token'] == native.selection_token
    replay = backend.annotate_layout(str(source), LAYOUT, annotation, request_id='label-one')
    assert replay['idempotent_replay'] and len(native.calls) == 1


@pytest.mark.parametrize('fault', ['missing_receipt', 'empty_receipt', 'utf8_placeholder', 'utf16_placeholder', 'missing_final_text', 'wrong_layout'])
def test_annotation_refuses_unfinished_text_replacement_and_preserves_source(backend, source, tmp_path, monkeypatch, no_native_control, fault):
    _, graph_id = ids(source)
    add_layout(source, [graph_id])
    add_layout(source, [graph_id], identity=OTHER_LAYOUT)
    original = source.read_bytes()
    native = FakeNative(monkeypatch, tmp_path, no_native_control)
    def annotate(*args, **kwargs):
        result = native.annotate(*args, **kwargs)
        working = args[0]
        member = f'layouts/{LAYOUT}/data.bin'
        if fault == 'missing_receipt':
            result.pop('selection_token')
        elif fault == 'empty_receipt':
            result['selection_token'] = ''
        elif fault in ('utf8_placeholder', 'utf16_placeholder'):
            encoding = 'utf-8' if fault == 'utf8_placeholder' else 'utf-16-le'
            rewrite(working, lambda values: values.__setitem__(member, values[member] + native.selection_token.encode(encoding)))
        elif fault == 'missing_final_text':
            rewrite(working, lambda values: values.__setitem__(member, b'No replacement text exists here'))
        elif fault == 'wrong_layout':
            change_json(working, f'layouts/{OTHER_LAYOUT}/sheet.json', lambda obj: obj.update(title='Wrong layout'))
        return result
    monkeypatch.setattr(sys.modules['prism_mcp.native_ui'], 'annotate', annotate)
    with pytest.raises(PrismError) as error:
        backend.annotate_layout(str(source), LAYOUT, AnnotationInput(text='Final label Ω', x_inches=1, y_inches=2))
    assert error.value.code == ('LAYOUT_LINK_MISMATCH' if fault == 'wrong_layout' else 'ANNOTATION_NOT_VERIFIED')
    assert source.read_bytes() == original


def test_annotation_revalidates_mutated_models_before_native_dispatch(backend, source, monkeypatch):
    annotation = AnnotationInput(text='a', x_inches=1, y_inches=2)
    annotation.font_size = 6.25
    server = importlib.import_module('prism_mcp.server')
    monkeypatch.setattr(server, 'backend', backend)
    result = server.prism_annotate_layout(str(source), LAYOUT, annotation)
    assert result.is_error and result.structured_content['error']['code'] == 'INVALID_ARGUMENT'
    json.dumps(result.structured_content, allow_nan=False)


@pytest.mark.parametrize('fault', ['boolean_number', 'precision'])
def test_style_detects_analysis_parameter_changes_hidden_by_float_equality(backend, source, tmp_path, monkeypatch, no_native_control, fault):
    before = read_project(source)
    member = f'analyses/{before["analyses"][0]["id"]}/parameters.json'
    if fault == 'precision':
        # JSON's binary float reader treats these neighboring decimal values as equal.
        rewrite(source, lambda values: values.__setitem__(member, values[member].replace(b'"confidenceLevel": 95.0', b'"confidenceLevel": 95.000000000000001')))
    original = source.read_bytes()
    def mutate(name, working):
        def change(values):
            payload = values[member]
            if fault == 'boolean_number':
                payload = payload.replace(b'"sum": false', b'"sum": 0')
            else:
                payload = payload.replace(b'95.000000000000001', b'95.000000000000002')
            assert payload != values[member], 'The fixture must exercise the intended JSON token'
            values[member] = payload
        rewrite(working, change)
    FakeNative(monkeypatch, tmp_path, no_native_control, mutate=mutate)
    with pytest.raises(PrismError) as error:
        backend.format_native_graph(str(source), before['graphs'][0]['id'], NativeGraphStyle(kind='violin'))
    assert error.value.code == 'UNEXPECTED_ANALYSIS_CHANGE' and source.read_bytes() == original


def fail_saved_checkpoint(monkeypatch, backend, fault):
    code = 'PRISM_CLEANUP_TIMEOUT' if fault == 'save' else 'UI_CLOSE_UNVERIFIED'

    def fail(*args, **kwargs):
        raise PrismError(code, 'Test-only saved checkpoint completion failure')

    if fault == 'close':
        monkeypatch.setattr(sys.modules['prism_mcp.native_ui'], 'close_saved_document', fail)
    else:
        helper = backend._save_after_native_ui

        def save_fails(op, *args):
            monkeypatch.setattr(op, 'script', fail)
            return helper(op, *args)

        monkeypatch.setattr(backend, '_save_after_native_ui', save_fails)
    monkeypatch.setattr(backend, '_publish', lambda *args: pytest.fail('Publication preceded verified save and close'))
    return code


@pytest.mark.parametrize('operation', ['style', 'create', 'place', 'align', 'resize', 'annotate'])
@pytest.mark.parametrize('fault', ['save', 'close'])
def test_post_ui_save_or_close_failure_prevents_publication(backend, source, tmp_path, monkeypatch, no_native_control, operation, fault):
    table_id, graph_id = ids(source)
    if operation in ('place', 'align', 'annotate'):
        add_graph(source)
        add_layout(source, [graph_id, NEW_GRAPH] if operation == 'align' else [graph_id])
    original = source.read_bytes()
    native = FakeNative(monkeypatch, tmp_path, no_native_control)
    expected_code = fail_saved_checkpoint(monkeypatch, backend, fault)
    actions = {
        'style': lambda: backend.format_native_graph(str(source), graph_id, NativeGraphStyle(kind='violin')),
        'create': lambda: backend.create_graph(str(source), table_id, 'New summary'),
        'place': lambda: backend.place_graph(str(source), LAYOUT, NEW_GRAPH, 1, 2),
        'align': lambda: backend.align_layout(str(source), LAYOUT),
        'resize': lambda: backend.resize_object(str(source), graph_id, 'graph', 2),
        'annotate': lambda: backend.annotate_layout(str(source), LAYOUT, AnnotationInput(text='a', x_inches=1, y_inches=2)),
    }
    with pytest.raises(PrismError) as error:
        actions[operation]()
    assert error.value.code == expected_code
    assert native.calls and not native.closed
    assert source.read_bytes() == original
    record = backend.runtime.status(error.value.details['run_id'])
    assert record['status'] == 'failed'
    assert 'native_checkpoint_close' not in record
