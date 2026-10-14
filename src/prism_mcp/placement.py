"""Preserve the user's chosen Prism display without activating other apps."""
from __future__ import annotations

import ctypes
import functools
import json
import math
import re
import time
import uuid

from .errors import PrismError
from .runtime import CONTROL_ROOT, osa, write_json

POLICY = CONTROL_ROOT/'window-placement.json'


class _CFUUIDBytes(ctypes.Structure):
    _fields_ = [(f'byte{i}', ctypes.c_uint8) for i in range(16)]


@functools.lru_cache(maxsize=1)
def _uuid_functions():
    # Public declarations: ColorSyncDevice.h (macOS 10.4+) and CFUUID.h.
    # The pinned Quartz wrapper does not expose the ColorSync conversion.
    try:
        colorsync = ctypes.CDLL('/System/Library/Frameworks/ColorSync.framework/ColorSync')
        foundation = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        create = colorsync.CGDisplayCreateUUIDFromDisplayID
        create.argtypes, create.restype = [ctypes.c_uint32], ctypes.c_void_p
        get_bytes = foundation.CFUUIDGetUUIDBytes
        get_bytes.argtypes, get_bytes.restype = [ctypes.c_void_p], _CFUUIDBytes
        release = foundation.CFRelease
        release.argtypes, release.restype = [ctypes.c_void_p], None
        return create, get_bytes, release
    except (OSError, AttributeError) as exc:
        raise PrismError('DISPLAY_IDENTITY_UNAVAILABLE', 'The public display UUID API is unavailable.',
                         cause=type(exc).__name__) from exc


def _display_uuid(identity):
    create, get_bytes, release = _uuid_functions()
    value = create(int(identity))
    if not value:
        raise PrismError('DISPLAY_IDENTITY_UNAVAILABLE', 'The active display has no readable UUID.', display_id=int(identity))
    try:
        return str(uuid.UUID(bytes=bytes(get_bytes(value))))
    finally:
        release(value)


def displays():
    import Quartz
    status, identities, count = Quartz.CGGetActiveDisplayList(16, None, None)
    if status:
        raise PrismError('DISPLAY_UNAVAILABLE', 'Could not inspect active display geometry.')
    result = []
    for identity in identities[:count]:
        b = Quartz.CGDisplayBounds(identity)
        result.append({'id': int(identity), 'uuid': _display_uuid(identity),
                       'is_main': int(identity) == int(Quartz.CGMainDisplayID()), 'x': b.origin.x, 'y': b.origin.y,
                       'width': b.size.width, 'height': b.size.height})
    return result


def _resolve_display(policy, available):
    saved = policy.get('display')
    if not isinstance(saved, dict):
        raise PrismError('DISPLAY_POLICY_INVALID', 'The recorded placement has no display identity.')
    if 'uuid' in saved:
        try:
            wanted = str(uuid.UUID(saved['uuid']))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PrismError('DISPLAY_POLICY_INVALID', 'The recorded display UUID is invalid.') from exc
        matches = [d for d in available if d.get('uuid') == wanted]
    else:
        # A legacy transient ID can belong to another screen after a restart.
        # Migration requires one exact geometry match and never guesses main.
        geometry = ('x', 'y', 'width', 'height')
        if any(type(saved.get(key)) not in (int, float) or not math.isfinite(saved[key]) for key in geometry):
            raise PrismError('DISPLAY_POLICY_INVALID', 'Legacy display placement has incomplete geometry.')
        matches = [d for d in available if all(d.get(key) == saved[key] for key in geometry)]
    if not matches:
        raise PrismError('DISPLAY_UNAVAILABLE', 'The recorded Prism display is unavailable; placement requires its exact identity.')
    if len(matches) != 1:
        raise PrismError('DISPLAY_AMBIGUOUS', 'More than one active display matches the recorded placement.')
    screen = matches[0]
    if 'uuid' not in saved and (screen.get('is_main') is not False or not screen.get('uuid')):
        raise PrismError('DISPLAY_IDENTITY_UNAVAILABLE', 'Legacy placement can migrate only to one identified secondary display.')
    return screen


def _migrate_policy(policy, screen, original_bytes):
    if 'uuid' in policy['display']:
        return policy
    backup = POLICY.with_name(f'{POLICY.stem}.pre-uuid-{uuid.uuid4().hex}.json')
    with backup.open('xb') as stream:
        stream.write(original_bytes)
    updated = {**policy, 'schema_version': 2, 'display': screen,
               'identity_migration': {'method': 'unique_exact_geometry_secondary_display',
                                      'migrated_at': time.time(), 'backup': str(backup)}}
    write_json(POLICY, updated)
    return updated


def frames():
    return json.loads(osa('''var ps=Application('System Events').processes.whose({bundleIdentifier:'com.GraphPad.Prism'})();
JSON.stringify(ps.length?ps[0].windows().map(w=>{var d=null;try{d=w.attributes.byName('AXDocument').value()}catch(e){}
return {title:w.name(),url:d,position:w.position(),size:w.size()}}):[]);''', jxa=True))


def configure_from_current():
    available = [w for w in frames() if w['url']]
    if not available:
        raise PrismError('NO_DOCUMENT_WINDOW', 'Open a Prism document on the intended display before recording placement.')
    window = available[0]
    x, y = window['position']; width, height = window['size']
    matches = [d for d in displays() if d['x'] <= x+width/2 < d['x']+d['width']
               and d['y'] <= y+height/2 < d['y']+d['height']]
    if len(matches) != 1:
        raise PrismError('DISPLAY_UNAVAILABLE', 'Current Prism window does not identify one active display.')
    policy = {'schema_version': 2, 'display': matches[0], 'document_frame': {'x': x, 'y': y, 'width': width, 'height': height},
              'recorded_at': time.time(), 'source': 'user-requested current Prism display'}
    write_json(POLICY, policy)
    return policy


def _visible_prism_frames():
    """Inspect WindowServer geometry without sending AX requests to busy Prism."""
    import AppKit
    import Quartz
    # NSPanel windows are not necessarily normal-level windows. Include native
    # floating, modal, and utility panels; transient popup menus use a separate
    # level and do not expose the movable AX window contract used below.
    levels = {int(Quartz.CGWindowLevelForKey(key)) for key in (
        Quartz.kCGNormalWindowLevelKey, Quartz.kCGFloatingWindowLevelKey,
        Quartz.kCGModalPanelWindowLevelKey, Quartz.kCGUtilityWindowLevelKey)}
    identities = {int(app.processIdentifier()) for app in
                  AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_('com.GraphPad.Prism')}
    rows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    if rows is None:
        raise PrismError('PLACEMENT_UNAVAILABLE', 'WindowServer geometry could not be inspected.')
    result = []
    for row in rows:
        if row.get('kCGWindowOwnerPID') not in identities or row.get('kCGWindowLayer') not in levels:
            continue
        raw = row.get('kCGWindowBounds', {})
        try:
            bounds = {k: float(raw[k]) for k in ('X', 'Y', 'Width', 'Height')}
            if not all(math.isfinite(v) for v in bounds.values()):
                raise ValueError('Nonfinite bounds')
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise PrismError('PLACEMENT_UNAVAILABLE', 'An owned window has invalid WindowServer geometry.') from exc
        if bounds['Width'] > 0 and bounds['Height'] > 0:
            result.append({'window_id': int(row['kCGWindowNumber']), **bounds})
    return result


def _inside(bounds, screen):
    return (bounds['X'] >= screen['x'] and bounds['Y'] >= screen['y']
            and bounds['X'] + bounds['Width'] <= screen['x'] + screen['width']
            and bounds['Y'] + bounds['Height'] <= screen['y'] + screen['height'])


def enforce():
    if not POLICY.exists():
        return {'configured': False, 'moved': []}
    original_bytes = POLICY.read_bytes()
    policy = json.loads(original_bytes)
    screen = _resolve_display(policy, displays())
    policy = _migrate_policy(policy, screen, original_bytes)
    visible = _visible_prism_frames()
    if all(_inside(bounds, screen) for bounds in visible):
        return {'configured': True, 'moved': [], 'display': screen,
                'inspection': 'window_server_bounds', 'visible_window_count': len(visible)}
    f = policy['document_frame']
    left = max(screen['x'], min(f['x'], screen['x']+screen['width']-min(f['width'], screen['width'])))
    top = max(screen['y']+30, min(f['y'], screen['y']+screen['height']-min(f['height'], screen['height']-30)))
    code = '''var ps=Application('System Events').processes.whose({bundleIdentifier:'com.GraphPad.Prism'})();var moved=[];
if(ps.length){ps[0].windows().forEach(w=>{var p=w.position(),s=w.size();var d=null;try{d=w.attributes.byName('AXDocument').value()}catch(e){}
var screen=SCREEN;var frame=FRAME;
if(p[0]<screen.x||p[1]<screen.y||p[0]+s[0]>screen.x+screen.width||p[1]+s[1]>screen.y+screen.height){
var width=Math.min(d?frame.width:s[0],screen.width),height=Math.min(d?frame.height:s[1],screen.height-30);
var x=d?frame.x:Math.min(screen.x+screen.width-width,frame.x+Math.max(0,(frame.width-width)/2));
var y=d?frame.y:Math.min(screen.y+screen.height-height,frame.y+Math.max(0,(frame.height-height)/2));
w.size=[width,height];w.position=[x,y];moved.push({title:w.name(),position:[x,y],size:[width,height]});}})}
JSON.stringify({configured:true,moved:moved});'''
    code = code.replace('SCREEN', json.dumps(screen)).replace('FRAME', json.dumps({**f, 'x': left, 'y': top}))
    try:
        result = json.loads(osa(code, jxa=True))
        if any(not _inside(bounds, screen) for bounds in _visible_prism_frames()):
            raise PrismError('PLACEMENT_TRANSIENT', 'Prism window placement has not yet reached the recorded display.')
        return {**result, 'display': screen}
    except PrismError as exc:
        # Prism can replace a welcome/document window during a native command.
        # Retry only the two AX lookup errors that identify that disappearance.
        if (exc.code == 'CONTROL_UNAVAILABLE'
                and re.search(r'\(-(1728|1719)\)\s*$', str(exc.details.get('stderr', '')))):
            raise PrismError('PLACEMENT_TRANSIENT', 'Prism replaced a window during placement inspection.',
                             control_error=exc.as_dict()) from exc
        raise
