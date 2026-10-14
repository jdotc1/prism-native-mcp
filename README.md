# Prism Native MCP

A local MCP server for GraphPad Prism on macOS. It connects an MCP client to
native Prism tables, descriptive analyses, graphs, and layouts. Prism calculates
the statistics and draws the graphs from the supplied observations.

This is an independent, version-pinned source preview. It requires a separately
installed, licensed copy of GraphPad Prism **11.1.0, build 110218**. It is not
affiliated with or endorsed by GraphPad, and compatibility with later Prism
versions is not established.

## What it does

- Inspects supported `.prism` archives, original observations, analysis results,
  and graph/layout relationships using native object UUIDs.
- Creates template-based two-group Column documents with native violin graphs
  and descriptive statistics; adds tables and updates observations.
- Creates native median/IQR companion graphs, formats supported graph settings,
  composes linked layouts, and adds editable static text.
- Exports through Prism to PDF, SVG, and PNG; exports saved table CSV separately.
- Records operation status, serializes mutations, and quarantines uncertain
  native execution until matching recovery evidence is available.

New observation families require two groups with at least three numeric values
each. The bundled seed contains synthetic data. Arbitrary analyses, general
graph creation, and unsupported storage/exclusion forms are outside the writer's
scope. Classic drawing binaries remain opaque; the server does not reconstruct
graphs from plotted coordinates. See [capabilities](docs/CAPABILITIES.md).

## Requirements

- macOS; historical native acceptance was performed on Apple silicon.
- GraphPad Prism 11.1.0, build 110218, installed separately. The default location
  is `/Applications/Prism 11.app`; `PRISM_MCP_APP` can select another location
  of that same version.
- Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/).
- macOS Automation and Accessibility permissions for native control. Text/OCR
  adapters also require Screen Recording. Read the permission errors before
  attempting a native operation.

The server uses stdio and has no hosted service or LLM dependency. Data returned
through MCP is visible to the connected client; the client's own processing and
retention settings still apply.

## Install and connect

From your checkout:

```sh
uv sync --locked --python 3.12
```

Configure your MCP client to launch the installed entry point:

```json
{
  "mcpServers": {
    "prism-native": {
      "command": "/absolute/path/to/prism-native-mcp/.venv/bin/prism-mcp"
    }
  }
}
```

Replace the example path with your checkout's actual absolute path. Clients
may use a different configuration shape. An optional checkout-relative launcher
is available at `bin/prism-mcp`; it resolves the checkout from its own location.

Call `prism_capabilities` first. Installed application, available control,
connected transport, and recorded native verification are separate states.
The capability check reads local application/window state; it is not a new
native editing acceptance run. A diagnostic CLI is also available:

```sh
.venv/bin/prism-native --help
```

## Example input

Supply original observations, rather than a rendered figure or precomputed
density. This synthetic two-group input can be passed in the `tables` list to
`prism_create_document`, together with a new absolute destination path:

```json
{
  "name": "Synthetic example",
  "units": "arbitrary units",
  "columns": [
    {"label": "Group A", "values": [1.5, 2.0, 3.25, 4.0]},
    {"label": "Group B", "values": [2.0, 3.5, 4.25, 5.0]}
  ]
}
```

Use `prism_inspect` to obtain object UUIDs before targeted edits. Ordinary
mutations refuse dirty target documents; `prism_save_document` explicitly saves
the named document's current edits. Some operations replace selected layout
contents. Review each tool's scope and use working copies.

Preserve operation IDs. A timeout can leave native work running; inspect status
and recovery evidence before retrying. Do not delete quarantine files to force
another operation. See [native control and recovery](docs/OPERATIONS.md).

## Evidence and limits

The private development baseline has dated native acceptance records for
template-based creation, data-driven updates, selected graph/layout controls,
export, and selected failure/recovery cases. Those records are summarized in
[verification](docs/VERIFICATION.md); private study inputs and detailed records
are not distributed. They do not qualify every exposed operation or a new Prism
version.

The public seed replaces two local author-metadata fields with the maintainer's
GitHub handle. Its other members are preserved. Native runtime acceptance has
not been repeated for this public export. Source checks and packaging checks
must not be presented as native Prism acceptance.

Static annotation text does not recalculate when observations change. Exact
appearance matching, arbitrary inferential analyses, and newer multiple-variable
graph creation are not claimed.

## Development

Synthetic/offline tests are included. Tests mock native dispatch where needed;
they do not establish live application behavior.

```sh
uv sync --locked --group dev
.venv/bin/python -m pytest tests
```

Two synthetic native acceptance runners are included in `experiments/` for
analysis-parameter comparisons and failure/recovery work. They are manual tools,
not automatic installation or CI steps. Run them only against an explicitly
chosen disposable native session; read their options and source first.

## Contributing and reporting issues

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
Use synthetic examples in public reports. Do not attach confidential datasets,
credentials, or unredacted runtime records.

The project license is MIT; see [LICENSE](LICENSE).
GraphPad Prism and third-party dependencies retain their own terms; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
