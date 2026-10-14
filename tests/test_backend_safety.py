"""Supplemental semantic safety tests; none dispatches Prism or mutates a source."""

from __future__ import annotations

import copy
from pathlib import Path
import zipfile

import pytest
from pydantic import ValidationError

from prism_mcp import backend as module
from prism_mcp import runtime
from prism_mcp.archive import read_project
from prism_mcp.backend import Backend, lookup
from prism_mcp.errors import PrismError
from prism_mcp.models import ColumnInput, GraphFormat, TableInput


@pytest.fixture(autouse=True)
def no_native_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "CONTROL_ROOT", tmp_path / "global-control")
    monkeypatch.setattr(runtime, "application_info", lambda: {
        "installed": True, "version_compatible": True, "path": "/test-only/Prism.app",
    })
    monkeypatch.setattr(runtime, "verify_running_application", lambda: "")
    monkeypatch.setattr(module, "windows", lambda: [])

    def prohibited(*args, **kwargs):
        pytest.fail("Safety unit test attempted native dispatch")

    monkeypatch.setattr(runtime.Operation, "script", prohibited)
    monkeypatch.setattr(runtime, "osa", prohibited)
    monkeypatch.setattr(runtime.subprocess, "Popen", prohibited)


@pytest.fixture
def backend(tmp_path):
    return Backend(tmp_path / "state")


@pytest.fixture
def native_document():
    return read_project(module.TEMPLATE)


def table_input(**extra):
    return TableInput(name="Timing", columns=[
        ColumnInput(label="No SIM", values=[1.1, 2.2, 3.3]),
        ColumnInput(label="SIM", values=[2.1, 3.2, 4.3]),
    ], **extra)


def actual_table(expected):
    return {"id": "table-id", "title": expected.name, "columns": [
        {"id": f"dataset-{index}", "title": column.label, "values": list(column.values)}
        for index, column in enumerate(expected.columns)
    ]}


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), -float("inf"), "1.5", [], {}])
def test_observations_reject_nonnumeric_nonfinite_and_boolean_values(value):
    with pytest.raises(ValidationError):
        ColumnInput(label="Group", values=[1, 2, value])


@pytest.mark.parametrize("field", ["y_min", "y_max", "y_interval"])
def test_axis_settings_reject_boolean_numbers(field):
    with pytest.raises(ValidationError):
        GraphFormat(**{field: True})


def test_measurement_resolution_rejects_boolean_value():
    with pytest.raises(ValidationError):
        table_input(measurement_resolution=True)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, 0.0])
def test_measurement_resolution_must_be_positive_and_finite(value):
    with pytest.raises(ValidationError):
        table_input(measurement_resolution=value)


def test_arbitrary_size_integer_becomes_validation_error_instead_of_runtime_crash():
    with pytest.raises(ValidationError):
        ColumnInput(label="Group", values=[1, 2, 10**400])


def test_integer_precision_cannot_be_lost_during_input_coercion():
    with pytest.raises(ValidationError):
        ColumnInput(label="Group", values=[1, 2, 2**53 + 1])


def test_source_id_alignment_and_minimum_violin_count_are_validated():
    with pytest.raises(ValidationError, match="align"):
        ColumnInput(label="Group", values=[1, 2, 3], study_ids=["S001"])
    with pytest.raises(ValidationError, match="three observations"):
        TableInput(name="Timing", columns=[
            ColumnInput(label="A", values=[1, None, 3]),
            ColumnInput(label="B", values=[2, 3, 4]),
        ])


def test_saved_numeric_change_rejected_without_original_resolution():
    expected = table_input()
    actual = actual_table(expected)
    actual["columns"][0]["values"][0] += 1e-12
    with pytest.raises(PrismError) as error:
        Backend._verify_table(actual, expected)
    assert error.value.code == "PRECISION_OR_DATA_MISMATCH"
    assert error.value.details["allowed_error"] == 0


def test_original_measurement_resolution_allows_only_explicit_subresolution_rounding():
    expected = table_input(measurement_resolution=1 / 60)
    actual = actual_table(expected)
    actual["columns"][0]["values"][0] += 1e-12
    report = Backend._verify_table(actual, expected)
    assert report[0]["exact_numeric_match"] is False
    assert 0 < report[0]["max_absolute_error"] < report[0]["allowed_error"]
    assert report[0]["measurement_resolution"] == 1 / 60
    actual["columns"][0]["values"][0] += 1e-7
    with pytest.raises(PrismError) as error:
        Backend._verify_table(actual, expected)
    assert error.value.code == "PRECISION_OR_DATA_MISMATCH"


def test_precision_check_preserves_interior_missing_value_positions():
    expected = table_input()
    expected.columns[0].values = [1.1, None, 2.2, 3.3]
    actual = actual_table(expected)
    actual["columns"][0]["values"] = [1.1, 2.2, None, 3.3]
    with pytest.raises(PrismError) as error:
        Backend._verify_table(actual, expected)
    assert error.value.code == "PRECISION_OR_DATA_MISMATCH"


def test_uuid_lookup_is_scoped_to_requested_object_type(native_document):
    graph_id = native_document["graphs"][0]["id"]
    assert lookup(native_document, "graphs", graph_id)["id"] == graph_id
    with pytest.raises(PrismError) as error:
        lookup(native_document, "tables", graph_id)
    assert error.value.code == "OBJECT_NOT_FOUND"


def test_wrong_table_uuid_fails_before_native_dispatch(backend, native_document, tmp_path):
    target = tmp_path / "existing.prism"
    target.write_bytes(module.TEMPLATE.read_bytes())
    before = target.read_bytes()
    with pytest.raises(PrismError) as error:
        backend.update_table(str(target), native_document["graphs"][0]["id"], table_input())
    assert error.value.code == "OBJECT_NOT_FOUND"
    assert target.read_bytes() == before
    status = backend.runtime.status(error.value.details["run_id"])
    assert status["status"] == "failed"
    assert status["steps"] == []


@pytest.mark.parametrize("missing", ["all_analyses", "all_summaries", "one_dataset", "uncovered_table"])
def test_native_summary_verification_requires_complete_observation_coverage(native_document, missing):
    doc = copy.deepcopy(native_document)
    if missing == "all_analyses":
        doc["analyses"] = []
    elif missing == "all_summaries":
        doc["analyses"][0]["summaries"] = []
    elif missing == "one_dataset":
        doc["analyses"][0]["summaries"].pop()
    else:
        additional = copy.deepcopy(doc["tables"][0])
        additional["id"] = "additional-observation-table"
        for index, column in enumerate(additional["columns"]):
            column["id"] = f"uncovered-dataset-{index}"
        doc["tables"].append(additional)
    with pytest.raises(PrismError) as error:
        Backend._verify_summaries(doc)
    assert error.value.code == "MISSING_NATIVE_RESULTS"


def test_summary_must_agree_with_saved_measurements(native_document):
    doc = copy.deepcopy(native_document)
    doc["tables"][0]["columns"][0]["values"][0] += 50
    with pytest.raises(PrismError) as error:
        Backend._verify_summaries(doc)
    assert error.value.code == "STALE_NATIVE_RESULTS"


def test_idempotent_replay_does_not_repeat_action_and_conflicting_inputs_fail(backend):
    calls = []

    def action(operation):
        calls.append(operation.run_id)
        return {"value": 1}

    first = backend._perform("test-only", {"value": 1}, action, request_id="repeatable")
    repeated = backend._perform("test-only", {"value": 1}, action, request_id="repeatable")
    assert repeated["run_id"] == first["run_id"]
    assert repeated["idempotent_replay"]
    assert len(calls) == 1
    with pytest.raises(PrismError) as error:
        backend._perform("test-only", {"value": 2}, action, request_id="repeatable")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(calls) == 1


def test_failed_request_is_not_retried_and_checkpoint_is_recorded(backend):
    calls = []

    def action(operation):
        calls.append(operation.run_id)
        raise RuntimeError("simulated process failure")

    with pytest.raises(PrismError) as first:
        backend._perform("test-only", {"value": 1}, action, request_id="failed-once")
    assert first.value.code == "OPERATION_FAILED"
    checkpoint = Path(first.value.details["checkpoint"])
    assert (checkpoint / "operation.json").is_file()
    with pytest.raises(PrismError) as second:
        backend._perform("test-only", {"value": 1}, action, request_id="failed-once")
    assert second.value.code == "PREVIOUS_OPERATION_FAILED"
    assert len(calls) == 1


def test_stale_existing_export_directory_fails_before_dispatch(backend, tmp_path):
    output = tmp_path / "old-exports"
    output.mkdir()
    (output / "graph-1.pdf").write_bytes(b"%PDF-1.7 old file")
    with pytest.raises(PrismError) as error:
        backend.export(str(tmp_path / "source.prism"), str(output))
    assert error.value.code == "OUTPUT_EXISTS"
    assert (output / "graph-1.pdf").read_bytes() == b"%PDF-1.7 old file"


def test_save_as_refuses_existing_unrelated_destination(backend, tmp_path):
    source = tmp_path / "source.prism"
    source.write_bytes(module.TEMPLATE.read_bytes())
    destination = tmp_path / "other.prism"
    destination.write_bytes(b"unrelated document")
    with pytest.raises(PrismError) as error:
        backend.save_document(str(source), str(destination))
    assert error.value.code == "TARGET_EXISTS"
    assert destination.read_bytes() == b"unrelated document"


@pytest.mark.parametrize("creating", [True, False])
def test_publication_preserves_destination_created_or_changed_during_staging(backend, tmp_path, monkeypatch, creating):
    target = tmp_path / "target.prism"
    if not creating:
        target.write_bytes(module.TEMPLATE.read_bytes())
    previous_hash = None if creating else runtime.digest(target)
    operation = backend.runtime.operation("test-only", {"path": str(target)})
    working = operation.directory / "working.prism"
    working.write_bytes(module.TEMPLATE.read_bytes())
    original_copy = module.shutil.copy2
    external_change = b"independent writer's later document revision"

    def competing_writer(source, destination, *args, **kwargs):
        result = original_copy(source, destination, *args, **kwargs)
        target.write_bytes(external_change)
        return result

    monkeypatch.setattr(module.shutil, "copy2", competing_writer)
    with pytest.raises(PrismError) as error:
        backend._publish(operation, working, target, previous_hash, False)
    assert error.value.code == ("TARGET_EXISTS" if creating else "DOCUMENT_CHANGED")
    assert target.read_bytes() == external_change


@pytest.fixture
def writable_seed(monkeypatch, tmp_path):
    """Keep even a deliberately misbehaving fake writer inside the test directory."""
    seed = tmp_path / 'seed.prism'
    seed.write_bytes(module.TEMPLATE.read_bytes())
    monkeypatch.setattr(module, 'TEMPLATE', seed)
    return seed


def native_save_as_autosaving_input(monkeypatch, tmp_path, *, initially_open=None, on_save=None):
    """Simulate the observed disk-write side effect, with no application control."""
    state = {'current': initially_open, 'opened': [], 'saved_from': [], 'commands': []}

    def windows():
        current = state['current']
        return [] if current is None else [{
            'path': str(current), 'title': current.name, 'edited': False, 'modal': False,
        }]

    def path_argument(command):
        argument = command.split(' ', 1)[1]
        assert argument.startswith('"') and argument.endswith('"')
        path = Path(argument[1:-1].replace('%%', '%'))
        assert path.is_relative_to(tmp_path), 'Fake native writer escaped its temporary test directory'
        return path

    def script(operation, commands, *, expected=(), **kwargs):
        state['commands'].append(list(commands))
        for command in commands:
            if command.startswith('Open '):
                current = path_argument(command)
                assert current.is_file()
                state['current'] = current
                state['opened'].append(current)
            elif command.startswith('Save '):
                destination = path_argument(command)
                current = state['current']
                assert current is not None and current.is_relative_to(tmp_path)
                state['saved_from'].append(current)
                # An archive comment changes saved bytes while preserving all
                # measured values and genuine native result/graph structures.
                with zipfile.ZipFile(current, 'a') as archive:
                    archive.comment = f'simulated native autosave {len(state["saved_from"])}'.encode()
                destination.write_bytes(current.read_bytes())
                state['current'] = destination
                if on_save is not None:
                    on_save(current, destination)
            elif command == 'Close':
                state['current'] = None
        assert all(path.is_file() and path.stat().st_size for path in expected)
        return {'status': 'succeeded', 'test_only_native_adapter': True}

    monkeypatch.setattr(module, 'windows', windows)
    monkeypatch.setattr(runtime.Operation, 'script', script)
    return state


@pytest.mark.parametrize('was_open', [False, True])
def test_save_as_preserves_source_and_unopened_original_checkpoint_when_prism_autosaves_input(
        backend, tmp_path, monkeypatch, writable_seed, was_open):
    source = tmp_path / 'source.prism'
    source.write_bytes(writable_seed.read_bytes())
    original = source.read_bytes()
    target = tmp_path / 'saved copy.prism'
    native = native_save_as_autosaving_input(monkeypatch, tmp_path, initially_open=source if was_open else None)

    result = backend.save_document(str(source), str(target))

    operation = backend.runtime.status(result['run_id'])
    checkpoint = Path(operation['directory'])
    assert result['ok'] and result['source_preserved']
    assert result['source_window_was_open'] is was_open
    assert source.read_bytes() == writable_seed.read_bytes() == original
    assert (checkpoint / 'original.prism').read_bytes() == original
    assert (checkpoint / 'original.prism') not in native['opened']
    assert writable_seed not in native['opened']
    assert source not in native['saved_from']
    assert native['saved_from'] == [checkpoint / 'input.prism']
    assert (checkpoint / 'input.prism').read_bytes() != original
    assert target.read_bytes() == (checkpoint / 'working.prism').read_bytes()
    assert native['current'] == target
    assert operation['input_document_sha256'] == result['save_as_source_sha256'] == runtime.digest(source)


def test_creation_preserves_seed_and_original_checkpoint_when_first_save_autosaves_input(
        backend, tmp_path, monkeypatch, writable_seed):
    original = writable_seed.read_bytes()
    seed_table = read_project(writable_seed)['tables'][0]
    table = TableInput(name=seed_table['title'], columns=[
        ColumnInput(label=column['title'], values=column['values']) for column in seed_table['columns']
    ])
    target = tmp_path / 'created.prism'
    native = native_save_as_autosaving_input(monkeypatch, tmp_path)

    result = backend.create_document(str(target), [table], layout=False)

    operation = backend.runtime.status(result['run_id'])
    checkpoint = Path(operation['directory'])
    assert result['ok']
    assert writable_seed.read_bytes() == original
    assert (checkpoint / 'original.prism').read_bytes() == original
    assert writable_seed not in native['opened']
    assert (checkpoint / 'original.prism') not in native['opened']
    assert native['saved_from'][0] == checkpoint / 'input.prism'
    assert (checkpoint / 'input.prism').read_bytes() != original
    assert operation['template_sha256'] == operation['input_document_sha256'] == runtime.digest(writable_seed)
    assert read_project(target)['tables'] == read_project(writable_seed)['tables']


@pytest.mark.parametrize('edited', [True, None])
def test_save_as_refuses_dirty_or_unknown_source_state_before_copy_or_dispatch(
        backend, tmp_path, monkeypatch, writable_seed, edited):
    source = tmp_path / 'source.prism'
    source.write_bytes(writable_seed.read_bytes())
    original = source.read_bytes()
    target = tmp_path / 'saved-copy.prism'
    monkeypatch.setattr(module, 'windows', lambda: [{
        'path': str(source), 'title': source.name, 'edited': edited, 'modal': False,
    }])
    with pytest.raises(PrismError) as error:
        backend.save_document(str(source), str(target))
    assert error.value.code == 'UNSAVED_CHANGES'
    assert source.read_bytes() == original
    assert not target.exists()
    operation = backend.runtime.status(error.value.details['run_id'])
    assert operation['status'] == 'failed'
    assert operation['steps'] == []
    assert not (Path(operation['directory']) / 'input.prism').exists()


def test_save_as_detects_concurrent_source_change_before_publishing_copy(
        backend, tmp_path, monkeypatch, writable_seed):
    source = tmp_path / 'source.prism'
    source.write_bytes(writable_seed.read_bytes())
    original = source.read_bytes()
    changed = b'independent later source revision'
    target = tmp_path / 'saved-copy.prism'
    native_save_as_autosaving_input(monkeypatch, tmp_path, on_save=lambda *_: source.write_bytes(changed))

    with pytest.raises(PrismError) as error:
        backend.save_document(str(source), str(target))

    assert error.value.code == 'DOCUMENT_CHANGED'
    assert source.read_bytes() == changed
    assert not target.exists()
    operation = backend.runtime.status(error.value.details['run_id'])
    assert operation['status'] == 'failed'
    assert (Path(operation['directory']) / 'original.prism').read_bytes() == original
