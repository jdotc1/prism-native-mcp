"""Import-renaming regression tests using test-owned archives and no native UI."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from prism_mcp import backend as module, runtime
from prism_mcp.archive import read_project
from prism_mcp.errors import PrismError
from prism_mcp.models import ColumnInput, GraphFormat, TableInput

from test_native_graph_controls import (
    FakeNative, LAYOUT, NEW_GRAPH, OTHER_GRAPH, OTHER_LAYOUT,
    add_graph, add_layout, backend, change_json, ids, no_native_control, rewrite, source,
)
from test_native_overlay_layout import SECOND_DATASETS, add_second_original_table


REPLACEMENT_GRAPH = '00000000-0000-4000-8000-000000000051'


@pytest.fixture(autouse=True)
def no_subprocesses(no_native_control, monkeypatch):
    def prohibited(*args, **kwargs):
        pytest.fail('Observation-update test attempted a native subprocess')
    monkeypatch.setattr(runtime.subprocess, 'run', prohibited)


@pytest.fixture
def update_source(source):
    _, first_graph = ids(source)
    change_json(source, f'graphs/{first_graph}/sheet.json',
                lambda obj: obj.update(title='Original violin 95% β'))
    add_second_original_table(source)
    add_graph(source, identity=OTHER_GRAPH, title='Unrelated graph 100%')
    change_json(source, f'graphs/{OTHER_GRAPH}/sheet.json',
                lambda obj: obj.update(inputDataSets=SECOND_DATASETS))
    add_graph(source, identity=NEW_GRAPH, title='Total session time 50% median IQR')
    add_layout(source, [OTHER_GRAPH, NEW_GRAPH, first_graph])
    add_layout(source, [NEW_GRAPH], identity=OTHER_LAYOUT)
    return source


def updated_table(source, visible_title=None):
    original = read_project(source)['tables'][0]
    # Reorder the same original observations so the genuine fixture's native
    # descriptive results remain valid. These mocks do not generate a graph.
    return TableInput(name='Updated observations 10%', columns=[
        ColumnInput(label=column['title'], values=list(reversed(column['values'])))
        for column in original['columns']
    ], graph=GraphFormat(title=visible_title))


class ImportRenamesGraphs(FakeNative):
    """Apply scripted archive effects only within the unit test's directory."""

    def __init__(self, *args, ignore_title=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ignore_title = ignore_title
        self.batches, self.events, self.imported, self.visible_titles = [], [], [], {}

    @staticmethod
    def text_argument(command):
        value = command.split(' ', 1)[1]
        assert value.startswith('"') and value.endswith('"')
        assert '"' not in value[1:-1]
        return value[1:-1].replace('%%', '%')

    def import_csv(self, command):
        assert self.selected[0] == 'D' and command.endswith(', 1, 1')
        csv_path = self.argument(command.removesuffix(', 1, 1'))
        with csv_path.open(newline='') as stream:
            header, *rows = list(csv.reader(stream))
        document = read_project(self.current)
        table = document['tables'][self.selected[1] - 1]
        assert len(header) == len(table['dataset_ids'])
        graphs = [graph for graph in document['graphs']
                  if set(graph['input_dataset_ids']) == set(table['dataset_ids'])]
        output = io.StringIO()
        csv.writer(output, lineterminator='\n').writerows(rows)

        def change(values):
            values[f'data/tables/{table["table_id"]}/data.csv'] = output.getvalue().encode()
            sheet_path = f'data/sheets/{table["id"]}/sheet.json'
            sheet = json.loads(values[sheet_path])
            sheet['title'] = csv_path.stem
            values[sheet_path] = json.dumps(sheet).encode()
            for identity, title in zip(table['dataset_ids'], header, strict=True):
                member = f'data/sets/{identity}.json'
                dataset = json.loads(values[member])
                dataset['title'] = title
                values[member] = json.dumps(dataset).encode()
            for graph in graphs:
                member = f'graphs/{graph["id"]}/sheet.json'
                graph_sheet = json.loads(values[member])
                graph_sheet['title'] = f'{csv_path.stem} automatic graph {graph["index"]}'
                values[member] = json.dumps(graph_sheet).encode()

        rewrite(self.current, change)
        self.imported.append({'table_id': table['id'], 'rows': rows, 'renamed': [g['id'] for g in graphs]})
        self.events.append(('import', table['id']))

    def script(self, operation, commands, *, expected=(), **kwargs):
        self.batches.append(list(commands))
        for command in commands:
            if command.startswith('Import '):
                self.import_csv(command)
            elif command.startswith('SetSheetTitle '):
                collection = 'tables' if self.selected[0] == 'D' else 'graphs'
                obj = read_project(self.current)[collection][self.selected[1] - 1]
                title = self.text_argument(command)
                self.events.append(('sheet_title', self.selected, obj['id'], title, command))
                if obj['id'] != self.ignore_title:
                    prefix = 'data/sheets' if collection == 'tables' else 'graphs'
                    change_json(self.current, f'{prefix}/{obj["id"]}/sheet.json', lambda row: row.update(title=title))
            elif command.startswith('SetGraphTitle '):
                assert self.selected[0] == 'G'
                graph = read_project(self.current)['graphs'][self.selected[1] - 1]
                title = self.text_argument(command)
                self.visible_titles[graph['id']] = title
                self.events.append(('visible_title', self.selected, graph['id'], title))
            elif command == 'ClearTable':
                assert self.selected[0] == 'D'
            else:
                super().script(operation, [command], **kwargs)
        assert all(path.is_file() and path.stat().st_size for path in expected)
        return {'status': 'succeeded', 'test_only_import_adapter': True}


@pytest.mark.parametrize('visible_title', [None, 'Independent visible header 25% β'])
def test_import_restores_all_bound_sheet_titles_before_optional_visible_formatting(
        backend, update_source, tmp_path, monkeypatch, no_native_control, visible_title):
    original = update_source.read_bytes()
    before = read_project(update_source)
    table = updated_table(update_source, visible_title)
    bound = [before['graphs'][0], before['graphs'][2]]
    native = ImportRenamesGraphs(monkeypatch, tmp_path, no_native_control)

    result = backend.update_table(str(update_source), before['tables'][0]['id'], table,
                                  expected_sha256=before['sha256'])

    after = read_project(update_source)
    assert result['ok'] and after['graphs'] == before['graphs']
    assert after['layouts'] == before['layouts']
    assert after['tables'][1] == before['tables'][1]
    assert after['tables'][0]['title'] == table.name
    assert [column['values'] for column in after['tables'][0]['columns']] == [column.values for column in table.columns]
    assert after['tables'][0]['columns'] != before['tables'][0]['columns']
    assert len(native.imported) == 1
    assert native.imported[0]['renamed'] == [graph['id'] for graph in bound]
    restores = [event for event in native.events if event[0] == 'sheet_title' and event[1][0] == 'G']
    assert [(event[1], event[2], event[3]) for event in restores] == [
        (('G', graph['index']), graph['id'], graph['title']) for graph in bound]
    assert all('%%' in event[4] for event in restores)
    assert all(event[3] != visible_title for event in restores)
    assert native.events[0] == ('import', before['tables'][0]['id'])
    assert native.events[1][0:4] == ('sheet_title', ('D', 1), before['tables'][0]['id'], table.name)
    if visible_title is None:
        assert not native.visible_titles
    else:
        assert native.visible_titles == {graph['id']: visible_title for graph in bound}
        for restore in restores:
            visible = ('visible_title', restore[1], restore[2], visible_title)
            assert native.events.index(restore) < native.events.index(visible)
    checkpoint = Path(backend.runtime.status(result['run_id'])['directory'])
    assert (checkpoint / 'original.prism').read_bytes() == original
    assert (checkpoint / 'original.prism') not in native.opened
    assert update_source not in native.saved_from


def corrupt_saved_graphs(path, fault):
    if fault == 'binding_change':
        change_json(path, f'graphs/{NEW_GRAPH}/sheet.json', lambda obj: obj.update(inputDataSets=SECOND_DATASETS))
    elif fault == 'binding_order':
        change_json(path, f'graphs/{NEW_GRAPH}/sheet.json', lambda obj: obj['inputDataSets'].reverse())
    elif fault == 'graph_order':
        change_json(path, 'document.json', lambda obj: obj['sheets']['graphs'].reverse())
    elif fault == 'graph_identity':
        layouts = read_project(path)['layouts']
        add_graph(path, identity=REPLACEMENT_GRAPH)
        def replace_id(obj):
            obj['sheets']['graphs'] = [REPLACEMENT_GRAPH if identity == NEW_GRAPH else identity
                                      for identity in obj['sheets']['graphs'][:-1]]
        change_json(path, 'document.json', replace_id)
        # Keep the simulated archive internally valid so the graph-identity
        # preservation guard, rather than dangling-link parsing, rejects it.
        for layout in layouts:
            change_json(path, f'layouts/{layout["id"]}/sheet.json', lambda obj: obj.update(
                linkedGraphSheets=[REPLACEMENT_GRAPH if identity == NEW_GRAPH else identity
                                   for identity in obj['linkedGraphSheets']]))
    elif fault == 'unrelated_title':
        change_json(path, f'graphs/{OTHER_GRAPH}/sheet.json', lambda obj: obj.update(title='Unexpected unrelated rename'))
    elif fault == 'layout_links':
        change_json(path, f'layouts/{LAYOUT}/sheet.json', lambda obj: obj['linkedGraphSheets'].reverse())
    elif fault == 'layout_order':
        change_json(path, 'document.json', lambda obj: obj['sheets']['layouts'].reverse())


@pytest.mark.parametrize('fault,code', [
    ('title_not_retained', 'GRAPH_LINK_MISMATCH'),
    ('binding_change', 'GRAPH_LINK_MISMATCH'), ('binding_order', 'GRAPH_LINK_MISMATCH'),
    ('graph_order', 'UNEXPECTED_GRAPH_CHANGE'), ('graph_identity', 'UNEXPECTED_GRAPH_CHANGE'),
    ('unrelated_title', 'GRAPH_LINK_MISMATCH'),
    ('layout_links', 'LAYOUT_LINK_MISMATCH'), ('layout_order', 'LAYOUT_LINK_MISMATCH'),
])
def test_import_preservation_failures_stop_before_publication(
        backend, update_source, tmp_path, monkeypatch, no_native_control, fault, code):
    original = update_source.read_bytes()
    before = read_project(update_source)
    native = ImportRenamesGraphs(monkeypatch, tmp_path, no_native_control,
        ignore_title=NEW_GRAPH if fault == 'title_not_retained' else None,
        on_save=lambda operation, path, count: corrupt_saved_graphs(path, fault))
    published = []

    def publish(*args, **kwargs):
        published.append(True)
        pytest.fail('Graph or layout preservation failed before publication')

    monkeypatch.setattr(backend, '_publish', publish)
    with pytest.raises(PrismError) as error:
        backend.update_table(str(update_source), before['tables'][0]['id'],
                             updated_table(update_source, 'Requested visible title'))
    assert error.value.code == code
    assert not published and update_source.read_bytes() == original
    operation = backend.runtime.status(error.value.details['run_id'])
    checkpoint = Path(operation['directory'])
    assert operation['status'] == 'failed' and (checkpoint / 'working.prism').exists()
    assert (checkpoint / 'original.prism').read_bytes() == original
    assert len(native.imported) == 1
