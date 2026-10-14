"""Placement error classification without contacting an application or display."""

import json
import uuid
from pathlib import Path

import pytest

from prism_mcp import placement, runtime
from prism_mcp.errors import PrismError


@pytest.fixture(autouse=True)
def isolated_placement(monkeypatch, tmp_path):
    screen = {'id': 5, 'uuid': '8c1f5e8e-2453-4cad-a9e3-8672b370c1fd', 'is_main': False,
              'x': -2560, 'y': 252, 'width': 2560, 'height': 1440}
    policy = tmp_path / 'window-placement.json'
    runtime.write_json(policy, {'display': screen, 'document_frame': {
        'x': -2400, 'y': 350, 'width': 1500, 'height': 900}})
    monkeypatch.setattr(placement, 'POLICY', policy)
    monkeypatch.setattr(placement, 'displays', lambda: [screen])
    monkeypatch.setattr(placement, '_visible_prism_frames', lambda: [
        {'window_id': 1, 'X': 50, 'Y': 100, 'Width': 900, 'Height': 700}])

    def forbidden(*args, **kwargs):
        pytest.fail('Placement test attempted actual native control')

    monkeypatch.setattr(placement, 'osa', forbidden)


@pytest.mark.parametrize('stderr', ["Can't get object. (-1728)", 'Invalid index. (-1719)\n'])
def test_known_disappearing_window_errors_are_classified_for_bounded_retry(monkeypatch, stderr):
    def disappeared(*args, **kwargs):
        raise PrismError('CONTROL_UNAVAILABLE', 'simulated AX failure', stderr=stderr)

    monkeypatch.setattr(placement, 'osa', disappeared)
    with pytest.raises(PrismError) as error:
        placement.enforce()
    assert error.value.code == 'PLACEMENT_TRANSIENT'
    assert error.value.details['control_error']['details']['stderr'] == stderr


@pytest.mark.parametrize('code,stderr', [
    ('CONTROL_UNAVAILABLE', 'Not authorized (-1743)'),
    ('CONTROL_UNAVAILABLE', 'Accessibility unavailable (-25211)'),
    ('CONTROL_UNAVAILABLE', 'Earlier object failure (-1728)\nNot authorized (-1743)'),
    ('CONTROL_TIMEOUT', 'Window lookup timed out (-1728)'),
])
def test_permissions_timeouts_and_other_failures_are_not_transient(monkeypatch, code, stderr):
    def hard_failure(*args, **kwargs):
        raise PrismError(code, 'simulated hard failure', stderr=stderr)

    monkeypatch.setattr(placement, 'osa', hard_failure)
    with pytest.raises(PrismError) as error:
        placement.enforce()
    assert error.value.code == code


def test_missing_display_stops_before_any_window_control(monkeypatch):
    monkeypatch.setattr(placement, 'displays', lambda: [])
    with pytest.raises(PrismError) as error:
        placement.enforce()
    assert error.value.code == 'DISPLAY_UNAVAILABLE'


def test_uuid_selects_same_display_after_numeric_id_and_geometry_change():
    saved = {'id': 2, 'uuid': '8C1F5E8E-2453-4CAD-A9E3-8672B370C1FD',
             'x': -2560, 'y': 252, 'width': 2560, 'height': 1440}
    wanted = {**saved, 'id': 5, 'uuid': saved['uuid'].lower(), 'x': -2880, 'width': 2880, 'is_main': False}
    reused_id = {**saved, 'uuid': str(uuid.uuid4()), 'is_main': True}
    assert placement._resolve_display({'display': saved}, [reused_id, wanted]) is wanted


def test_missing_saved_uuid_never_falls_back_to_numeric_id_or_geometry():
    policy = json.loads(placement.POLICY.read_bytes())
    other = {**policy['display'], 'uuid': str(uuid.uuid4())}
    with pytest.raises(PrismError) as error:
        placement._resolve_display(policy, [other])
    assert error.value.code == 'DISPLAY_UNAVAILABLE'


def test_duplicated_display_uuid_is_ambiguous():
    policy = json.loads(placement.POLICY.read_bytes())
    same = policy['display']
    with pytest.raises(PrismError) as error:
        placement._resolve_display(policy, [same, {**same, 'id': 9}])
    assert error.value.code == 'DISPLAY_AMBIGUOUS'


@pytest.mark.parametrize('identity', ['', None, False, 'invalid'])
def test_invalid_saved_uuid_cannot_trigger_legacy_fallback(identity):
    policy = json.loads(placement.POLICY.read_bytes())
    current = dict(policy['display'])
    policy['display']['uuid'] = identity
    with pytest.raises(PrismError) as error:
        placement._resolve_display(policy, [current])
    assert error.value.code == 'DISPLAY_POLICY_INVALID'


def test_legacy_policy_migrates_only_unique_exact_secondary_display_with_exact_backup(monkeypatch):
    current = json.loads(placement.POLICY.read_bytes())['display']
    legacy = {'display': {key: value for key, value in current.items() if key not in ('uuid', 'is_main')},
              'document_frame': {'x': -2400, 'y': 350, 'width': 1500, 'height': 900},
              'source': 'original user choice', 'recorded_at': 12.5}
    legacy['display']['id'] = 2
    original = (json.dumps(legacy, separators=(',', ':')) + '\n').encode()
    placement.POLICY.write_bytes(original)
    monkeypatch.setattr(placement, 'osa', lambda *args, **kwargs: '{"configured": true, "moved": []}')
    monkeypatch.setattr(placement, '_visible_prism_frames', lambda: [])
    result = placement.enforce()
    migrated = json.loads(placement.POLICY.read_bytes())
    assert result['display'] == current
    assert migrated['display'] == current
    assert migrated['schema_version'] == 2
    assert migrated['document_frame'] == legacy['document_frame']
    assert migrated['recorded_at'] == legacy['recorded_at']
    assert migrated['source'] == legacy['source']
    backup = Path(migrated['identity_migration']['backup'])
    assert backup.read_bytes() == original
    placement.enforce()
    assert len(list(placement.POLICY.parent.glob('window-placement.pre-uuid-*.json'))) == 1


def test_in_bounds_window_never_contacts_busy_accessibility(monkeypatch):
    monkeypatch.setattr(placement, '_visible_prism_frames', lambda: [
        {'window_id': 8, 'X': -2400, 'Y': 350, 'Width': 1500, 'Height': 900}])
    result = placement.enforce()
    assert result['inspection'] == 'window_server_bounds'
    assert result['visible_window_count'] == 1
    assert result['moved'] == []


def test_native_move_requires_observed_return_to_chosen_display(monkeypatch):
    samples = iter([
        [{'window_id': 8, 'X': 50, 'Y': 100, 'Width': 900, 'Height': 700}],
        [{'window_id': 8, 'X': -2400, 'Y': 350, 'Width': 1500, 'Height': 900}],
    ])
    monkeypatch.setattr(placement, '_visible_prism_frames', lambda: next(samples))
    monkeypatch.setattr(placement, 'osa', lambda *args, **kwargs: '{"configured": true, "moved": [{"title": "owned"}]}')
    assert placement.enforce()['moved'] == [{'title': 'owned'}]


def test_reported_native_move_without_correct_observed_bounds_is_not_success(monkeypatch):
    monkeypatch.setattr(placement, 'osa', lambda *args, **kwargs: '{"configured": true, "moved": []}')
    with pytest.raises(PrismError) as error:
        placement.enforce()
    assert error.value.code == 'PLACEMENT_TRANSIENT'


@pytest.mark.parametrize('mode,expected_code', [
    ('main', 'DISPLAY_IDENTITY_UNAVAILABLE'), ('unknown_main', 'DISPLAY_IDENTITY_UNAVAILABLE'),
    ('missing_uuid', 'DISPLAY_IDENTITY_UNAVAILABLE'), ('different_geometry', 'DISPLAY_UNAVAILABLE'),
    ('ambiguous', 'DISPLAY_AMBIGUOUS'),
])
def test_unsafe_legacy_migrations_leave_policy_and_windows_untouched(monkeypatch, mode, expected_code):
    policy = json.loads(placement.POLICY.read_bytes())
    current = dict(policy['display'])
    policy['display'].pop('uuid')
    policy['display'].pop('is_main')
    runtime.write_json(placement.POLICY, policy)
    original = placement.POLICY.read_bytes()
    available = [current]
    if mode == 'main':
        current['is_main'] = True
    elif mode == 'unknown_main':
        current.pop('is_main')
    elif mode == 'missing_uuid':
        current.pop('uuid')
    elif mode == 'different_geometry':
        current['x'] -= 100
    else:
        available.append({**current, 'id': 10, 'uuid': str(uuid.uuid4())})
    monkeypatch.setattr(placement, 'displays', lambda: available)
    with pytest.raises(PrismError) as error:
        placement.enforce()
    assert error.value.code == expected_code
    assert placement.POLICY.read_bytes() == original
    assert not list(placement.POLICY.parent.glob('window-placement.pre-uuid-*.json'))


def test_display_uuid_bridge_releases_created_core_foundation_value(monkeypatch):
    calls = []
    raw = placement._CFUUIDBytes(*range(16))
    monkeypatch.setattr(placement, '_uuid_functions', lambda: (
        lambda identity: calls.append(('create', identity)) or 42,
        lambda value: calls.append(('get_bytes', value)) or raw,
        lambda value: calls.append(('release', value)),
    ))
    assert placement._display_uuid(5) == str(uuid.UUID(bytes=bytes(range(16))))
    assert calls == [('create', 5), ('get_bytes', 42), ('release', 42)]


def test_null_display_uuid_never_reads_or_releases_a_null_pointer(monkeypatch):
    def forbidden(value):
        pytest.fail('A null CFUUID pointer was used')

    monkeypatch.setattr(placement, '_uuid_functions', lambda: (lambda identity: None, forbidden, forbidden))
    with pytest.raises(PrismError) as error:
        placement._display_uuid(5)
    assert error.value.code == 'DISPLAY_IDENTITY_UNAVAILABLE'
