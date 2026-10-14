"""Offline settings edits only; these tests never establish native Prism validity."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import uuid
import zipfile

import pytest
from pydantic import ValidationError

from prism_mcp import analysis_settings as module
from prism_mcp import placement, runtime
from prism_mcp.analysis_settings import DescriptiveSettings, write_descriptive_settings
from prism_mcp.archive import read_project, sha256_file
from prism_mcp.errors import PrismError

FIXTURE = Path(__file__).parents[1] / 'src/prism_mcp/templates/column_violin_summary.prism'


@pytest.fixture(autouse=True)
def no_application_control(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Offline settings test attempted application or process control')

    monkeypatch.setattr(runtime, 'osa', forbidden)
    monkeypatch.setattr(runtime, 'windows', forbidden)
    monkeypatch.setattr(runtime, 'verify_running_application', forbidden)
    monkeypatch.setattr(runtime.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(runtime.subprocess, 'run', forbidden)
    monkeypatch.setattr(placement, 'osa', forbidden)
    monkeypatch.setattr(placement, 'enforce', forbidden)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'input.prism'
    path.write_bytes(FIXTURE.read_bytes())
    return path


def analysis_id(source):
    with zipfile.ZipFile(source) as archive:
        return json.loads(archive.read('document.json'))['sheets']['analyses'][0]


def parameter_member(source):
    return f'analyses/{analysis_id(source)}/parameters.json'


def payloads(source):
    with zipfile.ZipFile(source) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def variant(source, mutations=None, *, extra_member=False):
    """Rewrite an owned fixture before the adapter captures its source revision."""
    stage = source.with_name('variant.prism')
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(stage, 'w') as output:
        output.comment = b'Preserved archive comment'
        for info in original.infolist():
            raw = original.read(info)
            if mutations and info.filename in mutations:
                raw = mutations[info.filename](raw)
            output.writestr(copy.copy(info), raw)
        if extra_member:
            info = zipfile.ZipInfo('misc/unknown-reader-payload.bin', date_time=(2023, 2, 4, 10, 12, 14))
            info.comment = b'Preserved member comment'
            info.extra = b'\xca\xfe\x04\x00DATA'
            info.external_attr = 0o100640 << 16
            output.writestr(info, b'\x00\xffunknown opaque bytes\x7f')
    os.replace(stage, source)


def set_parameter(source, pointer, value=None, *, delete=False):
    member = parameter_member(source)

    def change(raw):
        parsed = json.loads(raw)
        current = parsed
        parts = pointer.strip('/').split('/')
        for part in parts[:-1]:
            current = current[part]
        if delete:
            del current[parts[-1]]
        else:
            current[parts[-1]] = value
        return json.dumps(parsed, indent=2).encode()

    variant(source, {member: change})


def edit(source, destination, settings, *, identity=None, expected_hash=None):
    return write_descriptive_settings(source, destination, identity or analysis_id(source), settings,
                                      expected_source_sha256=expected_hash or sha256_file(source))


def test_partial_update_changes_only_requested_native_parameter_tokens(source, tmp_path):
    member = parameter_member(source)

    def unknown_fields(raw):
        text = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        text = text[:-1] + ',\n  "unknown/section~": {"meanCI": false, "fraction": 0.123456789012345678901234567890, "scientific": 1.2500e-7, "label": "μs / preserved"}\n}'
        return b'\xef\xbb\xbf' + text.encode()

    variant(source, {member: unknown_fields}, extra_member=True)
    original_bytes, original_hash = source.read_bytes(), sha256_file(source)
    before = payloads(source)
    destination = tmp_path / 'settings.prism'
    report = edit(source, destination, DescriptiveSettings(mean_ci=True, sum=True,
        coefficient_of_variation=True, confidence_level=90, significant_digit_count=6))

    assert source.read_bytes() == original_bytes
    after = payloads(destination)
    assert set(after) == set(before)
    for name in before:
        if name != member:
            assert after[name] == before[name]
    expected = before[member]
    for old, new in [(b'"meanCI": false', b'"meanCI": true'), (b'"sum": false', b'"sum": true'),
                     (b'"coefficientOfVariation": false', b'"coefficientOfVariation": true'),
                     (b'"confidenceLevel": 95.0', b'"confidenceLevel": 90.0'),
                     (b'"significantDigitCount": 10', b'"significantDigitCount": 6')]:
        expected = expected.replace(old, new, 1)
    assert after[member] == expected
    assert b'0.123456789012345678901234567890' in after[member]
    assert b'"unknown/section~": {"meanCI": false' in after[member]
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination) as changed:
        assert original.comment == changed.comment
        for key in ('comment', 'extra', 'external_attr', 'date_time', 'compress_type'):
            assert getattr(original.getinfo('misc/unknown-reader-payload.bin'), key) == getattr(changed.getinfo('misc/unknown-reader-payload.bin'), key)
    assert report['status'] == 'file_edit_requires_native_recalculation'
    assert report['source_unchanged'] and report['source_sha256_before'] == report['source_sha256_after'] == original_hash
    assert report['destination_sha256'] == sha256_file(destination)
    assert report['unchanged_member_payloads_verified'] and report['member_metadata_verified']
    assert report['native_recalculation_required'] and not report['native_roundtrip_verified']
    assert not report['native_result_members_changed']
    assert len(report['changed_json_pointers']) == 5
    assert len(report['experimental_numeric_values']) == 2
    assert all(change['before'] != change['after'] for change in report['changed_json_pointers'])
    assert read_project(destination)['relationships_valid']
    json.dumps(report, allow_nan=False)


def test_offline_reversal_restores_parameters_without_claiming_native_recalculation(source, tmp_path):
    before = payloads(source)
    edited, restored = tmp_path / 'edited.prism', tmp_path / 'restored.prism'
    edit(source, edited, {'mean_ci': True, 'sum': True, 'coefficient_of_variation': True,
                          'confidence_level': 90, 'significant_digit_count': 6})
    report = edit(edited, restored, {'mean_ci': False, 'sum': False, 'coefficient_of_variation': False,
                                    'confidence_level': 95, 'significant_digit_count': 10})
    assert payloads(restored) == before
    assert report['native_recalculation_required'] and not report['native_roundtrip_verified']


def test_unchanged_request_preserves_every_member_and_reports_no_changed_pointer(source, tmp_path):
    destination = tmp_path / 'unchanged.prism'
    report = edit(source, destination, {'mean_ci': False})
    assert payloads(destination) == payloads(source)
    assert report['changed_json_pointers'] == []
    assert report['native_recalculation_required']


@pytest.mark.parametrize('settings', [
    {}, {'mean_ci': None}, {'mean_ci': 1}, {'sum': 'true'}, {'coefficient_of_variation': []},
    {'mean': False}, {'median': False}, {'min_max_range': False}, {'unexpected': True},
    {'confidence_level': True}, {'confidence_level': '90'}, {'confidence_level': 0},
    {'confidence_level': 99}, {'confidence_level': 100}, {'confidence_level': float('nan')},
    {'confidence_level': float('inf')}, {'confidence_level': 10**400},
    {'significant_digit_count': True}, {'significant_digit_count': 6.0},
    {'significant_digit_count': '6'}, {'significant_digit_count': 0}, {'significant_digit_count': 7},
])
def test_model_rejects_unknown_null_coerced_and_unbounded_requests(settings):
    with pytest.raises(ValidationError):
        DescriptiveSettings.model_validate(settings)


def test_helper_revalidates_constructed_models_and_returns_serializable_error(source, tmp_path):
    with pytest.raises(PrismError) as error:
        edit(source, tmp_path / 'invalid.prism', DescriptiveSettings.model_construct(mean_ci='yes'))
    assert error.value.code == 'INVALID_ARGUMENT'
    json.dumps(error.value.as_dict(), allow_nan=False)


@pytest.mark.parametrize('pointer,value', [
    ('/version', '0.0.1'), ('/version', None), ('/@class', 'OtherParameters'),
    ('/$id', 'analyses/other/parameters.schema.json'),
    ('/content/data/handleReplicatesAs', 'OTHER'),
    ('/content/calc/meanCI', 1), ('/content/calc/sum', {'value': False, 'link': 'external'}),
    ('/content/calc/coefficientOfVariation', None), ('/content/calc/mean', False),
    ('/content/calc/median', False), ('/content/calc/minMaxRange', False),
    ('/content/options/confidenceLevel', {'value': 95, 'link': 'external'}),
    ('/content/options/confidenceLevel', '95'), ('/content/options/confidenceLevel', 99),
    ('/content/output/significantDigitCount', True), ('/content/output/significantDigitCount', 10.0),
])
def test_unsupported_saved_versions_shapes_and_invariants_stop_before_writing(source, tmp_path, pointer, value):
    set_parameter(source, pointer, value)
    before = source.read_bytes()
    destination = tmp_path / 'rejected.prism'
    with pytest.raises(PrismError) as error:
        edit(source, destination, {'sum': True})
    assert error.value.code == 'UNSUPPORTED_ANALYSIS_SETTINGS'
    assert source.read_bytes() == before
    assert not destination.exists()
    assert not list(tmp_path.glob('.prism-settings-*'))


@pytest.mark.parametrize('pointer', [
    '/@class', '/$id', '/version', '/content', '/content/data', '/content/calc',
    '/content/options', '/content/output', '/content/calc/mean', '/content/calc/meanCI',
    '/content/calc/sum', '/content/calc/coefficientOfVariation', '/content/calc/median',
    '/content/calc/minMaxRange', '/content/options/confidenceLevel', '/content/output/significantDigitCount',
])
def test_missing_observed_schema_fields_are_not_recreated_from_defaults(source, tmp_path, pointer):
    set_parameter(source, pointer, delete=True)
    destination = tmp_path / 'rejected.prism'
    with pytest.raises(PrismError) as error:
        edit(source, destination, {'sum': True})
    assert error.value.code == 'UNSUPPORTED_ANALYSIS_SETTINGS'
    assert not destination.exists()


@pytest.mark.parametrize('field,value', [('formatVersion', '1-6-2'), ('@class', 'OtherDocument'),
                                        ('$id', 'other.schema.json'), ('writer', '11.2.0.1')])
def test_unobserved_document_build_or_schema_is_rejected(source, tmp_path, field, value):
    def change(raw):
        document = json.loads(raw)
        if field == 'writer':
            document['modifiedBy']['version'] = value
        else:
            document[field] = value
        return json.dumps(document).encode()

    variant(source, {'document.json': change})
    with pytest.raises(PrismError) as error:
        edit(source, tmp_path / 'rejected.prism', {'sum': True})
    assert error.value.code in {'INVALID_DOCUMENT', 'UNSUPPORTED_ANALYSIS_SETTINGS'}


@pytest.mark.parametrize('mode', ['missing', 'other_application', 'other_platform'])
def test_writer_evidence_is_required_instead_of_inferred_from_creator(source, tmp_path, mode):
    def change(raw):
        document = json.loads(raw)
        if mode == 'missing':
            document.pop('modifiedBy')
        else:
            document['modifiedBy']['name' if mode == 'other_application' else 'platform'] = 'Unobserved'
        return json.dumps(document).encode()

    variant(source, {'document.json': change})
    with pytest.raises(PrismError) as error:
        edit(source, tmp_path / 'rejected.prism', {'sum': True})
    assert error.value.code == 'UNSUPPORTED_ANALYSIS_SETTINGS'


def test_analysis_uuid_is_scoped_and_case_insensitive(source, tmp_path):
    identity = analysis_id(source)
    report = edit(source, tmp_path / 'selected.prism', {'sum': True}, identity=identity.lower())
    assert report['analysis_id'] == identity
    with pytest.raises(PrismError) as error:
        edit(source, tmp_path / 'missing.prism', {'sum': True}, identity=str(uuid.uuid4()))
    assert error.value.code == 'OBJECT_NOT_FOUND'
    with pytest.raises(PrismError) as malformed:
        edit(source, tmp_path / 'invalid-id.prism', {'sum': True}, identity='../other')
    assert malformed.value.code == 'INVALID_ARGUMENT'


def test_other_analysis_class_is_not_modified(source, tmp_path):
    def change(raw):
        value = json.loads(raw)
        value['analysisClass'] = 'OTHER_ANALYSIS'
        return json.dumps(value).encode()

    identity = analysis_id(source)
    variant(source, {f'analyses/{identity}/sheet.json': change, parameter_member(source): change})
    with pytest.raises(PrismError) as error:
        edit(source, tmp_path / 'rejected.prism', {'sum': True})
    assert error.value.code == 'UNSUPPORTED_ANALYSIS_SETTINGS'


@pytest.mark.parametrize('mode', ['duplicate_key', 'truncated', 'not_json'])
def test_malformed_or_ambiguous_parameter_json_is_rejected(source, tmp_path, mode):
    def change(raw):
        if mode == 'duplicate_key':
            return raw.replace(b'"meanCI": false', b'"meanCI": false, "meanCI": true')
        return raw[:-7] if mode == 'truncated' else b'not JSON'

    variant(source, {parameter_member(source): change})
    with pytest.raises(PrismError) as error:
        edit(source, tmp_path / 'rejected.prism', {'sum': True})
    assert error.value.code in {'INVALID_DOCUMENT', 'UNSUPPORTED_ANALYSIS_SETTINGS'}


def test_existing_destination_same_path_and_symlinks_are_never_overwritten(source, tmp_path):
    original = source.read_bytes()
    target = tmp_path / 'existing.prism'
    target.write_bytes(b'unrelated existing document')
    with pytest.raises(PrismError) as error:
        edit(source, target, {'sum': True})
    assert error.value.code == 'TARGET_EXISTS'
    assert target.read_bytes() == b'unrelated existing document'
    with pytest.raises(PrismError) as same:
        edit(source, source, {'sum': True})
    assert same.value.code == 'INVALID_PATH'
    symlink = tmp_path / 'symbolic.prism'
    symlink.symlink_to(tmp_path / 'absent.prism')
    with pytest.raises(PrismError) as linked:
        edit(source, symlink, {'sum': True})
    assert linked.value.code == 'TARGET_EXISTS'
    assert symlink.is_symlink()
    assert source.read_bytes() == original


def test_source_symlink_and_stale_fingerprint_are_rejected(source, tmp_path):
    symbolic = tmp_path / 'source-link.prism'
    symbolic.symlink_to(source)
    with pytest.raises(PrismError) as linked:
        edit(symbolic, tmp_path / 'linked-output.prism', {'sum': True})
    assert linked.value.code == 'INVALID_PATH'
    with pytest.raises(PrismError) as stale:
        edit(source, tmp_path / 'stale-output.prism', {'sum': True}, expected_hash='0' * 64)
    assert stale.value.code == 'DOCUMENT_CHANGED'


def test_destination_created_at_publication_is_preserved(source, tmp_path, monkeypatch):
    destination = tmp_path / 'competing.prism'
    original_link = module.os.link

    def competing_writer(staged, target):
        destination.write_bytes(b'independent destination writer')
        original_link(staged, target)

    monkeypatch.setattr(module.os, 'link', competing_writer)
    with pytest.raises(PrismError) as error:
        edit(source, destination, {'sum': True})
    assert error.value.code == 'TARGET_EXISTS'
    assert destination.read_bytes() == b'independent destination writer'
    assert not list(tmp_path.glob('.prism-settings-*'))


@pytest.mark.parametrize('after_publication', [False, True])
def test_concurrent_source_change_aborts_and_preserves_independent_revision(source, tmp_path, monkeypatch, after_publication):
    destination = tmp_path / 'aborted.prism'
    changed = source.read_bytes() + b'independent source writer'
    if after_publication:
        original_link = module.os.link

        def write_then_change(staged, target):
            original_link(staged, target)
            source.write_bytes(changed)

        monkeypatch.setattr(module.os, 'link', write_then_change)
    else:
        original_write = module.zipfile.ZipFile.writestr

        def change_during_staging(archive, *args, **kwargs):
            result = original_write(archive, *args, **kwargs)
            source.write_bytes(changed)
            return result

        monkeypatch.setattr(module.zipfile.ZipFile, 'writestr', change_during_staging)
    with pytest.raises(PrismError) as error:
        edit(source, destination, {'sum': True})
    assert error.value.code == 'DOCUMENT_CHANGED'
    assert source.read_bytes() == changed
    assert not destination.exists()
    assert not list(tmp_path.glob('.prism-settings-*'))


def test_unrequested_member_damage_prevents_publication(source, tmp_path, monkeypatch):
    variant(source, extra_member=True)
    before = source.read_bytes()
    destination = tmp_path / 'damaged.prism'
    original_write = module.zipfile.ZipFile.writestr

    def damaged_write(archive, info, payload, *args, **kwargs):
        if info.filename == 'misc/unknown-reader-payload.bin':
            payload = b'unrequested payload damage'
        return original_write(archive, info, payload, *args, **kwargs)

    monkeypatch.setattr(module.zipfile.ZipFile, 'writestr', damaged_write)
    with pytest.raises(PrismError) as error:
        edit(source, destination, {'sum': True})
    assert error.value.code == 'ARCHIVE_PRESERVATION_FAILED'
    assert source.read_bytes() == before
    assert not destination.exists()
