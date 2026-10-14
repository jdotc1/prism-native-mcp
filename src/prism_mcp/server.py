"""Official MCP SDK v2 stdio transport. Scientific work lives in the backend."""
from __future__ import annotations

import json
from typing import Annotated, Literal
from mcp.server import MCPServer
from mcp import types
from pydantic import Field

from .analysis_settings import DescriptiveSettings
from .backend import Backend, project, lookup
from .errors import PrismError
from .models import AnnotationInput, GraphFormat, LayoutInput, NativeGraphStyle, NativeOverlayPanel, TableInput

mcp = MCPServer('Prism Native', version='0.1.0', instructions=(
    'Control local Prism 11.1 using native observation tables, linked violin graphs, '
    'descriptive analyses and layouts. Call prism_capabilities first. Use absolute paths '
    'and UUIDs from prism_inspect. Never substitute plotted coordinates for observations. '
    'Mutations are serialized and refuse unsaved changes. Creation and export require '
    'fresh destinations. Preserve run IDs; completion requires native log and fresh token. '
    'New families currently use a verified native two-group seed. No network or LLM backend.'))
backend = Backend()
READ = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
WRITE = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
REPLACE = types.ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def invoke(fn, *args, **kwargs) -> types.CallToolResult:
    try:
        payload = fn(*args, **kwargs)
        error = False
    except PrismError as exc:
        payload = {'ok': False, 'error': exc.as_dict()}
        error = True
    return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(payload, ensure_ascii=False, allow_nan=False))],
                                structured_content=payload, is_error=error)


@mcp.tool(annotations=READ)
def prism_capabilities() -> types.CallToolResult:
    """Report installed, configured, connected and live-verified states and actual native limits."""
    def result():
        value = backend.capabilities()
        value['connected'] = {'transport': 'stdio', 'tool_invocation_verified': True}
        return value
    return invoke(result)


@mcp.tool(annotations=READ)
def prism_discover(directory: str | None = None) -> types.CallToolResult:
    """List Prism windows and inspect .prism documents directly inside an optional local directory."""
    return invoke(backend.discover, directory)


@mcp.tool(annotations=READ)
def prism_inspect(path: str, include_values: bool = False) -> types.CallToolResult:
    """Inspect saved native UUIDs, source tables, analysis settings/results and graph/layout relationships."""
    return invoke(project, path, include_values=include_values)


@mcp.tool(annotations=READ)
def prism_read_table(path: str, table_id: str) -> types.CallToolResult:
    """Return a specific original-observation table by stable native UUID, including saved precision."""
    return invoke(lambda: lookup(project(path), 'tables', table_id))


@mcp.tool(annotations=READ)
def prism_read_analysis(path: str, analysis_id: str) -> types.CallToolResult:
    """Retrieve native analysis parameters, linked inputs and full-precision Prism results by UUID."""
    return invoke(lambda: lookup(project(path), 'analyses', analysis_id))


@mcp.tool(annotations=WRITE)
def prism_configure_analysis(path: str, analysis_id: str, settings: DescriptiveSettings,
                             expected_sha256: str | None = None, request_id: str | None = None) -> types.CallToolResult:
    """Configure supported native descriptive settings on an owned closed checkpoint, then recalculate/save in Prism and verify parameters, observations, and summaries. Native acceptance covers confidence 90/95 and saved result text at 6/10 significant digits; routine calls do not independently check exact CI endpoints or on-screen formatting."""
    return invoke(backend.configure_analysis, path, analysis_id, settings, expected_sha256, request_id)


@mcp.tool(annotations=WRITE)
def prism_create_document(path: str, tables: list[TableInput], layout: bool = True,
                          request_id: str | None = None) -> types.CallToolResult:
    """Create a modern .prism with native two-group observation/violin/summary families and optional grid layout."""
    return invoke(backend.create_document, path, tables, layout, request_id)


@mcp.tool(annotations=WRITE)
def prism_update_table(path: str, table_id: str, table: TableInput,
                       expected_sha256: str | None = None, request_id: str | None = None) -> types.CallToolResult:
    """Replace a UUID-targeted native table from original observations; recalculate and verify linked results/layouts."""
    return invoke(backend.update_table, path, table_id, table, expected_sha256, request_id)


@mcp.tool(annotations=WRITE)
def prism_format_graph(path: str, graph_id: str, formatting: GraphFormat,
                       request_id: str | None = None) -> types.CallToolResult:
    """Set native titles, Y-axis range/ticks and named color scheme; refresh linked layouts without changing observations."""
    return invoke(backend.format_graph, path, graph_id, formatting, request_id)


@mcp.tool(annotations=WRITE)
def prism_export(path: str, output_directory: str, formats: list[Literal['pdf', 'svg', 'png']] = ['pdf', 'svg'],
                  request_id: str | None = None) -> types.CallToolResult:
    """Export every native graph/layout into a fresh directory and verify current-run completion and output signatures."""
    return invoke(backend.export, path, output_directory, formats, request_id)


@mcp.tool(annotations=WRITE)
def prism_open_document(path: str, request_id: str | None = None) -> types.CallToolResult:
    """Open a native project and verify its exact document URL in Prism's window state."""
    return invoke(backend.open_document, path, request_id)


@mcp.tool(annotations=WRITE)
def prism_save_document(path: str, save_as: str | None = None, request_id: str | None = None) -> types.CallToolResult:
    """Explicitly save the target's current native state, including unsaved edits; retain a prior-disk checkpoint. Save As requires a new path."""
    return invoke(backend.save_document, path, save_as, request_id)


@mcp.tool(annotations=WRITE)
def prism_close_document(path: str, request_id: str | None = None) -> types.CallToolResult:
    """Close only the exact named project; refuse to discard unsaved changes."""
    return invoke(backend.close_document, path, request_id)


@mcp.tool(annotations=READ)
def prism_operation_status(run_id: str) -> types.CallToolResult:
    """Read durable operation status, native script logs, fingerprints and useful failure checkpoints."""
    return invoke(backend.runtime.status, run_id)


@mcp.tool(annotations=WRITE)
def prism_request_cancellation(run_id: str) -> types.CallToolResult:
    """Request cooperative cancellation before the next dispatch/publish boundary; does not kill Prism."""
    def cancel():
        status = backend.runtime.status(run_id)
        if status['status'] in ('succeeded', 'failed'):
            return {'run_id': run_id, 'cancelled': False, 'status': status['status']}
        (backend.runtime.state / 'runs' / run_id / 'cancel.request').touch()
        return {'run_id': run_id, 'cancel_requested': True, 'native_interruption_guaranteed': False}
    return invoke(cancel)


@mcp.tool(annotations=WRITE)
def prism_recover() -> types.CallToolResult:
    """Clear an uncertain native-execution quarantine only when matching terminal evidence proves the script stopped. Never retries or publishes automatically."""
    return invoke(backend.runtime.recover)


@mcp.tool(annotations=WRITE)
def prism_add_table(path: str, table: TableInput, request_id: str | None = None) -> types.CallToolResult:
    """Create a native two-group observation table, a bound violin graph, and descriptive analysis in an existing compatible project."""
    return invoke(backend.add_table, path, table, request_id)


@mcp.tool(annotations=WRITE)
def prism_create_layout(path: str, graph_ids: list[str], layout: LayoutInput,
                        request_id: str | None = None) -> types.CallToolResult:
    """Create a native aligned grid of consecutive graph UUIDs; require one genuine bound graph per cell."""
    return invoke(backend.create_layout, path, graph_ids, layout, request_id)


@mcp.tool(annotations=WRITE)
def prism_recalculate(path: str, request_id: str | None = None) -> types.CallToolResult:
    """Run native RecalcAll, refresh every linked layout, and verify descriptive counts/means/medians against source observations."""
    return invoke(backend.recalculate, path, request_id)


@mcp.tool(annotations=WRITE)
def prism_export_table(path: str, table_id: str, output_path: str,
                       request_id: str | None = None) -> types.CallToolResult:
    """Export exact CSV bytes of a UUID-targeted saved native observation table to a new file, preserving saved precision."""
    return invoke(backend.export_table, path, table_id, output_path, request_id)


@mcp.tool(annotations=WRITE)
def prism_format_native_graph(path: str, graph_id: str, style: NativeGraphStyle,
                              expected_sha256: str | None = None, request_id: str | None = None) -> types.CallToolResult:
    """Format a native classic violin or median/IQR graph on an owned checkpoint. Preserve observations and graph/layout bindings. Verified on the pinned Prism build; review each new figure's appearance."""
    return invoke(backend.format_native_graph, path, graph_id, style, expected_sha256, request_id)


@mcp.tool(annotations=WRITE)
def prism_create_graph(path: str, table_id: str, title: str, kind: Literal['median_iqr'] = 'median_iqr',
                       formatting: GraphFormat | None = None, style: NativeGraphStyle | None = None,
                       expected_sha256: str | None = None, request_id: str | None = None) -> types.CallToolResult:
    """Create a native median/IQR graph from an existing two-group Column observation table. Require exactly one new graph with the original dataset UUIDs; optional style stays native. Creation and measurement-driven updates are verified on the pinned Prism build."""
    return invoke(backend.create_graph, path, table_id, title, kind, formatting, style, expected_sha256, request_id)


@mcp.tool(annotations=WRITE)
def prism_place_graph(path: str, layout_id: str, graph_id: str, x_inches: float, y_inches: float,
                      scale_percent: int = 100, expected_sha256: str | None = None,
                      request_id: str | None = None) -> types.CallToolResult:
    """Experimental placement of a linked native graph into an existing layout. Refuse duplicate graph links and preserve every prior link, media object count, and observation table."""
    return invoke(backend.place_graph, path, layout_id, graph_id, x_inches, y_inches, scale_percent, expected_sha256, request_id)


@mcp.tool(annotations=WRITE)
def prism_align_layout(path: str, layout_id: str, axes: list[Literal['x', 'y']] | None = None,
                       expected_sha256: str | None = None, request_id: str | None = None) -> types.CallToolResult:
    """Experimental native X/Y axis alignment for a layout with exactly two distinct graph links and no media. Native text can exist without media, so align before adding annotations and require the native adapter to verify graph-only selection. Preserve observations and links; positions remain private native state."""
    return invoke(backend.align_layout, path, layout_id, axes, expected_sha256, request_id)


@mcp.tool(annotations=REPLACE)
def prism_compose_overlay_layout(path: str, layout_id: str,
                                 panels: Annotated[list[NativeOverlayPanel], Field(min_length=2, max_length=2)],
                                 expected_sha256: str | None = None, request_id: str | None = None) -> types.CallToolResult:
    """Replace all selected layout contents, including annotations, with two grouped native violin/summary graph pairs. Require four distinct classic graphs and identical original dataset UUIDs within each pair. Visible headers locate native UI selections only. Native acceptance includes original-measurement updates and restoration; review each new figure's appearance."""
    return invoke(backend.compose_overlay_layout, path, layout_id, panels, expected_sha256, request_id)


@mcp.tool(annotations=WRITE)
def prism_resize_object(path: str, object_id: str, kind: Literal['graph', 'layout'], width_inches: float,
                        scale_text: bool = True, request_id: str | None = None) -> types.CallToolResult:
    """Set a native graph or layout width through Prism's Resize dialog. Requires Accessibility and foreground UI; preserves source observations."""
    return invoke(backend.resize_object, path, object_id, kind, width_inches, scale_text, request_id)


@mcp.tool(annotations=WRITE)
def prism_annotate_layout(path: str, layout_id: str, annotation: AnnotationInput,
                          request_id: str | None = None) -> types.CallToolResult:
    """Add editable native static text at page coordinates. Local screenshot/OCR locates the new label for native positioning; requires screen-recording access and an unobstructed label."""
    return invoke(backend.annotate_layout, path, layout_id, annotation, request_id)


def main():
    mcp.run(transport='stdio')


if __name__ == '__main__':
    main()
