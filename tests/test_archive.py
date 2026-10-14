"""Saved native fixture checks and deliberately damaged archive variants."""

from __future__ import annotations

import hashlib
import json
import statistics
import warnings
import zipfile
from pathlib import Path

import pytest

from prism_mcp import archive
from prism_mcp.archive import read_project, sha256_file

FIXTURE = Path(__file__).parents[1] / "src/prism_mcp/templates/column_violin_summary.prism"
ABSENT_ID = "00000000-0000-4000-8000-000000000001"
LAYOUT_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def members():
    with zipfile.ZipFile(FIXTURE) as source:
        return {info.filename: source.read(info) for info in source.infolist()}


def object_at(members, name):
    return json.loads(members[name])


def put_object(members, name, value):
    members[name] = json.dumps(value).encode()


def write_variant(tmp_path, members, name="variant.prism"):
    target = tmp_path / name
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as output:
        for member, data in members.items():
            output.writestr(member, data)
    return target


def primary_ids(members):
    document = object_at(members, "document.json")
    sheet_id = document["sheets"]["data"][0]
    sheet = object_at(members, f"data/sheets/{sheet_id}/sheet.json")
    return sheet_id, sheet["table"]["uid"], sheet["table"]["dataSets"]


def test_fixture_scopes_original_observations_and_preserves_precision():
    before = FIXTURE.read_bytes()
    model = read_project(FIXTURE)
    assert FIXTURE.read_bytes() == before
    assert model["sha256"] == hashlib.sha256(before).hexdigest() == sha256_file(FIXTURE)
    assert model["format_version"] == "1-6-1"
    assert model["prism_version"] == "11.1.0.218"
    assert model["relationships_valid"]
    assert len(model["tables"]) == len(model["analyses"]) == len(model["graphs"]) == 1
    assert not model["layouts"]
    table = model["tables"][0]
    assert table["index"] == 1
    assert table["data_format"] == "y_single"
    assert [column["id"] for column in table["columns"]] == table["dataset_ids"]
    assert table["columns"][0]["raw_values"][0] == "1.23456789012345"
    assert table["columns"][0]["values"][0] == 1.23456789012345
    assert model["graphs"][0]["serialization"] == "PCFFGRA4"
    assert model["graphs"][0]["input_dataset_ids"] == table["dataset_ids"]
    analysis = model["analyses"][0]
    assert analysis["input_sheet_ids"] == [table["id"]]
    assert analysis["parameters"]["content"]["calc"]["mean"] is True
    for column, summary in zip(table["columns"], analysis["summaries"], strict=True):
        assert summary["dataset_id"] == column["id"]
        assert summary["n"] == len(column["values"])
        assert summary["mean"] == pytest.approx(statistics.mean(column["values"]), abs=1e-13)
        assert summary["median"] == statistics.median(column["values"])


def test_metadata_only_omits_value_arrays_but_checks_data(tmp_path, members):
    model = read_project(FIXTURE, include_values=False)
    assert "values" not in model["tables"][0]["columns"][0]
    _, table_id, _ = primary_ids(members)
    key = f"data/tables/{table_id}/data.csv"
    members[key] = members[key].replace(b"1.23456789012345", b"invalid")
    with pytest.raises(ValueError, match="Unsupported observation token"):
        read_project(write_variant(tmp_path, members), include_values=False)


def test_missing_value_is_retained_in_its_original_row(tmp_path, members):
    _, table_id, _ = primary_ids(members)
    key = f"data/tables/{table_id}/data.csv"
    members[key] = members[key].replace(b"1.23456789012345", b"")
    values = read_project(write_variant(tmp_path, members))["tables"][0]["columns"][0]
    assert values["values"][0] is None
    assert values["raw_values"][0] == ""
    assert len(values["values"]) == 6


@pytest.mark.parametrize("token, message", [(b"NaN", "Unsupported observation token"), (b"inf", "Unsupported observation token"), (b"1e999", "Nonfinite observation"), (b"1.23*", "Unsupported observation token"), (b" ", "Unsupported observation token")])
def test_unsupported_or_nonfinite_observations_fail_with_position_only(tmp_path, members, token, message):
    _, table_id, _ = primary_ids(members)
    key = f"data/tables/{table_id}/data.csv"
    members[key] = members[key].replace(b"1.23456789012345", token)
    with pytest.raises(ValueError, match=message) as error:
        read_project(write_variant(tmp_path, members))
    assert "row 1" in str(error.value)


@pytest.mark.parametrize("flag_location", ["dataset", "replicate", "table"])
def test_unverified_exclusion_metadata_fails_closed(tmp_path, members, flag_location):
    sheet_id, _, datasets = primary_ids(members)
    key = f"data/sets/{datasets[0]}.json" if flag_location != "table" else f"data/sheets/{sheet_id}/sheet.json"
    value = object_at(members, key)
    if flag_location == "dataset":
        value["excludedRows"] = [0]
    elif flag_location == "replicate":
        value["replicates"][0]["cellAttributes"] = {"0": ["EXCLUDED"]}
    else:
        value["table"]["excludedCells"] = [0]
    put_object(members, key, value)
    with pytest.raises(ValueError, match="[Uu]nverified|[Uu]nsupported"):
        read_project(write_variant(tmp_path, members))


def test_analysis_result_table_cannot_become_source(tmp_path, members):
    doc = object_at(members, "document.json")
    analysis = doc["sheets"]["analyses"][0]
    view_key = next(key for key in members if key.startswith(f"analyses/{analysis}/result_sheets/") and key.endswith(".json"))
    result_sheet = object_at(members, view_key)["dataSheet"]
    doc["sheets"]["data"] = [result_sheet]
    put_object(members, "document.json", doc)
    with pytest.raises(ValueError, match="Analysis result sheet"):
        read_project(write_variant(tmp_path, members))


def test_broken_graph_binding_is_rejected(tmp_path, members):
    graph_id = object_at(members, "document.json")["sheets"]["graphs"][0]
    key = f"graphs/{graph_id}/sheet.json"
    graph = object_at(members, key)
    graph["inputDataSets"][0] = ABSENT_ID
    put_object(members, key, graph)
    with pytest.raises(ValueError, match="Broken UUID link.*graph"):
        read_project(write_variant(tmp_path, members))


def test_analysis_cannot_claim_dataset_from_another_sheet(tmp_path, members):
    doc = object_at(members, "document.json")
    analysis_id = doc["sheets"]["analyses"][0]
    view_key = next(key for key in members if key.startswith(f"analyses/{analysis_id}/result_sheets/") and key.endswith(".json"))
    key = f"analyses/{analysis_id}/sheet.json"
    analysis = object_at(members, key)
    analysis["inputSheets"][0]["uid"] = object_at(members, view_key)["dataSheet"]
    put_object(members, key, analysis)
    with pytest.raises(ValueError, match="belongs to another sheet"):
        read_project(write_variant(tmp_path, members))


def test_native_layout_reports_exact_link_order_and_media_count(tmp_path, members):
    doc = object_at(members, "document.json")
    graph_id = doc["sheets"]["graphs"][0]
    doc["sheets"]["layouts"] = [LAYOUT_ID]
    put_object(members, "document.json", doc)
    put_object(members, f"layouts/{LAYOUT_ID}/sheet.json", {"uid": LAYOUT_ID, "title": "Synthetic structural test", "linkedGraphSheets": [graph_id], "mediaCollection": []})
    members[f"layouts/{LAYOUT_ID}/data.bin"] = b"PCFFGRA4"  # A parser fixture, not a Prism-generated graph.
    model = read_project(write_variant(tmp_path, members))
    assert model["layouts"][0]["linked_graph_ids"] == [graph_id]
    assert model["layouts"][0]["media_count"] == 0
    layout = object_at(members, f"layouts/{LAYOUT_ID}/sheet.json")
    layout["linkedGraphSheets"] = [ABSENT_ID]
    put_object(members, f"layouts/{LAYOUT_ID}/sheet.json", layout)
    with pytest.raises(ValueError, match="Broken UUID link.*layout"):
        read_project(write_variant(tmp_path, members))


@pytest.mark.parametrize("version", ["1-6-2", "2-0-0", None, []])
def test_unknown_format_version_is_rejected(tmp_path, members, version):
    doc = object_at(members, "document.json")
    doc["formatVersion"] = version
    put_object(members, "document.json", doc)
    with pytest.raises(ValueError, match="Unsupported Prism format version"):
        read_project(write_variant(tmp_path, members))


@pytest.mark.parametrize("bad_csv", [b'"unterminated,2\n', b"1,2,3\n", b"1,2\n", b"1\x00,2\n"])
def test_malformed_csv_or_dimensions_rejected(tmp_path, members, bad_csv):
    _, table_id, _ = primary_ids(members)
    members[f"data/tables/{table_id}/data.csv"] = bad_csv
    with pytest.raises(ValueError, match="CSV"):
        read_project(write_variant(tmp_path, members))


@pytest.mark.parametrize("unsafe", ["../outside", "/absolute", "a/../b", "a\\b", "a//b", "C:/root"])
def test_unsafe_zip_paths_rejected_without_extraction(tmp_path, members, unsafe):
    members[unsafe] = b"never extract"
    with pytest.raises(ValueError, match="Unsafe archive member path"):
        read_project(write_variant(tmp_path, members))
    assert not (tmp_path / "outside").exists()


def test_duplicate_archive_member_is_rejected(tmp_path, members):
    target = write_variant(tmp_path, members)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(target, "a") as output:
            output.writestr("document.json", members["document.json"])
    with pytest.raises(ValueError, match="Duplicate archive member"):
        read_project(target)


def test_oversized_member_is_rejected_before_decompression(tmp_path, members, monkeypatch):
    monkeypatch.setattr(archive, "MAX_MEMBER_BYTES", 100)
    with pytest.raises(ValueError, match="member exceeds size"):
        read_project(write_variant(tmp_path, members))


@pytest.mark.parametrize("payload", [b"not a ZIP", b"PK\x03\x04", b""])
def test_bad_zip_is_value_error(tmp_path, payload):
    target = tmp_path / "invalid.prism"
    target.write_bytes(payload)
    with pytest.raises(ValueError, match="Cannot read Prism archive"):
        read_project(target)


def test_duplicate_json_and_nonfinite_json_rejected(tmp_path, members):
    members["document.json"] = b'{"@class":"Document","@class":"Document"}'
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        read_project(write_variant(tmp_path, members))
    members["document.json"] = b'{"a":NaN}'
    with pytest.raises(ValueError, match="Nonfinite JSON"):
        read_project(write_variant(tmp_path, members))


def test_missing_member_breaks_link(tmp_path, members):
    _, _, datasets = primary_ids(members)
    del members[f"data/sets/{datasets[0]}.json"]
    with pytest.raises(ValueError, match="Broken UUID link"):
        read_project(write_variant(tmp_path, members))


def test_unknown_fields_are_tolerated_without_inventing_formatting(tmp_path, members):
    doc = object_at(members, "document.json")
    doc["futureBookkeeping"] = {"example": 1}
    put_object(members, "document.json", doc)
    model = read_project(write_variant(tmp_path, members))
    assert model["relationships_valid"]
    assert "formatting" not in model["graphs"][0]


def test_missing_file_and_invalid_include_values_raise_value_error(tmp_path):
    with pytest.raises(ValueError):
        read_project(tmp_path / "missing.prism")
    with pytest.raises(ValueError):
        sha256_file(tmp_path / "missing.prism")
    with pytest.raises(ValueError, match="boolean"):
        read_project(FIXTURE, include_values=1)


def test_saved_snapshot_change_is_detected(tmp_path, members, monkeypatch):
    target = write_variant(tmp_path, members)
    original = archive._read_model

    def changing_read(*args, **kwargs):
        result = original(*args, **kwargs)
        target.write_bytes(target.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(archive, "_read_model", changing_read)
    with pytest.raises(ValueError, match="changed while being inspected"):
        read_project(target)
