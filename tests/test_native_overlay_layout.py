"""Overlay publication guards with synthetic test archives and no native UI."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import get_type_hints

from mcp import types as mcp_types
import pytest
from pydantic import TypeAdapter, ValidationError

from prism_mcp import backend as module
from prism_mcp.archive import read_project
from prism_mcp.errors import PrismError
from prism_mcp.models import NativeOverlayPanel

from test_native_graph_controls import (
    FakeNative, LAYOUT, NEW_GRAPH, OTHER_GRAPH, OTHER_LAYOUT, add_graph, add_layout,
    backend, change_json, fail_saved_checkpoint, ids, no_native_control, rewrite, source,
)


FOURTH_GRAPH = '00000000-0000-4000-8000-000000000041'
FIFTH_GRAPH = '00000000-0000-4000-8000-000000000042'
SECOND_SHEET = '00000000-0000-4000-8000-000000000043'
SECOND_TABLE = '00000000-0000-4000-8000-000000000044'
SECOND_DATASETS = ['00000000-0000-4000-8000-000000000045', '00000000-0000-4000-8000-000000000046']
SECOND_ANALYSIS = '00000000-0000-4000-8000-000000000047'


@pytest.fixture(autouse=True)
def no_native_overlay(no_native_control, monkeypatch):
    def prohibited(*args, **kwargs):
        pytest.fail('Overlay test attempted actual native composition')
    monkeypatch.setattr(no_native_control, 'compose_overlay_layout', prohibited, raising=False)
    return no_native_control


@pytest.fixture
def overlay_source(source):
    _, violin = ids(source)
    add_graph(source, title='First native summary')
    add_graph(source, identity=OTHER_GRAPH, title='Second native violin')
    add_graph(source, identity=FOURTH_GRAPH, title='Second native summary')
    add_layout(source, [violin], media_count=1)
    add_layout(source, [violin], identity=OTHER_LAYOUT)
    return source


@pytest.fixture
def panels(overlay_source):
    return [
        NativeOverlayPanel(violin_id=ids(overlay_source)[1], summary_id=NEW_GRAPH,
            selection_label='First visible header', x_inches=1, y_inches=2),
        NativeOverlayPanel(violin_id=OTHER_GRAPH, summary_id=FOURTH_GRAPH,
            selection_label='Second visible header', x_inches=3, y_inches=2, scale_percent=90),
    ]


def add_second_original_table(path):
    """Clone only a test fixture's observations into another original table."""
    document_before = read_project(path)
    original = document_before['tables'][0]
    original_analysis = document_before['analyses'][0]['id']
    def change(values):
        document = json.loads(values['document.json'])
        document['sheets']['data'].append(SECOND_SHEET)
        document['sheets']['analyses'].append(SECOND_ANALYSIS)
        values['document.json'] = json.dumps(document).encode()
        sheet = json.loads(values[f'data/sheets/{original["id"]}/sheet.json'])
        sheet.update(uid=SECOND_SHEET, title='Second original observations')
        sheet['table'].update(uid=SECOND_TABLE, dataSets=SECOND_DATASETS)
        values[f'data/sheets/{SECOND_SHEET}/sheet.json'] = json.dumps(sheet).encode()
        for old_id, new_id in zip(original['dataset_ids'], SECOND_DATASETS, strict=True):
            dataset = json.loads(values[f'data/sets/{old_id}.json'])
            dataset['uid'] = new_id
            values[f'data/sets/{new_id}.json'] = json.dumps(dataset).encode()
        for name in ('content.json', 'data.csv', 'data.dt'):
            values[f'data/tables/{SECOND_TABLE}/{name}'] = values[f'data/tables/{original["table_id"]}/{name}']
        # Synthetic coverage for these identical test observations, not a
        # claimed native analysis or a deliverable scientific graph.
        analysis = json.loads(values[f'analyses/{original_analysis}/sheet.json'])
        analysis.update(uid=SECOND_ANALYSIS, resultSheets=[], inputSheets=[{'uid': SECOND_SHEET}])
        analysis.pop('currentResultSheet', None)
        for column, identity in zip(analysis['inputDataSets'], SECOND_DATASETS, strict=True):
            column['uid'] = identity
        values[f'analyses/{SECOND_ANALYSIS}/sheet.json'] = json.dumps(analysis).encode()
        values[f'analyses/{SECOND_ANALYSIS}/parameters.json'] = values[f'analyses/{original_analysis}/parameters.json']
        results = json.loads(values[f'analyses/{original_analysis}/results.json'])
        mapping = dict(zip(original['dataset_ids'], SECOND_DATASETS, strict=True))
        for sample in results['content']['samples'].values():
            sample['id']['col']['uid'] = mapping[sample['id']['col']['uid']]
        values[f'analyses/{SECOND_ANALYSIS}/results.json'] = json.dumps(results).encode()
    rewrite(path, change)
    assert len(read_project(path)['tables']) == 2


class OverlayNative(FakeNative):
    def __init__(self, monkeypatch, tmp_path, adapter, **kwargs):
        super().__init__(monkeypatch, tmp_path, adapter, **kwargs)
        monkeypatch.setattr(adapter, 'compose_overlay_layout', self.compose_overlay_layout)

    def compose_overlay_layout(self, path, panels, evidence):
        assert self.selected[0] == 'L' and evidence == path.parent
        document = read_project(path)
        selected = document['layouts'][self.selected[1] - 1]
        graph_ids = []
        for panel in panels:
            assert set(panel) == {'violin_index', 'violin_sheet_title', 'summary_index', 'summary_sheet_title',
                                  'selection_label', 'x_inches', 'y_inches', 'scale_percent'}
            for role in ('violin', 'summary'):
                graph = document['graphs'][panel[role + '_index'] - 1]
                assert graph['title'] == panel[role + '_sheet_title']
                graph_ids.append(graph['id'])
        change_json(path, f'layouts/{selected["id"]}/sheet.json',
                    lambda obj: obj.update(linkedGraphSheets=graph_ids, mediaCollection=[]))
        rewrite(path, lambda values: values.__setitem__(f'layouts/{selected["id"]}/data.bin',
                b'PCFFGRA4 test-only composition; this is not a native figure'))
        return self.call('compose_overlay_layout', path, (panels, evidence))


@pytest.mark.parametrize('field,value', [
    ('violin_id', 'not-a-uuid'), ('summary_id', 123),
    ('summary_id', '00000000000040008000000000000041'),
    ('summary_id', '{00000000-0000-4000-8000-000000000041}'),
    ('selection_label', 'abc'), ('selection_label', '    '), ('selection_label', 'a"bc'),
    ('selection_label', 'a\nbc'), ('x_inches', -.1), ('y_inches', 30.1),
    ('x_inches', float('nan')), ('y_inches', float('inf')), ('x_inches', True),
    ('scale_percent', 0), ('scale_percent', 1001), ('scale_percent', 100.0),
    ('scale_percent', '100'), ('scale_percent', False), ('unknown', 'value'),
])
def test_overlay_panel_schema_rejects_invalid_ids_headers_and_placement(panels, field, value):
    with pytest.raises(ValidationError):
        NativeOverlayPanel.model_validate({**panels[0].model_dump(), field: value})


@pytest.mark.parametrize('fault', ['one', 'three', 'tuple', 'same_pair_graph', 'reused_graph', 'same_casefold_header'])
def test_overlay_requires_exactly_two_distinct_pairs_and_headers(backend, overlay_source, panels, fault):
    values = [panel.model_dump() for panel in panels]
    if fault == 'one': values.pop()
    elif fault == 'three': values.append(dict(values[0]))
    elif fault == 'tuple': values = tuple(values)
    elif fault == 'same_pair_graph': values[0]['summary_id'] = values[0]['violin_id'].lower()
    elif fault == 'reused_graph': values[1]['summary_id'] = values[0]['violin_id']
    elif fault == 'same_casefold_header': values[1]['selection_label'] = values[0]['selection_label'].upper()
    original = overlay_source.read_bytes()
    with pytest.raises(PrismError) as error:
        backend.compose_overlay_layout(str(overlay_source), LAYOUT, values)
    assert error.value.code == 'INVALID_ARGUMENT' and overlay_source.read_bytes() == original
    json.dumps(error.value.as_dict(), allow_nan=False)


@pytest.mark.parametrize('opened,reversed_panels', [(False, False), (True, True)])
def test_composition_uses_original_pairs_and_replaces_only_selected_layout(backend, overlay_source, panels, tmp_path, monkeypatch, no_native_overlay, opened, reversed_panels):
    add_second_original_table(overlay_source)
    for identity in (OTHER_GRAPH, FOURTH_GRAPH):
        change_json(overlay_source, f'graphs/{identity}/sheet.json', lambda obj: obj.update(inputDataSets=SECOND_DATASETS))
    if reversed_panels:
        panels.reverse()
        panels[1].violin_id = panels[1].violin_id.lower()
    original, before = overlay_source.read_bytes(), read_project(overlay_source)
    native = OverlayNative(monkeypatch, tmp_path, no_native_overlay, opened=overlay_source if opened else None)
    result = backend.compose_overlay_layout(str(overlay_source), LAYOUT, panels,
                                            expected_sha256=before['sha256'], request_id='overlay-one')
    after = read_project(overlay_source)
    assert after['tables'] == before['tables'] and after['analyses'] == before['analyses'] and after['graphs'] == before['graphs']
    assert after['layouts'][1] == before['layouts'][1]
    assert set(after['layouts'][0]['linked_graph_ids']) == {graph['id'] for graph in before['graphs']}
    assert after['layouts'][0]['media_count'] == 0 and result['replaced_layout_contents']
    call = native.calls[0]
    assert call[0] == 'compose_overlay_layout' and call[3] == ('L', 1)
    assert [entry['violin_index'] for entry in call[2][0]] == ([3, 1] if reversed_panels else [1, 3])
    assert call[2][0][0]['selection_label'] == panels[0].selection_label
    assert call[2][0][0]['violin_sheet_title'] != panels[0].selection_label
    assert native.current == (overlay_source if opened else None)
    assert overlay_source not in native.saved_from and module.TEMPLATE not in native.opened
    directory = Path(backend.runtime.status(result['run_id'])['directory'])
    assert (directory / 'original.prism').read_bytes() == original and (directory / 'original.prism') not in native.opened
    checks = result['native_overlay_verification']
    assert checks['original_dataset_bindings_verified'] and checks['unselected_layouts_unchanged']
    assert checks['no_new_picture_or_media_members'] and not checks['saved_coordinates_and_grouping_decoded']
    assert checks['acceptance_status'] == 'native_controls_and_bindings_verified_visual_review_required'
    replay = backend.compose_overlay_layout(str(overlay_source), LAYOUT, panels,
                                            expected_sha256=before['sha256'], request_id='overlay-one')
    assert replay['idempotent_replay'] and len(native.calls) == 1


@pytest.mark.parametrize('fault', ['wrong_graph_id', 'wrong_layout_id', 'stale_revision', 'graph_class', 'graph_serialization', 'layout_class', 'different_original_table'])
def test_wrong_native_target_or_source_pair_stops_before_dispatch(backend, overlay_source, panels, fault):
    layout_id, revision = LAYOUT, None
    if fault == 'wrong_graph_id': panels[0].violin_id = read_project(overlay_source)['tables'][0]['id']
    elif fault == 'wrong_layout_id': layout_id = OTHER_GRAPH
    elif fault == 'stale_revision': revision = '0' * 64
    elif fault == 'graph_class': change_json(overlay_source, f'graphs/{NEW_GRAPH}/sheet.json', lambda obj: obj.update({'@class': 'UnobservedGraphSheet'}))
    elif fault == 'graph_serialization': rewrite(overlay_source, lambda values: values.__setitem__(f'graphs/{NEW_GRAPH}/data.bin', b'UNKNOWN! test-only'))
    elif fault == 'layout_class': change_json(overlay_source, f'layouts/{LAYOUT}/sheet.json', lambda obj: obj.update({'@class': 'UnobservedLayoutSheet'}))
    elif fault == 'different_original_table':
        add_second_original_table(overlay_source)
        change_json(overlay_source, f'graphs/{NEW_GRAPH}/sheet.json', lambda obj: obj.update(inputDataSets=SECOND_DATASETS))
    original = overlay_source.read_bytes()
    with pytest.raises(PrismError) as error:
        backend.compose_overlay_layout(str(overlay_source), layout_id, panels, expected_sha256=revision)
    expected = {'wrong_graph_id': 'OBJECT_NOT_FOUND', 'wrong_layout_id': 'OBJECT_NOT_FOUND',
        'stale_revision': 'DOCUMENT_CHANGED', 'graph_class': 'UNSUPPORTED_NATIVE_GRAPH',
        'graph_serialization': 'UNSUPPORTED_NATIVE_GRAPH', 'layout_class': 'UNSUPPORTED_NATIVE_LAYOUT',
        'different_original_table': 'GRAPH_LINK_MISMATCH'}
    assert error.value.code == expected[fault] and overlay_source.read_bytes() == original


@pytest.mark.parametrize('fault', [
    'missing_link', 'wrong_link', 'declared_media', 'other_layout_title', 'other_layout_binary',
    'other_layout_member', 'observations', 'analysis', 'graph_binding', 'graph_order',
    'backup', 'native_failure', 'picture', 'opaque_media', 'replaced_picture',
])
def test_composition_rejects_unrelated_changes_and_picture_substitution(backend, overlay_source, panels, tmp_path, monkeypatch, no_native_overlay, fault):
    if fault == 'wrong_link': add_graph(overlay_source, identity=FIFTH_GRAPH)
    if fault == 'replaced_picture': rewrite(overlay_source, lambda values: values.__setitem__('misc/retained.png', b'existing test-only asset'))
    original, before = overlay_source.read_bytes(), read_project(overlay_source)
    def mutate(name, working):
        if fault == 'native_failure': raise PrismError('UI_TARGET_MISMATCH', 'Test-only native ownership failure')
        elif fault == 'missing_link': change_json(working, f'layouts/{LAYOUT}/sheet.json', lambda obj: obj['linkedGraphSheets'].pop())
        elif fault == 'wrong_link': change_json(working, f'layouts/{LAYOUT}/sheet.json', lambda obj: obj['linkedGraphSheets'].__setitem__(-1, FIFTH_GRAPH))
        elif fault == 'declared_media': change_json(working, f'layouts/{LAYOUT}/sheet.json', lambda obj: obj['mediaCollection'].append({}))
        elif fault == 'other_layout_title': change_json(working, f'layouts/{OTHER_LAYOUT}/sheet.json', lambda obj: obj.update(title='Unintended layout'))
        elif fault in ('other_layout_binary', 'other_layout_member'):
            name = f'layouts/{OTHER_LAYOUT}/' + ('data.bin' if fault == 'other_layout_binary' else 'extra.txt')
            rewrite(working, lambda values: values.__setitem__(name, b'Unintended private drawing change'))
        elif fault == 'observations':
            member = f'data/tables/{before["tables"][0]["table_id"]}/data.csv'
            rewrite(working, lambda values: values.__setitem__(member, values[member].replace(b'1.23456789012345', b'99')))
        elif fault == 'analysis': change_json(working, f'analyses/{before["analyses"][0]["id"]}/parameters.json', lambda obj: obj['content']['calc'].update(sum=True))
        elif fault == 'graph_binding': change_json(working, f'graphs/{NEW_GRAPH}/sheet.json', lambda obj: obj['inputDataSets'].reverse())
        elif fault == 'graph_order': change_json(working, 'document.json', lambda obj: obj['sheets']['graphs'].reverse())
        elif fault == 'backup': (working.parent / 'original.prism').write_bytes(b'Unexpected original edit')
        else:
            name = {'picture': 'misc/imported.svg', 'opaque_media': 'media/picture.bin', 'replaced_picture': 'misc/retained.png'}[fault]
            rewrite(working, lambda values: values.__setitem__(name, b'Unexpected finished picture'))
    OverlayNative(monkeypatch, tmp_path, no_native_overlay, mutate=mutate)
    with pytest.raises(PrismError) as error:
        backend.compose_overlay_layout(str(overlay_source), LAYOUT, panels)
    expected = {'missing_link': 'LAYOUT_LINK_MISMATCH', 'wrong_link': 'LAYOUT_LINK_MISMATCH',
        'declared_media': 'LAYOUT_LINK_MISMATCH', 'other_layout_title': 'LAYOUT_LINK_MISMATCH',
        'other_layout_binary': 'UNEXPECTED_LAYOUT_CHANGE', 'other_layout_member': 'UNEXPECTED_LAYOUT_CHANGE',
        'observations': 'UNEXPECTED_DATA_CHANGE', 'analysis': 'UNEXPECTED_ANALYSIS_CHANGE',
        'graph_binding': 'GRAPH_LINK_MISMATCH', 'graph_order': 'UNEXPECTED_GRAPH_CHANGE',
        'backup': 'CHECKPOINT_CHANGED', 'native_failure': 'UI_TARGET_MISMATCH',
        'picture': 'UNEXPECTED_MEDIA_CHANGE', 'opaque_media': 'UNEXPECTED_MEDIA_CHANGE', 'replaced_picture': 'UNEXPECTED_MEDIA_CHANGE'}
    assert error.value.code == expected[fault] and overlay_source.read_bytes() == original
    assert backend.runtime.status(error.value.details['run_id'])['status'] == 'failed'


@pytest.mark.parametrize('fault', ['before_ui', 'before_publish', 'concurrent_revision'])
def test_overlay_cancellation_and_revision_guards(backend, overlay_source, panels, tmp_path, monkeypatch, no_native_overlay, fault):
    original = overlay_source.read_bytes()
    def on_script(op, commands):
        if fault == 'before_ui' and any(command.startswith('GoTo L, ') for command in commands): (op.directory / 'cancel.request').touch()
    def on_close(path):
        if fault == 'before_publish': (path.parent / 'cancel.request').touch()
    def mutate(name, working):
        if fault == 'concurrent_revision': overlay_source.write_bytes(b'Independent newer document')
    native = OverlayNative(monkeypatch, tmp_path, no_native_overlay, on_script=on_script, mutate=mutate, on_close=on_close)
    with pytest.raises(PrismError) as error:
        backend.compose_overlay_layout(str(overlay_source), LAYOUT, panels)
    assert error.value.code == ('DOCUMENT_CHANGED' if fault == 'concurrent_revision' else 'CANCELLED')
    assert overlay_source.read_bytes() == (b'Independent newer document' if fault == 'concurrent_revision' else original)
    assert len(native.calls) == (0 if fault == 'before_ui' else 1)


@pytest.mark.parametrize('fault', ['save', 'close'])
def test_overlay_save_or_close_failure_prevents_publication(backend, overlay_source, panels, tmp_path, monkeypatch, no_native_overlay, fault):
    original = overlay_source.read_bytes()
    native = OverlayNative(monkeypatch, tmp_path, no_native_overlay)
    expected_code = fail_saved_checkpoint(monkeypatch, backend, fault)
    with pytest.raises(PrismError) as error:
        backend.compose_overlay_layout(str(overlay_source), LAYOUT, panels)
    assert error.value.code == expected_code
    assert native.calls and not native.closed
    assert overlay_source.read_bytes() == original
    assert backend.runtime.status(error.value.details['run_id'])['status'] == 'failed'


def test_missing_composer_and_dirty_target_never_dispatch(backend, overlay_source, panels, monkeypatch, no_native_overlay):
    monkeypatch.delattr(no_native_overlay, 'compose_overlay_layout')
    with pytest.raises(PrismError) as error:
        backend.compose_overlay_layout(str(overlay_source), LAYOUT, panels)
    assert error.value.code == 'NATIVE_ADAPTER_UNAVAILABLE'
    monkeypatch.setattr(no_native_overlay, 'compose_overlay_layout', lambda *args: pytest.fail('Unexpected native call'), raising=False)
    monkeypatch.setattr(module, 'windows', lambda: [{'path': str(overlay_source), 'title': overlay_source.name, 'edited': True, 'modal': False}])
    with pytest.raises(PrismError) as error:
        backend.compose_overlay_layout(str(overlay_source), LAYOUT, panels)
    assert error.value.code == 'UNSAVED_CHANGES'


def test_overlay_mcp_schema_marks_replacement_and_forwards_typed_panels(backend, panels, monkeypatch):
    server = importlib.import_module('prism_mcp.server')
    monkeypatch.setattr(server, 'backend', backend)
    received = []
    def called(*args):
        received.append(args)
        return {'test_only': True}
    monkeypatch.setattr(backend, 'compose_overlay_layout', called)
    args = ('/test-only.prism', LAYOUT, panels, 'a' * 64, 'overlay-request')
    result = server.prism_compose_overlay_layout(*args)
    assert isinstance(result, mcp_types.CallToolResult) and not result.is_error and received == [args]
    assert server.REPLACE.destructive_hint and not server.REPLACE.open_world_hint
    schema = TypeAdapter(get_type_hints(server.prism_compose_overlay_layout, include_extras=True)['panels']).json_schema()
    assert schema['minItems'] == schema['maxItems'] == 2
    def failed(*args): raise PrismError('UI_TARGET_MISMATCH', 'Test-only native failure')
    monkeypatch.setattr(backend, 'compose_overlay_layout', failed)
    result = server.prism_compose_overlay_layout(*args)
    assert result.is_error and result.structured_content['error']['code'] == 'UI_TARGET_MISMATCH'


def test_overlay_revalidates_mutated_panels_and_keeps_capability_experimental(backend, overlay_source, panels, monkeypatch):
    panels[0].selection_label = 'abc'
    server = importlib.import_module('prism_mcp.server')
    monkeypatch.setattr(server, 'backend', backend)
    result = server.prism_compose_overlay_layout(str(overlay_source), LAYOUT, panels)
    assert result.is_error and result.structured_content['error']['code'] == 'INVALID_ARGUMENT'
    json.dumps(result.structured_content, allow_nan=False)
    monkeypatch.setattr(backend, '_configured', lambda: False)
    feature = backend.capabilities()['experimental']['native_overlay_composition']
    assert feature['status'] == 'native_data_driven_acceptance_verified_visual_review_required' and feature['tool'] == 'prism_compose_overlay_layout'
    assert 'annotations' in feature['scope']
