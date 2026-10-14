"""Prism 11.1 native graph appearance and live layout placement.

All scientific marks remain Prism's own data-driven graph objects. Screen
coordinates below select native controls only; none describe plotted data.
The backend owns the checkpoint and holds the global mutation lock.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import time
import uuid

from .errors import PrismError
from .runtime import osa, apple_quote
from . import native_ui as ui


def _finite(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise PrismError('INVALID_ARGUMENT', f'{name} is outside the native adapter range.')


def _press_dialog_point(title, expected_size, offset):
    """Fallback for identically named AX font buttons in this native dialog."""
    import Quartz
    state = json.loads(osa('''var p=Application('System Events').processes.byName('Prism');
p.frontmost=true;delay(.2);var w=p.windows.byName(''' + json.dumps(title) + ''');
JSON.stringify({position:w.position(),size:w.size(),pid:p.unixId()});''', jxa=True))
    if state['size'] != list(expected_size) or len(state['position']) != 2 or any(not math.isfinite(x) for x in state['position']):
        raise PrismError('UNSUPPORTED_UI_LAYOUT', 'Native formatting dialog geometry differs from the validated build.', dialog=title, state=state)
    rows = [w for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, 0)
            if w.get('kCGWindowOwnerPID') == state['pid'] and w.get('kCGWindowName') == title]
    if len(rows) != 1:
        raise PrismError('UI_TARGET_MISMATCH', 'Expected one visible owned formatting dialog.')
    bounds = dict(rows[0]['kCGWindowBounds'])
    expected = {'X': state['position'][0], 'Y': state['position'][1], 'Width': expected_size[0], 'Height': expected_size[1]}
    if bounds != expected or not ui._active():
        raise PrismError('UI_TARGET_MISMATCH', 'Formatting dialog ownership or position changed.')
    point = (bounds['X'] + offset[0], bounds['Y'] + offset[1])
    for event_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, event_type, point, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventSetFlags(event, 0)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 1)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(.06)
    time.sleep(.2)


_FONT_BUTTONS = {
    'title': (114, 'Change Graph Title Font'),
    'y_title': (276, 'Change Left Y Axis Title Font'),
    'x_labels': (462, 'Change X Axis Numbering Font'),
    'y_labels': (492, 'Change Left Y Axis Numbering Font'),
}


def _axes_font(target: str, size: int):
    if target not in _FONT_BUTTONS or type(size) is not int or not 4 <= size <= 72:
        raise PrismError('INVALID_ARGUMENT', 'Native classic font sizes must be whole points from 4 to 72.')
    offset, title = _FONT_BUTTONS[target]
    tab = osa('''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
return value of radio button "Titles & Fonts" of tab group 1 of window "Format Axes"
end tell''')
    if tab != '1':
        raise PrismError('UNEXPECTED_DIALOG', 'Titles & Fonts must be selected before a font action.')
    _press_dialog_point('Format Axes', (821, 665), (348, offset))
    # Floating NSFontPanel windows hide when their app deactivates. Activate
    # and verify the exact panel title in the same native event transaction.
    value = osa(f'''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.2
if not (exists window {apple_quote(title)}) then error "Unexpected native font panel"
tell window {apple_quote(title)}
if count of text fields is not 2 then error "Unknown font size controls"
set familyRows to every row of table 1 of scroll area 2 whose selected is true
if count of familyRows is not 1 then error "Expected one native font family"
if value of text field 1 of item 1 of familyRows is not "Arial" then error "Current font family is not verified Arial"
if value of text field 1 of row 1 of table 1 of scroll area 1 is not "Regular" then error "Unrecognized native typeface controls"
select row 1 of table 1 of scroll area 1
set value of text field 2 to "{size}"
perform action "AXConfirm" of text field 2
delay 0.2
set actual to value of text field 2
perform action "AXPress" of (first button whose description is "close button")
return actual
end tell
end tell''')
    if value != str(size):
        raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native font size did not match the requested integer point size.', expected=size, actual=value)
    return {'target': target, 'family': 'Arial', 'face': 'Regular', 'points': size}


def _axes_text(style: dict):
    ui.menu('Change', 'Axes Titles...')
    ui._dialog('Format Axes')
    summary = style['kind'] == 'median_iqr'
    show_title = style['show_title'] and not summary
    osa(f'''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
set t to tab group 1 of window "Format Axes"
perform action "AXPress" of radio button "Titles & Fonts" of t
delay 0.15
if value of checkbox "Show Graph Title" of t is not {int(show_title)} then perform action "AXPress" of checkbox "Show Graph Title" of t
if value of checkbox "Show Left Y axis Title" of t is not {int(not summary)} then perform action "AXPress" of checkbox "Show Left Y axis Title" of t
if value of checkbox "Show X axis Title" of t is 1 then perform action "AXPress" of checkbox "Show X axis Title" of t
end tell''')
    fonts = []
    if not summary:
        if show_title:
            fonts.append(_axes_font('title', style['title_font_size']))
        fonts.append(_axes_font('y_title', style['y_title_font_size']))
        fonts.append(_axes_font('x_labels', style['axis_font_size']))
        fonts.append(_axes_font('y_labels', style['axis_font_size']))
    origin = style.get('y_tick_origin')
    if origin is not None:
        _finite(origin, -1e12, 1e12, 'Tick origin')
        actual = osa(f'''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
set t to tab group 1 of window "Format Axes"
perform action "AXPress" of radio button "Left Y Axis" of t
delay 0.15
if count of text fields of t is not 5 then error "Unrecognized native tick origin controls"
set value of text field 3 of t to "{origin:.12g}"
perform action "AXConfirm" of text field 3 of t
delay 0.15
return value of text field 3 of t
end tell''')
        if float(actual) != origin:
            raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native Y tick origin did not match.', expected=origin, actual=actual)
    # The native seed includes an additional dotted reference line at Y=0.
    # Use Prism's own visibility setting so this publication style shows only
    # its regular ticks, without deleting or rewriting the stored reference.
    extra_ticks = json.loads(osa('''var p=Application('System Events').processes.byName('Prism');
p.frontmost=true;delay(.15);var t=p.windows.byName('Format Axes').tabGroups[0];
t.radioButtons.byName('Left Y Axis').actions.byName('AXPress').perform();delay(.15);
var gs=t.groups().filter(g=>g.radioGroups().flatMap(rg=>rg.radioButtons()).some(r=>r.name()==='Only regular ticks'));
if(gs.length!==1)throw Error('Expected one native additional-tick group');
var rs=gs[0].radioGroups().flatMap(g=>g.radioButtons()).filter(r=>r.name()==='Only regular ticks');
if(rs.length!==1)throw Error('Expected one native additional-tick visibility control');var r=rs[0];
var result;
if(r.enabled()){
 if(r.value()!==1)r.actions.byName('AXPress').perform();delay(.15);
 result={verified:r.value()===1,method:'native_regular_ticks_only'};
}else{
 var ts=gs[0].scrollAreas().flatMap(s=>s.tables());
 if(ts.length!==1)throw Error('Expected one native additional-tick table');
 var rows=ts[0].rows();
 var empty=rows.every(row=>row.textFields().length===2&&row.checkboxes().length===2
  &&row.textFields().every(f=>f.value()==='')&&row.checkboxes().every(c=>c.value()===0));
 result={verified:empty,method:'disabled_control_with_empty_additional_tick_rows',row_count:rows.length};
}
JSON.stringify(result);''', jxa=True))
    if extra_ticks.get('verified') is not True:
        raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native additional Y tick visibility did not match.')
    ui.close_dialog('Format Axes')
    return {'fonts': fonts, 'show_title': show_title, 'y_tick_origin': origin,
            'additional_y_ticks': False, 'additional_tick_verification': extra_ticks}


def _spacing():
    # Native integer percentages put the group centers at 4/13 and 9/13 of
    # the axis length. These are spacing properties, not data geometry.
    result = osa('''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
set t to tab group 1 of window "Format Graph"
perform action "AXPress" of radio button "Graph Settings" of t
delay 0.15
if count of text fields of t is not 4 then error "Unknown native column spacing controls"
set value of text field 1 of t to "35"
perform action "AXConfirm" of text field 1 of t
delay 0.15
set value of text field 3 of t to "58"
perform action "AXConfirm" of text field 3 of t
delay 0.15
set value of text field 4 of t to "58"
perform action "AXConfirm" of text field 4 of t
delay 0.15
return {value of text field 1 of t,value of text field 3 of t,value of text field 4 of t}
end tell''')
    if result != '35, 58, 58':
        raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native column spacing readback differs.', actual=result)
    return {'between_columns_percent': 35, 'before_first_percent': 58, 'after_last_percent': 58}


def _smoothing(value):
    if value not in ('Light', 'Medium', 'High'):
        raise PrismError('INVALID_ARGUMENT', 'Native smoothing must be Light, Medium, or High.')
    actual = osa('''var p=Application('System Events').processes.byName('Prism');p.frontmost=true;delay(.15);
var t=p.windows.byName('Format Graph').tabGroups[0];
function find(e){try{if(e.role()==='AXPopUpButton' && e.description()==='Smoothing')return e}catch(_){};for(var c of e.uiElements()){var f=find(c);if(f)return f}return null};
var c=find(t);if(!c)throw Error('Native smoothing control unavailable');
c.actions.byName('AXPress').perform();delay(.2);
c.menus[0].menuItems.byName(''' + json.dumps(value) + ''').actions.byName('AXPress').perform();delay(.15);c.value();''', jxa=True)
    if actual != value:
        raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native violin smoothing was not retained.', expected=value, actual=actual)
    return value


def format_classic(path: Path, labels: list[str], style: dict):
    if len(labels) != 2 or len(set(labels)) != 2 or style['kind'] not in ('violin', 'median_iqr'):
        raise PrismError('UNSUPPORTED_GRAPH', 'This native appearance adapter requires two distinct source groups.')
    with ui.foreground(path):
        result = {'adapter': 'Prism_11_1_classic_native_UI', 'kind': style['kind']}
        ui.classic_format_menu()
        datasets = []
        for index, (column, label) in enumerate(zip(('A', 'B'), labels)):
            ui.classic_dataset(column, label)
            color = style['colors'][index]
            changes = []
            if style['kind'] == 'violin':
                ui.classic_popup('Appearance', 'Violin plot (truncated)' if style['truncate'] else 'Violin plot')
                # Changing Appearance resets Plot to violin-only in this build.
                ui.classic_popup('Plot', 'Violin plot. Show all points')
                _smoothing(style.get('smoothing', 'High'))
                ui.classic_popup('Bar border thickness', '1/2 pt')
                ui.classic_popup('Symbol size', f"{style['point_size']:g}")
                changes.append(ui.classic_color('Bar fill color', style['fills'][index]))
                changes.append(ui.classic_color('Bar border color', color))
                changes.append(ui.classic_color('Symbol fill color', color, style['point_opacity']))
                changes.append(ui.classic_color('Line color', color, 0))
                changes.append(ui.classic_color('Error bar color', color, 0))
            else:
                ui.classic_popup('Appearance', 'Symbol (one per column)')
                ui.classic_popup('Plot', 'Median with interquartile range')
                ui.classic_bordered_circle()
                ui.classic_popup('Symbol size', f"{style['median_size']:g}")
                ui.classic_popup('Symbol border thickness', '1/2 pt')
                ui.classic_popup('Error bar thickness', '1 pt')
                changes.append(ui.classic_color('Symbol fill color', color))
                changes.append(ui.classic_color('Symbol border color', '#FFFFFF'))
                # Prism can propagate a changed border color to matching wells.
                # Always set the error bar color last for the exact dataset.
                changes.append(ui.classic_color('Error bar color', color))
            datasets.append({'column': column, 'label': label, 'colors': changes})
        result['datasets'] = datasets
        result['spacing'] = _spacing()
        ui.close_dialog('Format Graph')
        # Changing the native graph appearance can reset axis/scale-bar
        # visibility. Apply titles and the final frame after the graph type.
        result['text'] = _axes_text(style)
        result['axes'] = ui.axes_frame(style['width_inches'], style['height_inches'],
                                      hide_axes=style['kind'] == 'median_iqr', color='#444444')
        return result


def place_graph(path: Path, graph_index: int, graph_title: str, x_inches: float,
                y_inches: float, scale_percent: int = 100):
    if type(graph_index) is not int or graph_index < 1 or type(scale_percent) is not int:
        raise PrismError('INVALID_ARGUMENT', 'Graph index and scaling factor must be positive integers.')
    _finite(scale_percent, 1, 1000, 'Scaling factor')
    _finite(x_inches, 0, 30, 'Graph X position')
    _finite(y_inches, 0, 30, 'Graph Y position')
    with ui.foreground(path):
        ui.menu('Insert', 'Insert Prism Graph...')
        ui._dialog('Place Graph on Layout')
        selected = json.loads(osa('''var p=Application('System Events').processes.byName('Prism');
p.frontmost=true;delay(.2);var t=p.windows.byName('Place Graph on Layout').tabGroups[0];
t.radioButtons.byName('Assign Graph').actions.byName('AXPress').perform();delay(.15);
var o=t.scrollAreas[0].outlines[0];var rows=o.rows();
function texts(e){var a=[];try{if(['AXTextField','AXStaticText'].includes(e.role()))a.push(e.value())}catch(_){};for(var c of e.uiElements())a=a.concat(texts(c));return a}
var project=texts(rows[2]).join(' ');
if(!project.includes('(Current project)') || !project.includes(''' + json.dumps(path.name) + '''))throw Error('Current project is not the expected owned checkpoint');
var row=rows[''' + str(graph_index+2) + '''];
if(row.textFields[0].value()!==''' + json.dumps(graph_title) + ''')throw Error('Native graph title/index mismatch');
row.selected=true;delay(.2);if(!row.selected())throw Error('Native graph row selection did not persist');
function find(e,n){try{if(e.role()==='AXRadioButton' && e.name()===n)return e}catch(_){};for(var c of e.uiElements()){var f=find(c,n);if(f)return f}return null};
var live=find(t,'Live link. Update the layout when the graph changes.');
if(!live)throw Error('Native live linkage control unavailable');if(live.value()!==1)live.actions.byName('AXPress').perform();delay(.15);
if(live.value()!==1)throw Error('Native graph live link was not retained');
JSON.stringify({project:project,graph_title:row.textFields[0].value(),live_link:true});''', jxa=True))
        result = osa(f'''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
set t to tab group 1 of window "Place Graph on Layout"
perform action "AXPress" of radio button "Size and Location" of t
delay 0.15
if count of text fields of t is not 3 then error "Unknown graph location controls"
if (value of every static text of t) as text does not contain "in." then error "Native graph placement units must be inches"
set value of text field 1 of t to "{scale_percent}"
perform action "AXConfirm" of text field 1 of t
delay 0.15
set value of text field 2 of t to "{x_inches:.12g}"
perform action "AXConfirm" of text field 2 of t
delay 0.15
set value of text field 3 of t to "{y_inches:.12g}"
perform action "AXConfirm" of text field 3 of t
delay 0.15
return value of every text field of t
end tell''')
        actual = [float(x.strip()) for x in result.split(',')]
        if len(actual) != 3 or any(not math.isfinite(x) for x in actual) or actual[0] != scale_percent or any(abs(a-b) > .00501 for a,b in zip(actual[1:], (x_inches, y_inches))):
            raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native graph placement differs from the requested coordinates.', actual=result)
        ui.close_dialog('Place Graph on Layout')
        return {**selected, 'graph_index': graph_index, 'x_inches': x_inches, 'y_inches': y_inches,
                'scale_percent': scale_percent, 'native_fields': actual, 'display_resolution_inches': .01}


def align_all_graphs(path: Path, axes: list[str]):
    import Quartz
    if not axes or len(set(axes)) != len(axes) or any(x not in ('x', 'y') for x in axes):
        raise PrismError('INVALID_ARGUMENT', 'Choose distinct x and/or y axes to align.')
    with ui.foreground(path) as app:
        ui.focus_canvas(app, path, path.parent/'axis-alignment')
        ui.key(app, 0, Quartz.kCGEventFlagMaskCommand)
        for axis in axes:
            ui.menu('Arrange', f'Align {axis.upper()} Axes')
    return {'alignment': 'native_graph_axis_origins', 'axes': axes}


def compose_overlay_layout(path: Path, panels: list[dict], evidence: Path):
    """Rebuild a native layout from two live graph pairs, then group each pair.

    Each pair is aligned by Prism's native X/Y-axis commands. A unique visible
    base-graph title is used only to select the second pair in the layout UI.
    """
    import Quartz
    if len(panels) != 2:
        raise PrismError('INVALID_ARGUMENT', 'This composition adapter currently supports two graph pairs.')
    if panels[0]['selection_label'] == panels[1]['selection_label']:
        raise PrismError('INVALID_ARGUMENT', 'The two visible panel selection labels must be distinct.')
    with ui.foreground(path) as app:
        ui.focus_canvas(app, path, evidence/'initial-canvas-focus')
        ui.key(app, 0, Quartz.kCGEventFlagMaskCommand)
        ui.key(app, 51)
        receipts, grouped_frames = [], []
        for index, panel in enumerate(panels):
            ui.focus_canvas(app, path, evidence/f'panel-{index+1}-canvas-focus')
            for which in ('violin', 'summary'):
                # A title-free summary has a tighter native bounding box. A
                # small temporary offset keeps its upper error bar clear of
                # the violin's title while selecting the pair. Prism's native
                # axis alignment below establishes the final position.
                initial_y = panel['y_inches'] + (.2 if which == 'summary' else 0)
                receipts.append(place_graph(path, panel[f'{which}_index'], panel[f'{which}_sheet_title'],
                                            panel['x_inches'], initial_y, panel.get('scale_percent', 100)))
            folder = evidence / f'panel-{index+1}-pair-selection'
            folder.mkdir(parents=True, exist_ok=True)
            # Select only the two objects enclosed by this base graph's native
            # UI frame. Select All includes a previous group's child objects.
            ui.select_enclosed_graphs(app, path, panel['selection_label'], folder, excluded_frames=grouped_frames)
            ui.menu('Arrange', 'Align X Axes')
            ui.menu('Arrange', 'Align Y Axes')
            ui.menu('Arrange', 'Group')
            grouped_frames.append(ui.select_enclosed_graphs(app, path, panel['selection_label'],
                                                          evidence/f'panel-{index+1}-group-frame', frame_only=True))
        return {'native_objects': 'two_grouped_pairs_of_live_graphs', 'placements': receipts,
                'alignment': 'Prism_native_axis_origins', 'source': 'original_observation_tables',
                'grouped_UI_frames': grouped_frames}


def _rtf(text: str, size: float, color: str, bold: bool, italic: bool, alignment='left') -> bytes:
    import re
    _finite(size, 4, 72, 'Text point size')
    if (not float(size * 2).is_integer() or not isinstance(color, str)
            or not re.fullmatch(r'#[0-9a-fA-F]{6}', color)):
        raise PrismError('INVALID_ARGUMENT', 'Formatted text uses half-point sizes and #RRGGBB colors.')
    if not isinstance(alignment, str) or alignment not in ('left', 'center', 'right'):
        raise PrismError('INVALID_ARGUMENT', 'Text alignment must be left, center, or right.')
    escaped = []
    # RTF Unicode escapes are signed UTF-16 code units, including surrogate
    # pairs. No user text is interpreted as an RTF instruction.
    raw = text.encode('utf-16-le')
    for offset in range(0, len(raw), 2):
        code = int.from_bytes(raw[offset:offset+2], 'little')
        if code in (92, 123, 125):
            escaped.append('\\' + chr(code))
        elif 32 <= code < 127:
            escaped.append(chr(code))
        else:
            escaped.append('\\u' + str(code if code < 32768 else code-65536) + '?')
    red, green, blue = (int(color[i:i+2], 16) for i in (1, 3, 5))
    paragraph = {'left': 'ql', 'center': 'qc', 'right': 'qr'}[alignment]
    header = ('{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}'
              f'{{\\colortbl;\\red{red}\\green{green}\\blue{blue};}}'
              f'\\viewkind4\\uc1\\pard\\{paragraph}\\f0\\fs{int(size*2)}\\cf1\\b{int(bold)}\\i{int(italic)} ')
    return (header + ''.join(escaped) + '}').encode('ascii')


def annotate_text(path: Path, text: str, x_inches: float, y_inches: float, evidence: Path, *,
                  font_size=6, color='#444444', bold=False, italic=False, horizontal_anchor='left'):
    """Insert native RTF text using a temporary readable selection label.

    The temporary label is replaced in the selected native text object, not
    retained as a second object. Positioning uses the native text dimensions.
    """
    import AppKit
    _finite(x_inches, 0, 30, 'Text X position')
    _finite(y_inches, 0, 30, 'Text Y position')
    if horizontal_anchor not in ('left', 'center', 'right') or type(bold) is not bool or type(italic) is not bool:
        raise PrismError('INVALID_ARGUMENT', 'Unrecognized native text style or horizontal anchor.')
    final_rtf = _rtf(text, font_size, color, bold, italic, horizontal_anchor)
    token = 'Prism label ' + uuid.uuid4().hex[:12]
    token_rtf = _rtf(token, 20, '#000000', True, False)
    evidence.mkdir(parents=True, exist_ok=True)
    with ui.foreground(path) as app:
        pb = AppKit.NSPasteboard.generalPasteboard()
        backup = [{str(t): bytes(item.dataForType_(t)) for t in item.types()
                   if item.dataForType_(t) is not None} for item in pb.pasteboardItems() or []]
        own_count = None

        def put(value, rtf):
            nonlocal own_count
            if own_count is not None and pb.changeCount() != own_count:
                raise PrismError('CLIPBOARD_CHANGED', 'Clipboard changed during native text insertion; no replacement was made.')
            pb.clearContents()
            pb.setString_forType_(value, AppKit.NSPasteboardTypeString)
            pb.setData_forType_(rtf, AppKit.NSPasteboardTypeRTF)
            own_count = pb.changeCount()

        try:
            put(token, token_rtf)
            insertion = ui.contextual_paste(app, path, token, evidence)
            selection = ui.select_text(app, path, token, evidence)
            put(text, final_rtf)
            # Native code inspection established that Paste replaces an
            # exclusively selected text object and preserves its selection.
            ui.menu('Edit', 'Paste')
            time.sleep(.2)
            ui.menu('Arrange', 'Position Object...')
            ui._dialog('Position Text')
            dimensions = json.loads(osa('''var p=Application('System Events').processes.byName('Prism');
p.frontmost=true;delay(.2);var w=p.windows.byName('Position Text');
var units=w.staticTexts().map(x=>x.value()).join(' ');if(!units.includes('in.'))throw Error('Native text position units must be inches');
var fields=w.groups[0].textFields();if(fields.length!==5)throw Error('Unrecognized native text geometry dialog');
JSON.stringify(fields.map(x=>x.value()));''', jxa=True))
            # Prism's native classic text import can round half-point font
            # sizes at render time. Add a small real text-box margin so its
            # own word wrapping does not split a short single-line label.
            width = float(dimensions[2]) + .08
            if not math.isfinite(width) or width < 0 or width > 30:
                raise PrismError('UNSUPPORTED_UI_LAYOUT', 'Native text width is invalid.', width=width)
            left = x_inches - width * {'left': 0, 'center': .5, 'right': 1}[horizontal_anchor]
            _finite(left, 0, 30, 'Text left edge')
            value = osa(f'''tell application "System Events" to tell process "Prism"
set frontmost to true
delay 0.15
tell group 1 of window "Position Text"
set value of text field 3 to "{width:.12g}"
perform action "AXConfirm" of text field 3
delay 0.15
set value of text field 1 to "{left:.12g}"
perform action "AXConfirm" of text field 1
delay 0.15
set value of text field 2 to "{y_inches:.12g}"
perform action "AXConfirm" of text field 2
delay 0.15
return {{value of text field 1,value of text field 2}}
end tell
end tell''')
            actual = [float(x.strip()) for x in value.split(',')]
            if len(actual) != 2 or any(not math.isfinite(x) for x in actual) or any(abs(a-b) > .00501 for a,b in zip(actual, (left, y_inches))):
                raise PrismError('UI_FORMAT_NOT_APPLIED', 'Native text position differs from the requested position.', actual=value)
            ui.close_dialog('Position Text')
            return {'text': text, 'native_object': 'editable_text', 'x_inches': x_inches, 'y_inches': y_inches,
                    'horizontal_anchor': horizontal_anchor, 'native_position_fields': actual,
                    'font_size': font_size, 'color': color, 'bold': bold, 'italic': italic,
                    'native_text_box_width_inches': width,
                    'native_format': 'RTF_import_into_selected_native_text',
                    'selection_token': token, 'selection': selection, 'insertion': insertion}
        finally:
            if own_count is not None and pb.changeCount() == own_count:
                pb.clearContents()
                items = []
                for entry in backup:
                    item = AppKit.NSPasteboardItem.alloc().init()
                    for kind, data in entry.items():
                        item.setData_forType_(data, kind)
                    items.append(item)
                if items:
                    pb.writeObjects_(items)


def create_summary(path: Path):
    """Create a native median/IQR graph from the table selected by the backend."""
    with ui.foreground(path):
        ui.menu('Insert', 'New Graph of Existing Data...')
        ui._dialog('Create New Graph')
        result = json.loads(osa('''var p=Application('System Events').processes.byName('Prism');
p.frontmost=true;delay(.2);var w=p.windows.byName('Create New Graph');
function all(e,role){var a=[];if(e.role()===role)a.push(e);for(var c of e.uiElements())a=a.concat(all(c,role));return a}
var kinds=w.popUpButtons();if(kinds.length!==2 || kinds[1].value()!=='Column')throw Error('Native summary creation requires a Column observation table');
var table=kinds[0].value();
for(var name of ['Plot selected data sets only',"Create a new graph for each data set (don't put them all on one graph)"]){
var c=w.checkboxes.byName(name);if(c.value()!==0)c.actions.byName('AXPress').perform();delay(.1);if(c.value()!==0)throw Error('All original column datasets must be included in one native graph')}
var tabs=all(w,'AXRadioButton').filter(x=>x.name()==='Mean/median & error');
if(tabs.length!==1)throw Error('Native summary graph tab is not unique');tabs[0].actions.byName('AXPress').perform();delay(.2);
var areas=all(w,'AXRadioGroup').filter(x=>x.radioButtons().length===6);
if(areas.length!==1)throw Error('Native summary graph choices changed');
areas[0].radioButtons[1].actions.byName('AXPress').perform();delay(.2);
var texts=all(w,'AXStaticText').map(x=>x.value());
if(!texts.includes('Column mean, error bars'))throw Error('Unexpected native summary graph choice');
var popups=all(w,'AXPopUpButton').filter(x=>!['Column',table].includes(x.value()));
if(popups.length!==1)throw Error('Native summary statistic selector is ambiguous');
var c=popups[0];c.actions.byName('AXPress').perform();delay(.2);
c.menus[0].menuItems.byName('Median with interquartile range').actions.byName('AXPress').perform();delay(.2);
if(c.value()!=='Median with interquartile range')throw Error('Native median/IQR statistic was not retained');
JSON.stringify({source_table_title:table,graph_family:'Column',summary:'Median with interquartile range',native_data_binding:true});''', jxa=True))
        ui.close_dialog('Create New Graph', 'Create')
        return result
