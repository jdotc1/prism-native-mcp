"""Narrow Prism 11.1 AX adapters. No canvas tracing or imported figure layers.

These direct local dependencies also work in the standalone MCP process.
All callers must hold Runtime.locked() and operate on their own checkpoint.
"""
from __future__ import annotations

import contextlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from .errors import PrismError
from .runtime import osa, apple_quote, write_json


def menu(name: str, item: str, submenu: str | None = None):
    target = f'menu item {apple_quote(item)} of menu 1 of '
    if submenu:
        target += f'menu item {apple_quote(submenu)} of menu 1 of '
    target += f'menu bar item {apple_quote(name)} of menu bar 1'
    # Dispatch the leaf directly. Opening a menu bar item through AXPress can
    # leave AppKit's menu-tracking loop active after its window disappears,
    # delaying future Apple Events. Dialog/readback checks confirm application.
    token = uuid.uuid4().hex
    try:
        result = osa(f'tell application "System Events" to tell process "Prism"\nset frontmost to true\ndelay 0.2\n' +
                     f'if not enabled of {target} then error "Required native menu item is disabled"\n'
                     f'click {target}\ndelay 0.2\nreturn {apple_quote(token)}\nend tell')
    except PrismError as exc:
        raise PrismError(exc.code, str(exc), menu=name, item=item, submenu=submenu, **exc.details) from exc
    if result != token:
        raise PrismError('UI_ACTION_NOT_DISPATCHED', 'The native menu action did not confirm this dispatch.', menu=name, item=item)
    return result


def _window_state():
    return json.loads(osa('''var p=Application('System Events').processes.byName('Prism');
var ws=p.windows();var mains=ws.filter(w=>{try{return w.attributes.byName('AXMain').value()===true}catch(e){return false}});
var w=mains.length===1?mains[0]:ws[0]; var d=null;try{d=w.attributes.byName('AXDocument').value()}catch(e){}
var main=false;try{main=w.attributes.byName('AXMain').value()}catch(e){}
JSON.stringify({title:w.name(),url:d,main:main});''', jxa=True))


def _active():
    return osa('tell application "System Events" to get frontmost of process "Prism"') == 'true'


@contextlib.contextmanager
def foreground(path: Path):
    from .placement import enforce
    enforce()
    previous = osa('tell application "System Events" to get bundle identifier of first process whose frontmost is true')
    app = int(osa('tell application "System Events" to get unix id of process "Prism"'))
    osa('tell application "System Events" to set frontmost of process "Prism" to true')
    osa('''var p=Application('System Events').processes.byName('Prism');
var ws=p.windows().filter(w=>{try{return w.attributes.byName('AXDocument').value()===''' + json.dumps(path.resolve().as_uri()) + '''}catch(e){return false}});
if(ws.length!==1)throw Error('Exact target window is absent');
ws[0].actions.byName('AXRaise').perform();
ws[0].attributes.byName('AXMain').value=true;''', jxa=True)
    from urllib.parse import unquote, urlparse
    # A script's native progress panel can outlive its completion token briefly.
    # Wait for the document itself to become main before any canvas operation.
    deadline = time.monotonic() + 5
    while True:
        state = _window_state()
        actual = Path(unquote(urlparse(state['url']).path)).resolve() if state['url'] else None
        if actual == path.resolve() and state.get('main') is True and _active():
            break
        if time.monotonic() >= deadline:
            raise PrismError('UI_TARGET_MISMATCH', 'Native UI adapter did not acquire its exact checkpoint window.', window=state)
        time.sleep(.1)
    # AX menu/dialog actions target Prism explicitly and do not require global
    # keyboard focus. Mouse and keyboard helpers independently require focus
    # immediately before posting their global events.
    try:
        yield app
    finally:
        if previous and previous != 'com.GraphPad.Prism' and _active():
            with contextlib.suppress(PrismError):
                osa(f'tell application "System Events" to set frontmost of first process whose bundle identifier is {apple_quote(previous)} to true')


def close_saved_document(path: Path):
    """Close one clean owned checkpoint after its save script has returned."""
    from .runtime import windows
    target = str(path.resolve())

    def ready():
        rows = windows()
        selected = [row for row in rows if row.get('path') == target]
        if len(selected) != 1:
            raise PrismError('UI_TARGET_MISMATCH', 'Expected one saved checkpoint window before closing.', path=target)
        if selected[0].get('edited') is not False:
            raise PrismError('UNSAVED_CHANGES', 'The checkpoint is not confirmed saved; native close was refused.', path=target)
        if any(row.get('modal') for row in rows):
            raise PrismError('PRISM_MODAL', 'A native dialog prevents closing the saved checkpoint.')
        return selected[0]

    before = ready()
    with foreground(path):
        ready()
        menu('File', 'Close')
        deadline = time.monotonic() + 5
        while any(row.get('path') == target for row in windows()):
            if time.monotonic() >= deadline:
                raise PrismError('UI_CLOSE_UNVERIFIED', 'The saved checkpoint window did not close.', path=target)
            time.sleep(.1)
    return {'adapter': 'exact_saved_checkpoint_File_Close', 'path': target,
            'saved_state_before_close': before['edited'] is False, 'window_closed': True}


def key(app, code: int, flags=0):
    import Quartz
    if not _active():
        raise PrismError('FOCUS_LOST', 'Prism lost foreground focus; no keyboard event was sent.')
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    time.sleep(.15)


def _dialog_count(title: str) -> int:
    value = osa(f'tell application "System Events" to tell process "Prism" to get count of (every window whose name is {apple_quote(title)})')
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value):
        raise PrismError('UNEXPECTED_DIALOG', 'The native dialog count could not be verified.', expected=title, actual=value)
    return int(value)


def _require_dialog_absent(title: str):
    matches = _dialog_count(title)
    if matches:
        raise PrismError('UNEXPECTED_DIALOG', 'A matching native dialog was already open before this action.', expected=title, matches=matches)


def _dialog(title):
    deadline = time.monotonic() + 5
    matches = _dialog_count(title)
    while matches != 1 and time.monotonic() < deadline:
        time.sleep(.1)
        matches = _dialog_count(title)
    if matches != 1:
        raise PrismError('UNEXPECTED_DIALOG', 'Prism did not show one expected native dialog.', expected=title, actual=_window_state(), matches=matches)
    from .placement import enforce
    enforce()
    matches = _dialog_count(title)
    if matches != 1:
        raise PrismError('UNEXPECTED_DIALOG', 'Native dialog identity changed during placement.', expected=title, matches=matches)


def close_dialog(title: str, button='OK'):
    """Wait until a native dialog has actually closed before another action."""
    matches = _dialog_count(title)
    if matches != 1:
        raise PrismError('UNEXPECTED_DIALOG', 'Expected one owned dialog before closing it.', expected=title, matches=matches)
    result = osa(f'''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
if (count of (every window whose name is {apple_quote(title)})) is not 1 then error "Native dialog identity changed before closing"
perform action "AXPress" of button {apple_quote(button)} of window {apple_quote(title)}
repeat 60 times
if not (exists window {apple_quote(title)}) then return "closed"
delay 0.05
end repeat
error "Native dialog did not close"
end tell''')
    if result != 'closed':
        raise PrismError('UI_DIALOG_NOT_CLOSED', 'Native dialog closure was not confirmed.', expected=title, actual=result)


def classic_format_menu():
    """Open the verified classic graph dialog despite its contextual menu name."""
    _require_dialog_absent('Format Graph')
    token = uuid.uuid4().hex
    result = osa(f'''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
if exists window "Format Graph" then error "Format Graph was already open before this dispatch"
set candidates to every menu item of menu 1 of menu bar item "Change" of menu bar 1
set matchingItems to {{}}
repeat with candidate in candidates
set candidateID to missing value
try
set candidateID to value of attribute "AXIdentifier" of candidate
end try
if candidateID is "Change.formatGraph" then set end of matchingItems to contents of candidate
end repeat
if (count of matchingItems) is not 1 then error "Expected one native format graph action"
set chosenItem to item 1 of matchingItems
if not enabled of chosenItem then error "Native format graph action is disabled"
click chosenItem
return {apple_quote(token)}
end tell''')
    if result != token:
        raise PrismError('UI_ACTION_NOT_DISPATCHED', 'The native format dialog did not confirm this dispatch.')
    _dialog('Format Graph')


def classic_popup(description: str, value: str):
    """Select a named value in a native classic graph popup and read it back."""
    result = osa(f'''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
set c to first pop up button of tab group 1 of window "Format Graph" whose description is {apple_quote(description)}
if not enabled of c then error "Required native graph control is disabled"
perform action "AXPress" of c
delay 0.2
if not (exists menu 1 of c) then
perform action "AXPress" of c
delay 0.2
end if
perform action "AXPress" of menu item {apple_quote(value)} of menu 1 of c
delay 0.15
repeat 20 times
if value of c is {apple_quote(value)} then exit repeat
delay 0.05
end repeat
return value of c
end tell''')
    if result != value:
        raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native popup did not retain the requested setting.', control=description, expected=value, actual=result)
    return result


def classic_dataset(column: str, label: str):
    if column not in ('A', 'B'):
        raise PrismError('INVALID_ARGUMENT', 'The current classic adapter supports columns A and B.')
    # Enumerate even an already-selected suffix: another source table can use
    # the same column/label. Selecting the sole match also closes the popup.
    suffix = f':{column}:{label}'
    result = osa(f'''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
set c to first pop up button of tab group 1 of window "Format Graph" whose description is "Data set"
if not enabled of c then error "Required native dataset control is disabled"
perform action "AXPress" of c
delay 0.2
if not (exists menu 1 of c) then
perform action "AXPress" of c
delay 0.2
end if
set matches to every menu item of menu 1 of c whose name ends with {apple_quote(suffix)}
if (count of matches) is not 1 then error "Expected one exact source dataset in native graph"
set chosenName to name of item 1 of matches
perform action "AXPress" of item 1 of matches
delay 0.2
repeat 20 times
if value of c is chosenName then return chosenName
delay 0.05
end repeat
error "Native source dataset selection was not retained"
end tell''')
    if not isinstance(result, str) or not result.endswith(suffix):
        raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native dataset selection did not retain the requested source column.', column=column, actual=result)
    return result


def classic_color(description: str, color: str, opacity: float = 1):
    target = f'first color well of tab group 1 of window "Format Graph" whose description is {apple_quote(description)}'
    return native_color(target, color, opacity, description)


def native_color(target: str, color: str, opacity: float = 1, control='native color well'):
    """Set a color through NSColorPanel, committing its text via AXConfirm.

    AXValue alone changes the displayed text but does not notify the color well.
    AXConfirm is required and the originating well is checked before returning.
    """
    if (not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color)
            or type(opacity) not in (int, float) or not math.isfinite(opacity) or not 0 <= opacity <= 1):
        raise PrismError('INVALID_ARGUMENT', 'Color must be #RRGGBB and opacity must be from zero to one.')
    # In this Prism build, changing opacity can update NSColorPanel without
    # notifying the originating well. A one-bit RGB change followed by the
    # intended color commits the complete RGBA value through native actions.
    transient_color = f'{int(color[1:], 16) ^ 1:06X}'
    osa(f'''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
if exists window "Colors" then
perform action "AXPress" of (first button of window "Colors" whose description is "close button")
delay 0.25
end if
set c to {target}
if not enabled of c then error "Required native color well is disabled"
perform action "AXPress" of c
delay 0.2
end tell''')
    from .placement import enforce
    enforce()
    result = osa(f'''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
tell window "Colors"
if value of pop up button 1 of splitter group 1 is not "RGB Sliders" then error "Native color panel must show RGB sliders"
tell text field "Opacity" of splitter group 1
set value to "{opacity * 100:.8g}"
perform action "AXConfirm"
end tell
delay 0.15
tell text field 4 of splitter group 1
set value to {apple_quote(transient_color)}
perform action "AXConfirm"
delay 0.15
set value to {apple_quote(color[1:])}
perform action "AXConfirm"
end tell
delay 0.15
perform action "AXPress" of (first button whose description is "close button")
end tell
delay 0.2
return value of ({target})
end tell''')
    expected = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)] + [opacity]
    try:
        parts = result.split()
        actual = [float(v) for v in parts[1:]] if parts[0] == 'rgb' else []
    except (ValueError, TypeError, AttributeError, IndexError):
        actual = []
    finite_rgba = len(actual) == 4 and all(math.isfinite(v) and 0 <= v <= 1 for v in actual)
    quantize = lambda values: [int(math.floor(v * 255 + .5)) for v in values]
    expected_8bit = quantize(expected)
    actual_8bit = quantize(actual) if finite_rgba else None
    if actual_8bit != expected_8bit:
        raise PrismError('UI_COLOR_NOT_APPLIED', 'Native color well did not retain the requested RGBA value.', control=control, expected=expected, actual=result)
    return {'control': control, 'color': color.upper(), 'opacity': opacity, 'native_value': result,
            'verification': 'finite_exact_8bit_rgba', 'native_rgba_8bit': actual_8bit}


def _committed_scale_bar_hidden(expected_visibility: str):
    """Verify the native model after applying the axis-visibility dialog.

    A newly hidden axis frame can enable its scale bar during OK, even after
    the open dialog reported the checkbox off. A second native transaction
    removes it, and a reopened dialog verifies the committed state.
    """
    changed = False
    for _ in range(2):
        menu('Change', 'Frame and Origin...')
        _dialog('Format Axes')
        values = osa('''tell application "System Events" to tell process "Prism"
set t to tab group 1 of window "Format Axes"
if value of radio button "Frame and Origin" of t is not 1 then error "Unexpected axes tab"
return (value of pop up button 7 of t as text) & linefeed & (value of checkbox "Show Scale Bar" of t as text)
end tell''').splitlines()
        if values == [expected_visibility, '0']:
            close_dialog('Format Axes', button='Cancel')
            return {'native_scale_bar': False, 'committed_state_reopened': True,
                    'second_native_transaction_required': changed}
        if values != [expected_visibility, '1']:
            raise PrismError('UI_FORMAT_NOT_APPLIED', 'Committed native axis visibility changed unexpectedly.', actual=values)
        if changed:
            close_dialog('Format Axes', button='Cancel')
            break
        osa('''tell application "System Events" to tell process "Prism"
set c to checkbox "Show Scale Bar" of tab group 1 of window "Format Axes"
if not enabled of c then error "Native scale bar cannot be changed"
perform action "AXPress" of c
if value of c is not 0 then error "Native scale bar did not switch off"
end tell''')
        close_dialog('Format Axes')
        changed = True
    raise PrismError('UI_FORMAT_NOT_APPLIED', 'The native scale bar did not remain hidden after applying the dialog.')


def axes_frame(width_inches: float, height_inches: float, *, hide_axes=False, color='#4A4A4A'):
    """Set real native axis lengths; font/marker sizes stay in physical points."""
    if any(type(x) not in (int, float) or not math.isfinite(x) or not .1 <= x <= 20 for x in (width_inches, height_inches)):
        raise PrismError('INVALID_ARGUMENT', 'Native axis lengths must be between 0.1 and 20 inches.')
    if type(hide_axes) is not bool or not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        raise PrismError('INVALID_ARGUMENT', 'Axis visibility must be boolean and color must be #RRGGBB.')
    _require_dialog_absent('Format Axes')
    menu('Change', 'Frame and Origin...')
    _dialog('Format Axes')
    units = osa('tell application "System Events" to tell process "Prism" to get value of every static text of tab group 1 of window "Format Axes"')
    if not re.search(r'(?<!\w)in\.(?!\w)', units) or re.search(r'(?<!\w)(?:cm|mm)\.(?!\w)', units):
        raise PrismError('UNSUPPORTED_UI_UNITS', 'Native axis length controls must display inches; no size fields were changed.', units=units)
    visibility = 'Hide both X and Y' if hide_axes else 'Show both X and Y'
    values = osa(f'''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
set t to tab group 1 of window "Format Axes"
if value of radio button "Frame and Origin" of t is not 1 then error "Unexpected axes tab"
if (count of text fields of t) is not 6 then error "Unrecognized native axis size controls"
if not (exists checkbox "Show Scale Bar" of t) then error "Unrecognized native scale bar control"
set c to pop up button 10 of t
if value of c is not "Custom" then
perform action "AXPress" of c
delay 0.2
perform action "AXPress" of menu item "Custom" of menu 1 of c
delay 0.2
end if
set value of text field 3 of t to "{height_inches:.12g}"
perform action "AXConfirm" of text field 3 of t
delay 0.15
set value of text field 4 of t to "{width_inches:.12g}"
perform action "AXConfirm" of text field 4 of t
delay 0.15
set c to pop up button 7 of t
if value of c is not {apple_quote(visibility)} then
perform action "AXPress" of c
delay 0.2
perform action "AXPress" of menu item {apple_quote(visibility)} of menu 1 of c
delay 0.15
end if
if {str(hide_axes).lower()} and value of checkbox "Show Scale Bar" of t is not 0 then
perform action "AXPress" of checkbox "Show Scale Bar" of t
repeat 20 times
if value of checkbox "Show Scale Bar" of t is 0 then exit repeat
delay 0.05
end repeat
end if
if not {str(hide_axes).lower()} then
set c to pop up button 9 of t
perform action "AXPress" of c
delay 0.2
perform action "AXPress" of menu item "1/2 pt" of menu 1 of c
delay 0.15
end if
return (value of text field 4 of t as text) & linefeed & (value of text field 3 of t as text) & linefeed & (value of pop up button 7 of t as text) & linefeed & (value of checkbox "Show Scale Bar" of t as text) & linefeed & (value of pop up button 9 of t as text)
end tell''')
    # This pinned native dialog displays lengths to two decimal places, even
    # when it accepts more precise input. Verify that display resolution only;
    # the native export remains the separate evidence for final physical size.
    try:
        width, height, actual_visibility, scale_bar, line_width = (part.strip() for part in values.splitlines())
        actual_dimensions = [float(width), float(height)]
        confirmed = (all(math.isfinite(v) for v in actual_dimensions)
                     and all(abs(actual - expected) <= .005 + 1e-12
                             for actual, expected in zip(actual_dimensions, (width_inches, height_inches)))
                     and actual_visibility == visibility and scale_bar in ('0', '1')
                     and (hide_axes or line_width == '1/2 pt'))
    except (ValueError, TypeError, AttributeError):
        confirmed = False
    if not confirmed:
        raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native axes did not retain the requested dimensions, visibility, scale bar, or line width.',
                         expected_dimensions=[width_inches, height_inches], expected_visibility=visibility,
                         expected_scale_bar=False if hide_axes else 'unchanged', actual=values,
                         field_resolution_inches=.01)
    # Hiding both axes can asynchronously re-enable the scale bar before or
    # after this draft readback. The mandatory committed-dialog check below
    # corrects it once and requires zero before the operation can succeed.
    # With both axes and the scale bar hidden, Prism disables axis color and
    # thickness. Those invisible properties cannot and need not be changed.
    applied_color = ({'visible': False, 'native_line_width': line_width} if hide_axes else
                     native_color('color well 5 of tab group 1 of window "Format Axes"', color))
    close_dialog('Format Axes')
    committed_scale = _committed_scale_bar_hidden(visibility)
    return {'axis_width_inches': width_inches, 'axis_height_inches': height_inches,
            'hide_axes': hide_axes, 'native_fields': values, 'axis_color': applied_color,
            'native_axis_width_inches': actual_dimensions[0], 'native_axis_height_inches': actual_dimensions[1],
            'native_scale_bar': False, 'committed_scale_bar': committed_scale, 'field_resolution_inches': .01}


def _symbol_palette(app: int):
    """Return one owned native palette with the empirically verified geometry."""
    import Quartz
    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, 0)
    if windows is None:
        raise PrismError('UNSUPPORTED_SYMBOL_PALETTE', 'Native symbol palette geometry is unavailable.')
    rows = [window for window in windows if window.get('kCGWindowOwnerPID') == app
            and window.get('kCGWindowName') == 'Data Point Symbols']
    if len(rows) != 1:
        raise PrismError('UNSUPPORTED_SYMBOL_PALETTE', 'Expected one native symbol palette.')
    try:
        bounds = {key: float(rows[0]['kCGWindowBounds'][key]) for key in ('X', 'Y', 'Width', 'Height')}
        number = int(rows[0]['kCGWindowNumber'])
        if not all(math.isfinite(value) for value in bounds.values()) or number <= 0:
            raise ValueError('Invalid palette bounds or identity')
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise PrismError('UNSUPPORTED_SYMBOL_PALETTE', 'Native symbol palette bounds or identity are invalid.') from exc
    if (bounds['Width'], bounds['Height']) != (233, 205):
        raise PrismError('UNSUPPORTED_SYMBOL_PALETTE', 'Native symbol palette geometry changed.')
    return number, bounds


def classic_bordered_circle():
    """Choose Prism's second circle, which exposes independent fill/border.

    The classic symbol palette exposes no AX children. The coordinate is a
    validated native UI choice, never graph or scientific summary geometry.
    """
    import Quartz
    osa('''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
perform action "AXPress" of (first pop up button of tab group 1 of window "Format Graph" whose description is "Symbol shape")
end tell''')
    time.sleep(.2)
    app = int(osa('tell application "System Events" to get unix id of process "Prism"'))
    number, bounds = _symbol_palette(app)
    if not _active():
        raise PrismError('UNSUPPORTED_SYMBOL_PALETTE', 'Native symbol palette geometry or foreground ownership changed.')
    if _symbol_palette(app) != (number, bounds):
        raise PrismError('UI_TARGET_MISMATCH', 'Native symbol palette moved or changed before selection.')
    point = (bounds['X'] + 44, bounds['Y'] + 16)
    for event_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, event_type, point, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventSetFlags(event, 0)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 1)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(.05)
    time.sleep(.15)
    deadline = time.monotonic() + 2
    while True:
        windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, 0)
        if windows is None:
            raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native symbol palette closure could not be verified.')
        if not any(window.get('kCGWindowOwnerPID') == app and window.get('kCGWindowName') == 'Data Point Symbols' for window in windows):
            break
        if time.monotonic() >= deadline:
            raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native symbol palette did not close after selection.')
        time.sleep(.05)
    enabled = osa('''tell application "System Events" to tell process "Prism" to get enabled of (first color well of tab group 1 of window "Format Graph" whose description is "Symbol border color")''')
    if enabled != 'true':
        raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native symbol border was not enabled after selecting the bordered circle.')
    return {'symbol': 'native_circle_with_independent_fill_and_border', 'palette_size': [233, 205]}


def owned_window(app: int, path: Path):
    import Quartz
    from urllib.parse import unquote, urlparse
    state = _window_state()
    actual = Path(unquote(urlparse(state['url']).path)).resolve() if state.get('url') else None
    if actual != path.resolve() or state.get('main') is not True:
        raise PrismError('UI_TARGET_MISMATCH', 'The exact checkpoint must remain the native main window.')
    rows = [w for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
            if w.get('kCGWindowOwnerPID') == app and w.get('kCGWindowLayer') == 0
            and w.get('kCGWindowName') == path.name]
    if len(rows) != 1:
        raise PrismError('UI_TARGET_MISMATCH', 'Expected one visible checkpoint window for text selection.')
    try:
        raw = rows[0]['kCGWindowBounds']
        if any(isinstance(raw[key], bool) or not isinstance(raw[key], (int, float))
               or not math.isfinite(raw[key]) for key in ('X','Y','Width','Height')):
            raise ValueError('Invalid native window dimensions')
        bounds = {key: float(raw[key]) for key in ('X','Y','Width','Height')}
        if bounds['Width'] <= 0 or bounds['Height'] <= 0:
            raise ValueError('Native window dimensions must be positive')
    except (KeyError, TypeError, ValueError) as exc:
        raise PrismError('UI_TARGET_MISMATCH', 'Native window geometry is invalid.') from exc
    return int(rows[0]['kCGWindowNumber']), bounds


def _blank_canvas(app: int, path: Path, evidence: Path, text=''):
    """Find blank native page space for an object action, never graph data."""
    import Quartz
    import Foundation
    evidence.mkdir(parents=True, exist_ok=True)
    number, bounds = owned_window(app, path)
    image = evidence/'annotation-before.png'
    subprocess.run(['/usr/sbin/screencapture', '-x', '-o', '-l', str(number), str(image)], check=True, timeout=8)
    source = Quartz.CGImageSourceCreateWithURL(Foundation.NSURL.fileURLWithPath_(str(image)), None)
    bitmap = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if Quartz.CGImageGetBitsPerPixel(bitmap) != 32 or Quartz.CGImageGetBitsPerComponent(bitmap) != 8:
        raise PrismError('UNSUPPORTED_SCREENSHOT', 'Blank-space detection requires the verified 32-bit screenshot format.')
    width, height = Quartz.CGImageGetWidth(bitmap), Quartz.CGImageGetHeight(bitmap)
    stride = Quartz.CGImageGetBytesPerRow(bitmap)
    data = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(bitmap)))
    area = json.loads(osa('''var ws=Application('System Events').processes.byName('Prism').windows().filter(w=>{try{return w.attributes.byName('AXDocument').value()===''' + json.dumps(path.resolve().as_uri()) + '''}catch(e){return false}});
if(ws.length!==1)throw Error('Exact canvas document is absent');
var a=ws[0].splitterGroups[0].scrollAreas[0];
JSON.stringify({p:a.position(),s:a.size()});''', jxa=True))
    sx, sy = width/bounds['Width'], height/bounds['Height']
    left = max(0, int((area['p'][0]-bounds['X']+12)*sx))
    right = min(width, int((area['p'][0]-bounds['X']+area['s'][0]-12)*sx))
    top = max(0, int((area['p'][1]-bounds['Y']+area['s'][1]*.4)*sy))
    bottom = min(height-1, int((area['p'][1]-bounds['Y']+area['s'][1]-35)*sy))
    pad = max(1, int(24*sy))
    required = min(430, max(180, len(text)*8))*sx
    increment = max(1, int(3*sx))
    point = None
    for y in range(bottom-pad, top, -max(1, int(8*sy))):
        beginning = None
        for x in range(left, right, increment):
            empty = all(min(data[yy*stride+x*4:yy*stride+x*4+4]) >= 250
                        for yy in (y, y+pad//2, y+pad))
            if empty and beginning is None: beginning = x
            if not empty: beginning = None
            if beginning is not None and x-beginning >= required:
                point = (bounds['X']+(beginning+10*sx)/sx, bounds['Y']+(y+3*sy)/sy)
                break
        if point: break
    if not point:
        raise PrismError('NO_BLANK_ANNOTATION_SPACE', 'No sufficiently large blank page region was found for safe native text insertion.')
    if owned_window(app, path) != (number, bounds) or not _active():
        raise PrismError('UI_TARGET_MISMATCH', 'Window geometry or ownership changed before contextual Paste.')
    return number, bounds, image, point


def focus_canvas(app: int, path: Path, evidence: Path):
    """Focus the layout/graph canvas before Select All, not the navigator."""
    import Quartz
    number, bounds, image, point = _blank_canvas(app, path, evidence)
    for event_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, event_type, point, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventSetFlags(event, 0)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 1)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(.05)
    time.sleep(.15)
    result = {'window_number': number, 'bounds': bounds, 'point': point, 'screenshot': str(image)}
    write_json(evidence/'canvas-focus.json', result)
    return result


def contextual_paste(app: int, path: Path, text: str, evidence: Path):
    """Paste into verified blank page space using Prism's native context action.

    Native code inspection established that contextual Paste uses the clicked
    page location. Pixel checks identify a UI insertion region, never graph data.
    """
    import Quartz
    number, bounds, image, point = _blank_canvas(app, path, evidence, text)
    insertion_point = point
    write_json(evidence/'context-paste-attempt.json', {'point': point, 'bounds': bounds, 'window_number': number})
    for event_type in (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, event_type, point, Quartz.kCGMouseButtonRight)
        Quartz.CGEventSetFlags(event, 0)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(.05)
    # Prism's classic contextual menu is a separate layer-101 native window,
    # absent from System Events' AX tree. Read only that owned menu with OCR.
    menus = [w for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, 0)
             if w.get('kCGWindowOwnerPID') == app and w.get('kCGWindowLayer') == 101]
    if len(menus) != 1:
        raise PrismError('CONTEXT_MENU_UNAVAILABLE', 'Expected one owned native context menu.')
    menu_number = int(menus[0]['kCGWindowNumber'])
    menu_bounds = dict(menus[0]['kCGWindowBounds'])
    menu_image = evidence/'context-menu.png'
    subprocess.run(['/usr/sbin/screencapture', '-x', '-o', '-l', str(menu_number), str(menu_image)], check=True, timeout=8)
    result = subprocess.run([sys.executable, '-m', 'prism_mcp.native_ui', str(menu_image)], capture_output=True,
                            text=True, check=True, timeout=60)
    matches = [r for r in json.loads(result.stdout) if r['text'].strip().casefold() == 'paste']
    if len(matches) != 1 or not _active():
        raise PrismError('CONTEXT_MENU_UNAVAILABLE', 'Contextual Paste was not uniquely identified while Prism retained focus.')
    x,y,w,h = matches[0]['bounds']
    point = (menu_bounds['X']+menu_bounds['Width']*(x+w/2), menu_bounds['Y']+menu_bounds['Height']*(1-y-h/2))
    for event_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, event_type, point, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventSetFlags(event, 0)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 1)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(.05)
    write_json(evidence/'context-paste-selection.json', {'menu_bounds': menu_bounds,
               'menu_window_number': menu_number, 'paste_match': matches[0], 'click': point})
    time.sleep(.5)
    return {'insertion': 'native_contextual_paste', 'blank_space_screen_point': insertion_point,
            'menu_selection_screen_point': point, 'screenshot': str(image)}


def _selection_canvas(path: Path):
    return json.loads(osa('''var ws=Application('System Events').processes.byName('Prism').windows().filter(w=>{try{return w.attributes.byName('AXDocument').value()===''' + json.dumps(path.resolve().as_uri()) + '''}catch(e){return false}});
if(ws.length!==1)throw Error('Exact canvas document is absent');
var a=ws[0].splitterGroups[0].scrollAreas[0];JSON.stringify({p:a.position(),s:a.size()});''', jxa=True))


def _canvas_ocr_rows(rows, area, bounds):
    """Exclude navigator, toolbar, and status text from a canvas selection."""
    try:
        numbers = [*area['p'], *area['s'], *(bounds[k] for k in ('X','Y','Width','Height'))]
        if (len(area['p']) != 2 or len(area['s']) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in numbers)
                or min(*area['s'], bounds['Width'], bounds['Height']) <= 0):
            raise ValueError('Invalid canvas geometry')
        left = (area['p'][0]-bounds['X'])/bounds['Width']
        right = left+area['s'][0]/bounds['Width']
        top = 1-(area['p'][1]-bounds['Y'])/bounds['Height']
        bottom = top-area['s'][1]/bounds['Height']
        if not (-1e-12 <= left < right <= 1+1e-12 and -1e-12 <= bottom < top <= 1+1e-12):
            raise ValueError('Canvas is outside the owned window')
        left,right,bottom,top = max(0,left),min(1,right),max(0,bottom),min(1,top)
        result = []
        for row in rows:
            x,y,width,height = row['bounds']
            if (not isinstance(row['text'], str)
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in row['bounds'])
                    or width <= 0 or height <= 0):
                raise ValueError('Invalid text bounds')
            if left <= x and x+width <= right and bottom <= y and y+height <= top:
                result.append(row)
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise PrismError('TEXT_SELECTION_UNAVAILABLE', 'Canvas text recognition has invalid ownership geometry.') from exc


def select_text(app: int, path: Path, text: str, evidence: Path, *, add_to_selection=False):
    """Locate a just-inserted native label in its own window using local OCR.

    This selects a UI object only. No observed geometry is used to make a graph.
    A unique match, unchanged window bounds, and foreground ownership are required.
    """
    import Quartz
    def window(): return owned_window(app, path)
    number, bounds = window()
    area = _selection_canvas(path)
    image = evidence / 'annotation-window.png'
    subprocess.run(['/usr/sbin/screencapture', '-x', '-o', '-l', str(number), str(image)], check=True, timeout=8)
    try:
        result = subprocess.run([sys.executable, '-m', 'prism_mcp.native_ui', str(image)],
                                capture_output=True, text=True, check=True, timeout=60)
        rows = json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError) as exc:
        raise PrismError('TEXT_SELECTION_UNAVAILABLE', 'Local native text recognition did not complete; checkpoint remains unmodified by selection.') from exc
    normalize = lambda s: re.sub(r'[^\w]', '', s).casefold().replace('0', 'o')
    wanted = normalize(text)
    def matching(candidates):
        candidates = _canvas_ocr_rows(candidates, area, bounds)
        found = [row for row in candidates if wanted and wanted in normalize(row['text'])]
        if not found and len(wanted) >= 15:
            from difflib import SequenceMatcher
            found = [row for row in candidates if SequenceMatcher(None, wanted, normalize(row['text'])).ratio() >= .96]
        return found
    matches = matching(rows)
    recognition_pass = 'whole_window'
    if not matches:
        # Small native labels can fall below Vision's full-window text scale.
        # Recognize overlapping UI regions of the same unchanged screenshot.
        try:
            result = subprocess.run([sys.executable, '-m', 'prism_mcp.native_ui', str(image), '--tiles'],
                                    capture_output=True, text=True, check=True, timeout=60)
            matches = matching(json.loads(result.stdout))
            recognition_pass = 'overlapping_window_regions'
        except (subprocess.SubprocessError, ValueError) as exc:
            raise PrismError('TEXT_SELECTION_UNAVAILABLE', 'Local region text recognition did not complete.') from exc
    if len(matches) != 1:
        raise PrismError('TEXT_SELECTION_UNAVAILABLE', 'The new label was not uniquely readable in its own window.', match_count=len(matches))
    current_window = window()
    active = _active()
    if current_window != (number, bounds) or _selection_canvas(path) != area or not active:
        details = {'before_window_number': number, 'before_bounds': bounds,
                   'after_window_number': current_window[0], 'after_bounds': current_window[1],
                   'prism_frontmost': active}
        write_json(evidence/'selection-ownership-change.json', details)
        raise PrismError('UI_TARGET_MISMATCH', 'Window geometry or foreground ownership changed before text selection.', **details)
    x, y, width, height = matches[0]['bounds']
    point = (bounds['X'] + bounds['Width'] * (x + width/2),
             bounds['Y'] + bounds['Height'] * (1-y-height/2))
    for event_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, event_type, point, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskShift if add_to_selection else 0)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 1)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(.05)
    return {'recognition': 'local_Apple_Vision', 'window_number': number,
            'bounds': bounds, 'canvas': area, 'text_match': matches[0], 'screenshot': str(image), 'recognition_pass': recognition_pass}


def select_enclosed_graphs(app: int, path: Path, title: str, evidence: Path, *, excluded_frames=(), frame_only=False):
    """Marquee the two native objects at one layout graph's UI frame.

    The four blue selection handles identify a UI object frame, never plotted
    measurements, curves, or summary marks. Native marquee selection uses
    intersection, so previous groups' UI frames must be explicitly excluded.
    """
    import AppKit
    import Quartz
    evidence.mkdir(parents=True, exist_ok=True)
    chosen = select_text(app, path, title, evidence)
    time.sleep(.2)
    number, bounds = owned_window(app, path)
    screenshot = evidence/'selected-frame.png'
    subprocess.run(['/usr/sbin/screencapture', '-x', '-o', '-l', str(number), str(screenshot)],
                   check=True, timeout=8)
    bitmap = AppKit.NSBitmapImageRep.imageRepWithContentsOfFile_(str(screenshot))
    if (bitmap is None or bitmap.samplesPerPixel() != 4 or bitmap.bitsPerPixel() != 32
            or bitmap.bitsPerSample() != 8 or not bitmap.hasAlpha()
            or bitmap.bitmapFormat() != 2 or bitmap.isPlanar()
            or bitmap.colorSpaceName() != 'NSCalibratedRGBColorSpace'):
        raise PrismError('UNSUPPORTED_SELECTION_FRAME', 'Native UI frame inspection requires the verified RGBA screenshot format.')
    width, height = bitmap.pixelsWide(), bitmap.pixelsHigh()
    sx, sy = width/bounds['Width'], height/bounds['Height']
    data, stride = bytes(bitmap.bitmapData()), bitmap.bytesPerRow()
    if width <= 0 or height <= 0 or stride < width*4 or len(data) < stride*height:
        raise PrismError('UNSUPPORTED_SELECTION_FRAME', 'Native screenshot storage is incomplete or inconsistent.')
    area = json.loads(osa('''var ws=Application('System Events').processes.byName('Prism').windows().filter(w=>{try{return w.attributes.byName('AXDocument').value()===''' + json.dumps(path.resolve().as_uri()) + '''}catch(e){return false}});
if(ws.length!==1)throw Error('Exact canvas document is absent');var a=ws[0].splitterGroups[0].scrollAreas[0];JSON.stringify({p:a.position(),s:a.size()});''', jxa=True))
    left, top = (max(0, int((area['p'][i]-bounds[k])*scale)) for i,k,scale in ((0,'X',sx),(1,'Y',sy)))
    right = min(width, int((area['p'][0]+area['s'][0]-bounds['X'])*sx))
    bottom = min(height, int((area['p'][1]+area['s'][1]-bounds['Y'])*sy))
    pixels = set()
    for y in range(top, bottom):
        for x in range(left, right):
            red, green, blue, alpha = data[y*stride+x*4:y*stride+x*4+4]
            if alpha > 250 and blue > 145 and green > red+10 and blue > green+20 and red > 25:
                pixels.add((x, y))
    handles = []
    while pixels:
        component = {pixels.pop()}
        pending = list(component)
        while pending:
            x,y = pending.pop()
            for neighbor in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                if neighbor in pixels:
                    pixels.remove(neighbor);component.add(neighbor);pending.append(neighbor)
        xs, ys = zip(*component)
        box = (min(xs), min(ys), max(xs)+1, max(ys)+1)
        w,h = box[2]-box[0],box[3]-box[1]
        # The visible page starts eight logical points inside this build's AX
        # canvas. A group at page X=0 can have half of its left handles clipped.
        clipped_left_handle = frame_only and box[0] <= left+12*sx and w >= 2
        if (7*sx <= w <= 10*sx or clipped_left_handle and w <= 10*sx) and 6*sy <= h <= 9*sy and len(component)/(w*h) >= .86:
            handles.append(box)
    if len(handles) != 4:
        raise PrismError('UNSUPPORTED_SELECTION_FRAME', 'Exactly four native rectangular selection handles are required.', handle_count=len(handles))
    handles.sort(key=lambda box: (box[1], box[0]))
    a,b,c,d = handles
    if (max(abs(a[1]-b[1]),abs(c[1]-d[1]),abs(a[0]-c[0]),abs(b[0]-d[0])) > 2
            or b[0]-a[0] < 40*sx or c[1]-a[1] < 40*sy):
        raise PrismError('UNSUPPORTED_SELECTION_FRAME', 'Native selection handles do not form one unambiguous graph frame.')
    x0,y0 = bounds['X']+a[0]/sx-4, bounds['Y']+a[1]/sy-4
    x1,y1 = bounds['X']+d[2]/sx+4, bounds['Y']+d[3]/sy+4
    result = {'selection': 'native_marquee_from_selected_graph_UI_handles', 'title_selection': chosen,
              'frame_screenshot': str(screenshot), 'handle_boxes_pixels': handles,
              'screen_rectangle': [x0,y0,x1,y1], 'window_number': number, 'window_bounds': bounds}
    if frame_only:
        return {**result, 'selection': 'selected_native_group_UI_frame', 'marquee_dispatched': False}
    if not (area['p'][0] < x0 < x1 < area['p'][0]+area['s'][0]
            and area['p'][1] < y0 < y1 < area['p'][1]+area['s'][1]):
        raise PrismError('UNSUPPORTED_SELECTION_FRAME', 'The selected graph is clipped by the canvas viewport.')
    for prior in excluded_frames:
        if not isinstance(prior, dict) or prior.get('window_number') != number or prior.get('window_bounds') != bounds:
            raise PrismError('UI_TARGET_MISMATCH', 'The canvas moved after the previous panel frame was recorded.')
        rectangle = prior.get('screen_rectangle')
        if (not isinstance(rectangle, (list, tuple)) or len(rectangle) != 4
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in rectangle)
                or rectangle[0] >= rectangle[2] or rectangle[1] >= rectangle[3]):
            raise PrismError('UNSUPPORTED_SELECTION_FRAME', 'A previous panel has an invalid native UI frame.')
        px0,py0,px1,py1 = rectangle
        if x0 < px1 and x1 > px0 and y0 < py1 and y1 > py0:
            raise PrismError('UI_SELECTION_OVERLAP', 'Panel frames overlap; increase the requested panel spacing before composing.')
    focus_canvas(app, path, evidence/'clear-selection')
    if owned_window(app, path) != (number, bounds) or not _active():
        raise PrismError('UI_TARGET_MISMATCH', 'The native graph frame changed before rectangle selection.')
    def post(kind, point):
        event = Quartz.CGEventCreateMouseEvent(None, kind, point, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventSetFlags(event, 0)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 1)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    try:
        post(Quartz.kCGEventLeftMouseDown, (x0,y0))
        time.sleep(.1)
        for step in range(1,9):
            post(Quartz.kCGEventLeftMouseDragged, (x0+(x1-x0)*step/8, y0+(y1-y0)*step/8))
            time.sleep(.025)
    finally:
        post(Quartz.kCGEventLeftMouseUp, (x1,y1))
    time.sleep(.2)
    eligible = osa('''tell application "System Events" to tell process "Prism" to get enabled of menu item "Group" of menu 1 of menu bar item "Arrange" of menu bar 1''')
    if eligible != 'true':
        raise PrismError('UI_SELECTION_NOT_APPLIED', 'The native rectangle did not select a groupable graph pair.')
    result.update(native_group_action_enabled=True, excluded_previous_panel_count=len(excluded_frames))
    write_json(evidence/'pair-selection.json', result)
    return result


def annotate(path: Path, text: str, x_inches: float, y_inches: float, evidence: Path, *,
             font_size=6, color='#444444', bold=False, italic=False, horizontal_anchor='left'):
    """Insert and position an editable native text object with explicit style."""
    from .native_style import annotate_text
    return annotate_text(path, text, x_inches, y_inches, evidence, font_size=font_size,
                         color=color, bold=bold, italic=italic, horizontal_anchor=horizontal_anchor)


def resize(path: Path, kind: str, width_inches: float, scale_text: bool = True):
    if kind not in ('graph', 'layout'):
        raise PrismError('INVALID_ARGUMENT', 'Resize supports graph or layout.')
    dialog = 'Resize Graph' if kind == 'graph' else 'Resize Layout(s)'
    radio = 'Make the graph width:' if kind == 'graph' else 'Set the layout placeholder width to'
    field = 3 if kind == 'graph' else 2
    with foreground(path):
        menu('Change', 'More Choices...', submenu='Resize Graph' if kind == 'graph' else 'Resize Layout')
        _dialog(dialog)
        units = osa(f'tell application "System Events" to tell process "Prism" to get value of every static text of window {apple_quote(dialog)}')
        if 'in.' not in units:
            raise PrismError('UNSUPPORTED_UI_UNITS', 'Native resize dialog must display inches.', units=units)
        osa(f'''tell application "System Events" to tell process "Prism"
tell window {apple_quote(dialog)}
click radio button {apple_quote(radio)}
set value of text field {field} to "{width_inches:.8g}"
if value of checkbox "Change point size of text proportionally" is not {int(scale_text)} then click checkbox "Change point size of text proportionally"
click button "OK"
end tell
end tell''')
        return {'kind': kind, 'width_inches': width_inches, 'scale_text': scale_text, 'adapter': 'native_resize_dialog'}


if __name__ == '__main__':
    # Isolated, bounded OCR worker. Native NSDictionary avoids a Python mapping
    # bridge that raises NSInvalidArgumentException on absent framework keys.
    import Vision
    import Foundation
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
        Foundation.NSURL.fileURLWithPath_(sys.argv[1]), Foundation.NSDictionary.dictionary())
    import Quartz
    regions = [(0, 0, 1, 1)] if '--tiles' not in sys.argv[2:] else [(x, y, .6, .6) for y in (0, .4) for x in (0, .4)]
    rows = []
    for x, y, width, height in regions:
        request = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(None)
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRegionOfInterest_(Quartz.CGRectMake(x, y, width, height))
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError(str(error))
        for result in request.results():
            box = result.boundingBox()
            row = {'text': result.topCandidates_(1)[0].string(), 'bounds': [x+box.origin.x*width,
                    y+box.origin.y*height, box.size.width*width, box.size.height*height]}
            # Overlap regions may recognize the same native text twice. Keep
            # one only when its exact text and overlapping screen box agree.
            a,b,w,h = row['bounds']
            duplicate = False
            for prior in rows:
                c,d,pw,ph = prior['bounds']
                intersection = max(0,min(a+w,c+pw)-max(a,c))*max(0,min(b+h,d+ph)-max(b,d))
                if row['text'] == prior['text'] and intersection > .5*min(w*h,pw*ph):
                    duplicate = True
                    break
            if not duplicate:
                rows.append(row)
    print(json.dumps(rows))
