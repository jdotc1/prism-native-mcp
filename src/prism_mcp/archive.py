"""Read and verify saved Prism 11 archives without modifying their contents.

Only the JSON/CSV structures observed in current-format fixtures are interpreted.
Graph and layout binary payloads remain opaque. A successful read is structural
verification of a saved snapshot, not evidence that Prism opened or recalculated it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import stat
import uuid
import zipfile
import zlib
from pathlib import Path
from typing import Any, BinaryIO

SUPPORTED_FORMAT_VERSIONS = frozenset({"1-6-1"})
VERIFIED_PRISM_VERSIONS = frozenset({"11.1.0.218"})
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_MEMBER_COUNT = 50_000
MAX_TABLE_CELLS = 20_000_000
MAX_TABLE_ROWS = 500_000
MAX_TABLE_COLUMNS = 10_000

_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_DATA_SHEET = re.compile(r"data/sheets/([^/]+)/sheet\.json\Z")
_DATA_SET = re.compile(r"data/sets/([^/]+)\.json\Z")


def _hash_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    while block := stream.read(1024 * 1024):
        digest.update(block)
    return digest.hexdigest()


def sha256_file(path: str | Path) -> str:
    """Hash a file in bounded memory; missing/unreadable files raise ValueError."""
    try:
        with Path(path).open("rb") as stream:
            return _hash_stream(stream)
    except OSError as exc:
        raise ValueError(f"Cannot hash file: {exc.strerror or type(exc).__name__}") from exc


def _uid(value: Any, location: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid UUID at {location}")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid UUID at {location}") from exc
    if str(parsed) != value.lower():
        raise ValueError(f"Noncanonical UUID at {location}")
    return value


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object at {location}")
    return value


def _list(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"Expected JSON array at {location}")
    return value


def _ids(value: Any, location: str, *, objects: bool = False) -> list[str]:
    entries = _list(value, location)
    found = [
        _uid(_object(v, location).get("uid") if objects else v, location)
        for v in entries
    ]
    if len({v.lower() for v in found}) != len(found):
        raise ValueError(f"Duplicate UUID at {location}")
    return found


def _finite_json_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("Nonfinite JSON number")
    return value


def _reject_constant(raw: str) -> Any:
    raise ValueError(f"Nonfinite JSON constant: {raw}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class _Archive:
    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        infos = archive.infolist()
        if len(infos) > MAX_MEMBER_COUNT:
            raise ValueError("Archive has too many members")
        self.names: set[str] = set()
        total = 0
        for info in infos:
            name = info.orig_filename
            parts = name.rstrip("/").split("/")
            if (
                not name
                or "\x00" in name
                or "\\" in name
                or name.startswith("/")
                or ":" in parts[0]
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise ValueError("Unsafe archive member path")
            if name in self.names:
                raise ValueError("Duplicate archive member name")
            self.names.add(name)
            if info.flag_bits & 1:
                raise ValueError("Encrypted archive members are unsupported")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("Archive symlink members are unsupported")
            if info.file_size > MAX_MEMBER_BYTES:
                raise ValueError("Archive member exceeds size limit")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("Archive expanded size exceeds limit")
        # Check all members, including unknown payloads, without extracting files.
        damaged = archive.testzip()
        if damaged is not None:
            raise ValueError("Archive CRC verification failed")

    def read(self, member: str) -> bytes:
        if member not in self.names:
            raise ValueError(f"Broken archive reference: missing {member}")
        return self.archive.read(member)

    def json(self, member: str) -> dict[str, Any]:
        try:
            value = json.loads(
                self.read(member).decode("utf-8-sig"),
                object_pairs_hook=_unique_object,
                parse_float=_finite_json_float,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError(f"Malformed JSON in {member}") from exc
        return _object(value, member)

    def sheet(self, kind: str, identifier: str) -> dict[str, Any]:
        member = f"{kind}/{identifier}/sheet.json"
        result = self.json(member)
        if _uid(result.get("uid"), member) != identifier:
            raise ValueError(f"Mismatched object UUID in {member}")
        return result


def _ensure_links(identifiers: list[str], known: Any, location: str) -> None:
    if any(identifier not in known for identifier in identifiers):
        raise ValueError(f"Broken UUID link at {location}")


def _dimensions(content: dict[str, Any], location: str) -> tuple[int, int]:
    if type(content.get("version")) is not int or content["version"] != 1:
        raise ValueError(f"Unsupported table storage version at {location}")
    rows, columns = content.get("numberOfRows"), content.get("numberOfColumns")
    if any(type(v) is not int or v < 0 for v in (rows, columns)):
        raise ValueError(f"Invalid table dimensions at {location}")
    if rows > MAX_TABLE_ROWS or columns > MAX_TABLE_COLUMNS or rows * columns > MAX_TABLE_CELLS:
        raise ValueError(f"Table exceeds cell limit at {location}")
    return rows, columns


def _csv_rows(archive: _Archive, table_id: str) -> tuple[list[list[str]], int]:
    prefix = f"data/tables/{table_id}"
    row_count, column_count = _dimensions(archive.json(f"{prefix}/content.json"), prefix)
    try:
        raw = archive.read(f"{prefix}/data.csv").decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(raw, newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError(f"Malformed CSV at table {table_id}") from exc
    if len(rows) != row_count or any(len(row) != column_count for row in rows):
        raise ValueError(f"CSV dimensions disagree with table metadata at {table_id}")
    if "\x00" in raw:
        raise ValueError(f"NUL character in CSV at table {table_id}")
    return rows, column_count


def _numeric_value(raw: str, dataset_id: str, row: int) -> float | None:
    if raw == "":
        return None
    if not _NUMBER.fullmatch(raw):
        raise ValueError(f"Unsupported observation token at dataset {dataset_id}, row {row}")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"Nonfinite observation at dataset {dataset_id}, row {row}")
    return value


def _numeric_metadata(dataset: dict[str, Any], location: str) -> None:
    """Fail closed on unverified flags rather than treating excluded cells as data."""
    if dataset.get("attributes", []) != ["DS_ATTR_Y"]:
        raise ValueError(f"Unsupported observation dataset attributes at {location}")
    replicates = _list(dataset.get("replicates"), location)
    if len(replicates) != 1:
        raise ValueError(f"Unsupported y_single replicate mapping at {location}")
    replicate = _object(replicates[0], location)
    if replicate.get("valueType") != "real" or replicate.get("commonAttributes") != ["NUMERIC"]:
        raise ValueError(f"Unsupported observation value type or attributes at {location}")
    known = {"@class", "valueType", "commonAttributes", "decimalsLength", "significantDigitsCount", "lastRow", "firstRow"}
    if set(replicate) - known:
        raise ValueError(f"Unverified observation replicate metadata at {location}")
    for key in dataset:
        if any(marker in key.lower() for marker in ("exclu", "mask", "flags")):
            raise ValueError(f"Unsupported observation exclusion metadata at {location}")


def _mv_metadata(dataset: dict[str, Any], location: str) -> str:
    """Read the variable type, never infer it from the replicate's valueType.

    Prism serializes categorical and label variables with valueType=real too.
    Only observed display attributes and an empty cell-attribute range are safe
    here; exclusion flags must not silently become ordinary observations.
    """
    if dataset.get("@class") != "VariableDataSet" or dataset.get("format") != "y_single":
        raise ValueError(f"Unsupported multiple-variable dataset at {location}")
    kind = dataset.get("currentType")
    if not isinstance(kind, str) or kind not in {"continuous", "categorical", "label"}:
        raise ValueError(f"Unsupported multiple-variable type at {location}")
    if dataset.get("attributes") != ["DS_ATTR_Y"] or dataset.get("flags", []) != []:
        raise ValueError(f"Unsupported observation dataset attributes or flags at {location}")
    if any(any(marker in key.lower() for marker in ("exclu", "mask")) or ("flags" in key.lower() and key != "flags") for key in dataset):
        raise ValueError(f"Unsupported observation exclusion metadata at {location}")
    replicates = _list(dataset.get("replicates"), location)
    if len(replicates) != 1:
        raise ValueError(f"Unsupported y_single replicate mapping at {location}")
    replicate = _object(replicates[0], location)
    attributes = replicate.get("commonAttributes")
    if replicate.get("valueType") != "real" or attributes not in (["NUMERIC"], ["NUMERIC", "NOAUTODEC", "USER"]):
        raise ValueError(f"Unsupported observation value type or attributes at {location}")
    known = {"@class", "valueType", "maxWidth", "commonAttributes", "decimalsLength", "significantDigitsCount", "lastRow", "firstRow", "cellAttributes"}
    if set(replicate) - known:
        raise ValueError(f"Unverified observation replicate metadata at {location}")
    for entry in _list(replicate.get("cellAttributes", []), location):
        entry = _object(entry, location)
        # The Wine sample has {"rows":"0~177"}, with no attribute changes.
        if set(entry) != {"rows"} or not isinstance(entry["rows"], str) or not re.fullmatch(r"[0-9]+~[0-9]+", entry["rows"]):
            raise ValueError(f"Unverified observation cell attributes at {location}")
    return kind


def _mv_columns(table: dict[str, Any], datasets: dict[str, dict[str, Any]], rows: list[list[str]], count: int, include_values: bool, warnings: list[str]) -> list[dict[str, Any]]:
    identifier = table["uid"]
    if "valuesStorageData" in table:
        raise ValueError(f"Unverified observation storage mapping at table {identifier}")
    if table.get("flags", []) != [] or any(any(marker in key.lower() for marker in ("exclu", "mask", "flags")) and key != "flags" for key in table):
        raise ValueError(f"Unsupported observation exclusion metadata at table {identifier}")
    # Source MV fixtures have one CSV column per variable and no row-title slot.
    if table.get("rowTitlesDataSet") or count != len(table["dataSets"]):
        raise ValueError(f"Unverified CSV-to-dataset mapping at table {identifier}")
    columns = []
    for position, dataset_id in enumerate(table["dataSets"]):
        dataset = datasets[dataset_id]
        kind = _mv_metadata(dataset, dataset_id)
        column = {"id": dataset_id, "title": dataset.get("title", ""), "variable_type": kind, "native_metadata": dataset}
        if "valuesStorageData" in dataset:
            storage = _object(dataset["valuesStorageData"], dataset_id)
            origin = "calculated" if storage.get("@class") == "TransformDataSource" else "unverified_storage"
            column.update(value_origin=origin, values_parsed=False)
            warnings.append(f"Dataset {dataset_id}: {origin} values are not exposed as stored observations; native storage metadata is available.")
        else:
            raw_values = [row[position] for row in rows]
            values = ([_numeric_value(raw, dataset_id, row) for row, raw in enumerate(raw_values, 1)]
                      if kind == "continuous" else [raw if raw != "" else None for raw in raw_values])
            column.update(value_origin="stored", values_parsed=True)
            if include_values:
                column.update(values=values, raw_values=raw_values)
        columns.append(column)
    return columns


def _pointer(parent: str, key: Any) -> str:
    return f"{parent}/{str(key).replace('~', '~0').replace('/', '~1')}"


def _json_objects(value: Any, pointer: str = ""):
    """Iterate native objects without interpreting unknown graph parameters."""
    pending = [(pointer, value)]
    while pending:
        path, current = pending.pop()
        if isinstance(current, dict):
            yield path, current
            pending.extend((_pointer(path, key), item) for key, item in reversed(list(current.items())))
        elif isinstance(current, list):
            pending.extend((_pointer(path, index), current[index]) for index in range(len(current) - 1, -1, -1))


def _mv_graph(sheet: dict[str, Any], data_sheets: dict[str, dict[str, Any]], owners: dict[str, str], analyses: list[str]) -> dict[str, Any]:
    """Validate observed UUID fields, without resolving Prism's settings merge.

    Axis labelsSource may name the bound table itself (column names), or one of
    its datasets (including row titles). Legend/object UUIDs are a different
    namespace; arbitrary UUID-looking settings are deliberately not interpreted.
    """
    graph = _object(sheet.get("graph"), f"graph {sheet['uid']}")
    if graph.get("@class") != "MVGraph":
        raise ValueError(f"Unsupported native graph class at {sheet['uid']}")
    bindings: list[dict[str, str]] = []

    def binding(pointer: str, value: Any, kind: str, known: Any, owner: str | None = None) -> str:
        identifier = _uid(value, pointer)
        _ensure_links([identifier], known, f"graph {sheet['uid']}{pointer}")
        if owner is not None and owners.get(identifier) != owner:
            raise ValueError(f"Graph variable belongs to another data sheet at {pointer}")
        bindings.append({"json_pointer": pointer, "target_kind": kind, "target_id": identifier})
        return identifier

    sources = [("/graph", graph)]
    if "settingsSource" in sheet:
        binding("/settingsSource", sheet["settingsSource"], "analysis", analyses)
    if "mergedSettingsStorage" in sheet:
        merged = _object(sheet["mergedSettingsStorage"], "mergedSettingsStorage")
        if _uid(merged.get("uid"), "mergedSettingsStorage.uid") != sheet["uid"]:
            raise ValueError("Mismatched merged-settings graph UUID")
        sources.append(("/mergedSettingsStorage/graph", _object(merged.get("graph"), "mergedSettingsStorage.graph")))
    for pointer, settings in sources:
        if "@class" in settings and settings["@class"] != "MVGraph":
            raise ValueError(f"Unsupported native graph class at {pointer}")
        data_sheet = binding(f"{pointer}/dataSheet", settings.get("dataSheet"), "data_sheet", data_sheets)
        table_id = data_sheets[data_sheet]["table"]["uid"]
        for key in ("xVariables", "yVariables"):
            for index, identifier in enumerate(_ids(settings.get(key, []), f"{pointer}/{key}")):
                binding(f"{pointer}/{key}/{index}", identifier, "dataset", owners, data_sheet)
        for key in ("gdoSettings", "gdoSettingsExt", "gdoSettingsExtended"):
            if key not in settings:
                continue
            native_settings = _object(settings[key], f"{pointer}/{key}")
            for path, obj in _json_objects(native_settings, f"{pointer}/{key}"):
                for variable in ("colorVariable", "labelsVariable", "groupingVariable"):
                    if variable in obj:
                        binding(f"{path}/{variable}", obj[variable], "dataset", owners, data_sheet)
        for axis in ("axisX", "axisY"):
            if axis not in settings:
                continue
            axis_object = _object(settings[axis], f"{pointer}/{axis}")
            for key in ("segments", "segments_ext"):
                for index, segment in enumerate(_list(axis_object.get(key, []), f"{pointer}/{axis}/{key}")):
                    segment = _object(segment, f"{pointer}/{axis}/{key}/{index}")
                    if "labelsSource" not in segment:
                        continue
                    path = f"{pointer}/{axis}/{key}/{index}/labelsSource"
                    value = segment["labelsSource"]
                    if value == table_id:
                        binding(path, value, "table", [table_id])
                    else:
                        binding(path, value, "dataset", owners, data_sheet)
    native_objects = []
    for pointer, obj in _json_objects(sheet):
        if isinstance(obj.get("@class"), str):
            native_object = {"json_pointer": pointer, "class": obj["@class"]}
            if "uid" in obj:
                native_object["uid"] = obj["uid"]
            native_objects.append(native_object)
    return {
        "serialization": "MVGraph JSON",
        "native_parameters": sheet,
        "native_objects": native_objects,
        "bindings": bindings,
        "data_sheet_ids": list(dict.fromkeys(item["target_id"] for item in bindings if item["target_kind"] == "data_sheet")),
        "referenced_dataset_ids": list(dict.fromkeys(item["target_id"] for item in bindings if item["target_kind"] == "dataset")),
        "settings_merge_interpreted": False,
    }


def _read_model(archive: _Archive, include_values: bool) -> dict[str, Any]:
    document = archive.json("document.json")
    if document.get("@class") != "Document":
        raise ValueError("Archive document class is unsupported")
    version = document.get("formatVersion")
    if not isinstance(version, str) or version not in SUPPORTED_FORMAT_VERSIONS:
        raise ValueError(f"Unsupported Prism format version: {version}")
    prism_version = _object(document.get("modifiedBy", document.get("createdBy", {})), "document author").get("version")
    warnings = ["Graph/layout binary formatting is opaque; relationships_valid covers verified JSON links only."]
    if not isinstance(prism_version, str) or prism_version not in VERIFIED_PRISM_VERSIONS:
        warnings.append(f"Prism writer version {prism_version} has not been empirically verified.")
    sheet_lists = _object(document.get("sheets"), "document.sheets")
    ordered = {kind: _ids(sheet_lists.get(kind, []), f"document.sheets.{kind}") for kind in ("data", "graphs", "analyses", "layouts", "info")}
    all_root_ids = [identifier for values in ordered.values() for identifier in values]
    if len(set(all_root_ids)) != len(all_root_ids):
        raise ValueError("Document assigns one sheet UUID to multiple sheet types")
    datasets: dict[str, dict[str, Any]] = {}
    data_sheets: dict[str, dict[str, Any]] = {}
    for name in sorted(archive.names):
        if match := _DATA_SET.fullmatch(name):
            identifier = _uid(match[1], name)
            dataset = archive.json(name)
            if dataset.get("uid") != identifier:
                raise ValueError(f"Mismatched dataset UUID at {name}")
            datasets[identifier] = dataset
        elif match := _DATA_SHEET.fullmatch(name):
            identifier = _uid(match[1], name)
            data_sheets[identifier] = archive.sheet("data/sheets", identifier)
    _ensure_links(ordered["data"], data_sheets, "document.sheets.data")
    owners: dict[str, str] = {}
    for identifier, sheet in data_sheets.items():
        table = _object(sheet.get("table"), f"data sheet {identifier}")
        _uid(table.get("uid"), f"table for {identifier}")
        dataset_ids = _ids(table.get("dataSets", []), f"datasets for {identifier}")
        if table.get("rowTitlesDataSet"):
            dataset_ids.append(_uid(table["rowTitlesDataSet"], identifier))
        _ensure_links(dataset_ids, datasets, f"table {identifier} datasets")
        for dataset_id in dataset_ids:
            if dataset_id in owners and owners[dataset_id] != identifier:
                raise ValueError(f"Ambiguous dataset ownership for {dataset_id}")
            owners[dataset_id] = identifier
        _csv_rows(archive, table["uid"])

    tables = []
    for index, identifier in enumerate(ordered["data"], 1):
        sheet = data_sheets[identifier]
        if "ANALYSIS_VIEW" in _list(sheet.get("flags", []), identifier):
            raise ValueError("Analysis result sheet is incorrectly listed as a source table")
        table = sheet["table"]
        dataset_ids = _ids(table.get("dataSets", []), identifier)
        rows, count = _csv_rows(archive, table["uid"])
        columns: list[dict[str, Any]] = []
        if table.get("format") == "column" and table.get("dataFormat") == "y_single":
            if table.get("valuesStorageData"):
                raise ValueError(f"Unverified observation storage mapping at table {identifier}")
            if any(any(marker in key.lower() for marker in ("exclu", "mask")) for key in table):
                raise ValueError(f"Unsupported observation exclusion metadata at table {identifier}")
            offset = int(bool(table.get("rowTitlesDataSet")))
            if count != len(dataset_ids) + offset:
                raise ValueError(f"Unverified CSV-to-dataset mapping at table {identifier}")
            for position, dataset_id in enumerate(dataset_ids):
                dataset = datasets[dataset_id]
                if dataset.get("format") != "y_single":
                    raise ValueError(f"Dataset/table format mismatch for {dataset_id}")
                _numeric_metadata(dataset, dataset_id)
                raw_values = [row[position + offset] for row in rows]
                values = [_numeric_value(raw, dataset_id, row) for row, raw in enumerate(raw_values, 1)]
                column = {"id": dataset_id, "title": dataset.get("title", "")}
                if include_values:
                    column.update(values=values, raw_values=raw_values)
                columns.append(column)
        elif table.get("format") == "multivariable" and table.get("dataFormat") == "y_single":
            columns = _mv_columns(table, datasets, rows, count, include_values, warnings)
        else:
            warnings.append(f"Table {identifier}: observation parsing unsupported for {table.get('format')}/{table.get('dataFormat')}.")
        tables.append({"id": identifier, "index": index, "title": sheet.get("title", ""), "table_id": table["uid"], "format": table.get("format"), "data_format": table.get("dataFormat"), "dataset_ids": dataset_ids, "columns": columns, "row_count": len(rows), "csv_column_count": count})

    graphs = []
    for index, identifier in enumerate(ordered["graphs"], 1):
        graph = archive.sheet("graphs", identifier)
        inputs = _ids(graph.get("inputDataSets", []), identifier)
        _ensure_links(inputs, owners, f"graph {identifier} inputs")
        item = {"id": identifier, "index": index, "title": graph.get("title", ""), "input_dataset_ids": inputs}
        if graph.get("@class") == "GraphSheet":
            item.update(_mv_graph(graph, data_sheets, owners, ordered["analyses"]))
            member = f"graphs/{identifier}/data.bin"
            if member in archive.names:
                binary = archive.read(member)
                item["opaque_binary"] = {"member": member, "header": binary[:8].decode("ascii", errors="replace"), "size": len(binary), "sha256": hashlib.sha256(binary).hexdigest()}
        else:
            binary = archive.read(f"graphs/{identifier}/data.bin")
            header = binary[:8].decode("ascii", errors="replace")
            if header != "PCFFGRA4":
                warnings.append(f"Graph {identifier}: unverified binary serialization header.")
            item["serialization"] = header
        graphs.append(item)
    if any(graph["serialization"] == "MVGraph JSON" for graph in graphs):
        warnings.append("MVGraph binding checks cover observed dataSheet, variable, axis labelsSource, and settingsSource fields only; native object IDs, settings merges, and rendering are not interpreted.")

    analyses = []
    for index, identifier in enumerate(ordered["analyses"], 1):
        analysis = archive.sheet("analyses", identifier)
        inputs = _ids(analysis.get("inputDataSets", []), identifier, objects=True)
        input_sheets = _ids(analysis.get("inputSheets", []), identifier, objects=True)
        _ensure_links(inputs, owners, f"analysis {identifier} datasets")
        _ensure_links(input_sheets, set(data_sheets) | set(ordered["analyses"]), f"analysis {identifier} sheets")
        if all(sheet in data_sheets for sheet in input_sheets) and any(owners[dataset] not in input_sheets for dataset in inputs):
            raise ValueError(f"Analysis input dataset belongs to another sheet: {identifier}")
        parameters = archive.json(f"analyses/{identifier}/parameters.json")
        results = archive.json(f"analyses/{identifier}/results.json")
        if parameters.get("analysisClass") != analysis.get("analysisClass"):
            raise ValueError(f"Analysis class disagrees with parameters: {identifier}")
        result_ids = _ids(analysis.get("resultSheets", []), identifier, objects=True)
        if analysis.get("currentResultSheet"):
            _ensure_links([_uid(analysis["currentResultSheet"], identifier)], result_ids, f"current result view {identifier}")
        for result_id in result_ids:
            view = archive.json(f"analyses/{identifier}/result_sheets/{result_id}.json")
            if view.get("uid") != result_id:
                raise ValueError(f"Mismatched analysis result view UUID: {result_id}")
            view_sheet = _uid(view.get("dataSheet"), result_id)
            _ensure_links([view_sheet], data_sheets, f"result view {result_id}")
            if view_sheet in ordered["data"]:
                raise ValueError("Analysis result view is incorrectly listed as a source table")
        summaries = []
        content = _object(results.get("content", {}), identifier)
        if analysis.get("analysisClass") == "DESCRIPTIVE_STATISTICS":
            summary_datasets: set[str] = set()
            for sample in _object(content.get("samples", {}), identifier).values():
                sample = _object(sample, identifier)
                column = _object(_object(sample.get("id"), identifier).get("col"), identifier)
                dataset_id = _uid(column.get("uid"), identifier)
                _ensure_links([dataset_id], inputs, f"summary {identifier} dataset")
                if dataset_id in summary_datasets:
                    raise ValueError(f"Duplicate descriptive summary dataset in analysis {identifier}")
                summary_datasets.add(dataset_id)
                values = _object(sample.get("value"), identifier)
                if "dataset_id" in values:
                    raise ValueError(f"Reserved summary key in analysis {identifier}")
                summaries.append({"dataset_id": dataset_id, **values})
        if analysis.get("isCalculated") is not True:
            warnings.append(f"Analysis {identifier} is not marked calculated in the saved project.")
        status = content.get("status", {})
        if isinstance(status, dict) and status.get("error", "VALID") != "VALID":
            warnings.append(f"Analysis {identifier} saved result status is not VALID.")
        analyses.append({"id": identifier, "index": index, "title": analysis.get("title", ""), "analysis_class": analysis.get("analysisClass"), "input_dataset_ids": inputs, "input_sheet_ids": input_sheets, "parameters": parameters, "results": results, "summaries": summaries})

    layouts = []
    for index, identifier in enumerate(ordered["layouts"], 1):
        layout = archive.sheet("layouts", identifier)
        linked_graphs = _ids(layout.get("linkedGraphSheets", []), identifier)
        _ensure_links(linked_graphs, ordered["graphs"], f"layout {identifier} graphs")
        media = _list(layout.get("mediaCollection", []), identifier)
        archive.read(f"layouts/{identifier}/data.bin")
        layouts.append({"id": identifier, "index": index, "title": layout.get("title", ""), "linked_graph_ids": linked_graphs, "media_count": len(media)})
    for identifier in ordered["info"]:
        archive.sheet("info", identifier)
    return {"format_version": version, "prism_version": prism_version, "tables": tables, "graphs": graphs, "analyses": analyses, "layouts": layouts, "relationships_valid": True, "warnings": warnings, "member_count": len(archive.names)}


def read_project(path: str | Path, include_values: bool = True) -> dict[str, Any]:
    """Inspect a stable modern .prism snapshot and verify its observed JSON links.

    Malformed, incompatible, unsupported numeric metadata, or changing snapshots
    raise ValueError. Values and raw_values keys are omitted when include_values
    is false; structural and numeric validation still runs. No files are extracted.
    """
    if type(include_values) is not bool:
        raise ValueError("include_values must be a boolean")
    target = Path(path).expanduser().resolve()
    try:
        before = target.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Project must be a regular file")
        if before.st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Project archive exceeds size limit")
        with target.open("rb") as stream:
            digest = _hash_stream(stream)
            stream.seek(0)
            with zipfile.ZipFile(stream) as container:
                result = _read_model(_Archive(container), include_values)
        after = target.stat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after):
            raise ValueError("Project changed while being inspected; retry after Prism saves")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
        raise ValueError(f"Cannot read Prism archive: {type(exc).__name__}") from exc
    return {"path": str(target), "sha256": digest, **result}
