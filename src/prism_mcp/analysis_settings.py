"""Restricted offline edits to one observed native descriptive-settings schema.

This file-edit stage does not perform native Prism analysis or recalculation. The
caller must own the source and new destination checkpoints, ensure that neither
has an open Prism writer, and hold the global Prism operation lock through native
open/recalculation/save/verification. This module never inspects or controls UI.

Only the selected parameters.json scalar tokens change. Native results and all
other member payloads remain untouched, so callers must not treat saved results
as recalculated. Prism 11.1.0 build 110218 must open the edited copy, recalculate,
save, and verify settings/results before publication by the MCP wrapper.
Native differential acceptance established confidence 90/95 and saved result
text at 6/10 significant digits. Other numeric values remain outside the tested
subset. File editing alone never establishes native recalculation.
"""

from __future__ import annotations

import copy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Literal
import uuid
import zipfile
import zlib

from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError, field_validator, model_validator

from .archive import MAX_ARCHIVE_BYTES, _Archive, _read_model, sha256_file
from .errors import PrismError

ADAPTER_STATUS = 'file_edit_requires_native_recalculation'
FORMAT_VERSION = '1-6-1'
PRISM_WRITER_VERSION = '11.1.0.218'
PARAMETERS_SCHEMA = 'analyses/descriptive_statistics/parameters.schema.json'
PARAMETERS_VERSION = '0.0.0'
ANALYSIS_CLASS = 'DESCRIPTIVE_STATISTICS'
CONFIDENCE_CANDIDATES = (90.0, 95.0)
SIGNIFICANT_DIGIT_CANDIDATES = (6, 10)


class DescriptiveSettings(BaseModel):
    """A nonempty partial update; omitted fields retain their exact saved text."""

    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    mean_ci: StrictBool | None = None
    sum: StrictBool | None = None
    coefficient_of_variation: StrictBool | None = None
    confidence_level: Literal[90.0, 95.0] | None = None
    significant_digit_count: Literal[6, 10] | None = None

    @field_validator('confidence_level', mode='before')
    @classmethod
    def confidence_number(cls, value):
        if value is not None and (type(value) not in (int, float) or value not in CONFIDENCE_CANDIDATES):
            raise ValueError('Confidence must be the JSON number 90 or 95; other values remain outside the verified subset.')
        return float(value) if value is not None else None

    @field_validator('significant_digit_count', mode='before')
    @classmethod
    def digit_integer(cls, value):
        if value is not None and type(value) is not int:
            raise ValueError('Significant digits must be a JSON integer, not a Boolean or float.')
        return value

    @model_validator(mode='after')
    def nonempty_scalars(self):
        if not self.model_fields_set:
            raise ValueError('Request at least one descriptive setting.')
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError('Omit unchanged settings; explicit null is unsupported.')
        return self


_POINTERS = {
    'mean_ci': '/content/calc/meanCI',
    'sum': '/content/calc/sum',
    'coefficient_of_variation': '/content/calc/coefficientOfVariation',
    'confidence_level': '/content/options/confidenceLevel',
    'significant_digit_count': '/content/output/significantDigitCount',
}


def _schema_error(message, *, pointer=None):
    details = {'supported_parameter_schema': PARAMETERS_SCHEMA, 'adapter_status': ADAPTER_STATUS}
    if pointer is not None:
        details['json_pointer'] = pointer
    return PrismError('UNSUPPORTED_ANALYSIS_SETTINGS', message, **details)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('Nonfinite JSON constant')


_JSON_DECODER = json.JSONDecoder(object_pairs_hook=_unique_object, parse_float=Decimal,
                                 parse_constant=_reject_constant)


def _at(value, pointer):
    current = value
    for part in pointer.lstrip('/').split('/'):
        if not isinstance(current, dict) or part not in current:
            raise _schema_error('A required scalar parameter is missing.', pointer=pointer)
        current = current[part]
    return current


def _validate_parameters(value):
    if not isinstance(value, dict):
        raise _schema_error('Expected a native analysis parameters object.')
    for key, expected in {'@class': 'AnalysisParameters', '$id': PARAMETERS_SCHEMA,
                          'version': PARAMETERS_VERSION, 'analysisClass': ANALYSIS_CLASS}.items():
        if value.get(key) != expected:
            raise _schema_error('The native parameter class, schema, or version is outside the observed subset.',
                                pointer='/' + key)
    for group in ('data', 'calc', 'options', 'output'):
        if not isinstance(_at(value, '/content/' + group), dict):
            raise _schema_error('A required native parameter group is not an object.', pointer='/content/' + group)
    if _at(value, '/content/data/handleReplicatesAs') != 'AVERAGE':
        raise _schema_error('Only the observed AVERAGE replicate handling is supported.',
                            pointer='/content/data/handleReplicatesAs')
    for name in ('mean', 'median', 'minMaxRange', 'meanCI', 'sum', 'coefficientOfVariation'):
        pointer = '/content/calc/' + name
        current = _at(value, pointer)
        if type(current) is not bool:
            raise _schema_error('Only direct Boolean flags are supported; linked parameter objects are outside this subset.',
                                pointer=pointer)
        if name in ('mean', 'median', 'minMaxRange') and current is not True:
            raise _schema_error('Mean, median, and minimum/maximum must remain enabled for verification.', pointer=pointer)
    confidence = _at(value, '/content/options/confidenceLevel')
    if (type(confidence) not in (int, Decimal) or confidence not in CONFIDENCE_CANDIDATES):
        raise _schema_error('Only direct confidence candidates 90 and 95 are supported.',
                            pointer='/content/options/confidenceLevel')
    digits = _at(value, '/content/output/significantDigitCount')
    if type(digits) is not int or digits not in SIGNIFICANT_DIGIT_CANDIDATES:
        raise _schema_error('Only direct significant-digit candidates 6 and 10 are supported.',
                            pointer='/content/output/significantDigitCount')


def _scalar_spans(text, pointers):
    """Locate exact scalar tokens without reserializing any unknown JSON field."""
    spans = {}

    def whitespace(index):
        while index < len(text) and text[index] in ' \t\r\n':
            index += 1
        return index

    def visit(index, prefix):
        index = whitespace(index)
        if text[index] != '{':
            raise _schema_error('Requested parameter path is not an object.', pointer=prefix)
        index = whitespace(index + 1)
        while text[index] != '}':
            key, index = _JSON_DECODER.raw_decode(text, index)
            index = whitespace(index)
            if text[index] != ':':
                raise ValueError('Missing JSON member separator')
            start = whitespace(index + 1)
            _, end = _JSON_DECODER.raw_decode(text, start)
            pointer = prefix + '/' + key.replace('~', '~0').replace('/', '~1')
            if pointer in pointers:
                spans[pointer] = (start, end)
            elif any(wanted.startswith(pointer + '/') for wanted in pointers):
                visit(start, pointer)
            index = whitespace(end)
            if text[index] == '}':
                break
            if text[index] != ',':
                raise ValueError('Missing JSON item separator')
            index = whitespace(index + 1)

    visit(0, '')
    if set(spans) != set(pointers):
        raise _schema_error('Could not uniquely locate every requested scalar parameter.')
    return spans


def _updated_parameters(raw, settings):
    bom = b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b''
    try:
        text = raw.decode('utf-8-sig')
        value = _JSON_DECODER.decode(text)
        _validate_parameters(value)
        requested = {_POINTERS[name]: current for name, current in settings.model_dump(exclude_unset=True).items()}
        spans = _scalar_spans(text, requested)
        changes, replacements = [], []
        for pointer, after in requested.items():
            before = _at(value, pointer)
            if before == after:
                continue
            changes.append({'pointer': pointer, 'before': float(before) if isinstance(before, Decimal) else before,
                            'after': after})
            start, end = spans[pointer]
            replacements.append((start, end, json.dumps(after, allow_nan=False)))
        for start, end, replacement in sorted(replacements, reverse=True):
            text = text[:start] + replacement + text[end:]
        _validate_parameters(_JSON_DECODER.decode(text))
        return bom + text.encode('utf-8'), sorted(changes, key=lambda change: change['pointer'])
    except (UnicodeDecodeError, ValueError, IndexError, RecursionError) as exc:
        raise _schema_error('Native parameter JSON is malformed or ambiguous.') from exc


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _source_unchanged(source, identity, expected_hash):
    try:
        if source.is_symlink() or _identity(source.stat()) != identity or sha256_file(source) != expected_hash:
            raise PrismError('DOCUMENT_CHANGED', 'The closed source checkpoint changed during the offline edit.')
    except (OSError, ValueError) as exc:
        raise PrismError('DOCUMENT_CHANGED', 'The closed source checkpoint became unavailable during the offline edit.') from exc


def _member_metadata(info):
    # CRC, compressed sizes, offsets, and data-descriptor flags belong to the
    # rewritten ZIP container; payload hashes are verified independently below.
    return tuple(getattr(info, name) for name in (
        'filename', 'date_time', 'compress_type', 'comment', 'extra', 'create_system',
        'create_version', 'extract_version', 'reserved', 'volume', 'internal_attr', 'external_attr'))


def write_descriptive_settings(source: str | Path, destination: str | Path, analysis_id: str,
                               settings: DescriptiveSettings | dict[str, Any], *,
                               expected_source_sha256: str) -> dict[str, Any]:
    """Write a new owned checkpoint and return an audit; never modify the source.

    Caller responsibility: both paths must be owned checkpoints with no open
    Prism writer, coordinated under the global Prism operation lock. This offline
    function cannot establish window/writer ownership. It refuses existing or
    identical destinations and checks the source's identity and SHA-256 before
    and after the edit. The output still requires native recalculation and verification.
    """
    try:
        requested = DescriptiveSettings.model_validate(
            settings.model_dump(exclude_unset=True, warnings=False) if isinstance(settings, DescriptiveSettings) else settings)
    except ValidationError as exc:
        raise PrismError('INVALID_ARGUMENT', 'Unsupported descriptive settings request.',
                         validation_errors=exc.errors(include_url=False, include_input=False, include_context=False)) from exc
    if not isinstance(expected_source_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_source_sha256):
        raise PrismError('INVALID_ARGUMENT', 'Supply the inspected source checkpoint SHA-256.')
    try:
        parsed_id = uuid.UUID(analysis_id)
        if str(parsed_id) != analysis_id.lower():
            raise ValueError('Noncanonical UUID')
    except (ValueError, TypeError, AttributeError) as exc:
        raise PrismError('INVALID_ARGUMENT', 'Supply a canonical native analysis UUID.') from exc
    try:
        source, destination = Path(source).expanduser(), Path(destination).expanduser()
    except (TypeError, ValueError) as exc:
        raise PrismError('INVALID_PATH', 'Supply filesystem paths for both owned checkpoints.') from exc
    if not source.is_absolute() or not destination.is_absolute():
        raise PrismError('INVALID_PATH', 'Source and destination must be absolute checkpoint paths.')
    if source.is_symlink():
        raise PrismError('INVALID_PATH', 'The source checkpoint must not be a symbolic link.')
    source, destination = source.resolve(), destination.parent.resolve() / destination.name
    if source == destination:
        raise PrismError('INVALID_PATH', 'The offline adapter requires a separate new checkpoint.')
    if destination.exists() or destination.is_symlink():
        raise PrismError('TARGET_EXISTS', 'The offline adapter never overwrites an existing destination.')
    if source.suffix.lower() != '.prism' or destination.suffix.lower() != '.prism' or not destination.parent.is_dir():
        raise PrismError('INVALID_PATH', 'Use .prism checkpoint files in an existing owned destination directory.')

    stage = None
    published = False
    completed = False
    stage_identity = None
    try:
        before = source.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_ARCHIVE_BYTES:
            raise PrismError('INVALID_DOCUMENT', 'The source must be a bounded regular Prism archive.')
        source_identity = _identity(before)
        if sha256_file(source) != expected_source_sha256:
            raise PrismError('DOCUMENT_CHANGED', 'The source checkpoint does not match its inspected SHA-256.')
        baseline_members = {}
        with zipfile.ZipFile(source) as original:
            archive = _Archive(original)
            document = archive.json('document.json')
            observed = _read_model(archive, include_values=False)
            writer = document.get('modifiedBy')
            if (document.get('@class') != 'Document' or document.get('$id') != 'document.schema.json'
                    or observed['format_version'] != FORMAT_VERSION or observed['prism_version'] != PRISM_WRITER_VERSION
                    or not isinstance(writer, dict) or writer.get('name') != 'Prism'
                    or writer.get('version') != PRISM_WRITER_VERSION or writer.get('platform') != 'Mac'):
                raise _schema_error('The source document version or schema is outside the observed Prism build.')
            matches = [analysis for analysis in observed['analyses'] if analysis['id'].lower() == analysis_id.lower()]
            if len(matches) != 1:
                raise PrismError('OBJECT_NOT_FOUND', 'No unique native analysis has the requested UUID.', analysis_id=analysis_id)
            selected_id = matches[0]['id']
            sheet = archive.json(f'analyses/{selected_id}/sheet.json')
            if (sheet.get('@class') != 'AnalysisSheet' or sheet.get('$id') != 'analyses/sheet.schema.json'
                    or sheet.get('analysisClass') != ANALYSIS_CLASS):
                raise _schema_error('The selected analysis is not the observed native descriptive-statistics class.')
            parameter_member = f'analyses/{selected_id}/parameters.json'
            original_parameters = archive.read(parameter_member)
            edited_parameters, changes = _updated_parameters(original_parameters, requested)
            descriptor, stage_name = tempfile.mkstemp(prefix='.prism-settings-', suffix='.prism', dir=destination.parent)
            stage = Path(stage_name)
            with os.fdopen(descriptor, 'w+b') as stream:
                with zipfile.ZipFile(stream, 'w') as output:
                    output.comment = original.comment
                    for info in original.infolist():
                        payload = archive.read(info.filename)
                        baseline_members[info.filename] = (hashlib.sha256(payload).hexdigest(), _member_metadata(info))
                        output.writestr(copy.copy(info), edited_parameters if info.filename == parameter_member else payload)
                stream.flush()
                os.fsync(stream.fileno())

            with zipfile.ZipFile(stage) as written:
                checked = _Archive(written)
                _read_model(checked, include_values=False)
                if written.comment != original.comment or written.namelist() != original.namelist():
                    raise PrismError('ARCHIVE_PRESERVATION_FAILED', 'The archive comment or member order changed.')
                for info in written.infolist():
                    wanted_hash, metadata = baseline_members[info.filename]
                    if info.filename == parameter_member:
                        wanted_hash = hashlib.sha256(edited_parameters).hexdigest()
                    if hashlib.sha256(checked.read(info.filename)).hexdigest() != wanted_hash or _member_metadata(info) != metadata:
                        raise PrismError('ARCHIVE_PRESERVATION_FAILED', 'An archive member changed beyond the requested parameter tokens.',
                                         member=info.filename)
        _source_unchanged(source, source_identity, expected_source_sha256)
        output_hash = sha256_file(stage)
        os.link(stage, destination)  # Atomic publication with no-overwrite semantics.
        published = True
        stage_identity = _identity(stage.stat())
        _source_unchanged(source, source_identity, expected_source_sha256)
        result = {
            'status': ADAPTER_STATUS, 'analysis_id': selected_id,
            'source': str(source), 'destination': str(destination),
            'source_sha256_before': expected_source_sha256, 'source_sha256_after': expected_source_sha256,
            'source_unchanged': True, 'destination_sha256': output_hash,
            'parameter_member': parameter_member, 'changed_json_pointers': changes,
            'parameters_sha256_before': baseline_members[parameter_member][0],
            'parameters_sha256_after': hashlib.sha256(edited_parameters).hexdigest(),
            'unchanged_member_payloads_verified': True, 'member_metadata_verified': True,
            'archive_container_repacked': True, 'native_result_members_changed': False,
            'native_roundtrip_verified': False, 'native_recalculation_required': True,
            'experimental_numeric_values': [change for change in changes
                if (change['pointer'] == '/content/options/confidenceLevel' and change['after'] == 90.0)
                or (change['pointer'] == '/content/output/significantDigitCount' and change['after'] == 6)],
            'caller_contract': 'Owned closed checkpoints and global Prism operation lock; live writer ownership is not inspected here.',
        }
        completed = True
        return result
    except FileExistsError as exc:
        raise PrismError('TARGET_EXISTS', 'Another writer created the destination before publication.') from exc
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
        if isinstance(exc, PrismError):
            raise
        raise PrismError('INVALID_DOCUMENT', 'The offline archive edit could not be completed.', cause=type(exc).__name__) from exc
    finally:
        if stage is not None:
            if published and stage_identity is not None:
                # A failed final source check retracts only our untouched output.
                # The source and independently replaced destination remain intact.
                if not completed:
                    try:
                        if not destination.is_symlink() and _identity(destination.stat()) == stage_identity:
                            destination.unlink()
                    except OSError:
                        pass
            stage.unlink(missing_ok=True)
