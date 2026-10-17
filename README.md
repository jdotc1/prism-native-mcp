# Prism Native MCP

**Connect an MCP client to native GraphPad Prism tables, statistics, graphs, and layouts.**

Prism Native MCP is a local stdio server for GraphPad Prism on macOS. Supply
original observations, build supported Column documents and violin/median-IQR
graphs, inspect native objects, and export figures through Prism. **Prism
calculates the statistics and draws the graphs from the observations.**

Use it for a repeatable two-group descriptive workflow that remains editable in
Prism: data table → native analysis and graphs → linked layout → PDF, SVG, or PNG.

**[Install and connect](#install-and-connect)** ·
**[Try a synthetic document](#create-a-synthetic-document)** ·
**[Supported scope](docs/CAPABILITIES.md)** ·
**[Release packages](https://github.com/jdotc1/prism-native-mcp/releases)**

> **Independent source preview.** This project requires a separately installed,
> licensed copy of **GraphPad Prism 11.1.0, build 110218**. It is not affiliated
> with or endorsed by GraphPad. Native mutations are pinned to that exact build;
> compatibility with other versions is not established. The wheel and source
> archive are Python packages, not standalone macOS applications.

## What you can do

| Workflow | Supported scope |
| --- | --- |
| Inspect a saved document | Read supported `.prism` tables, original observations, result objects, and graph/layout bindings using native UUIDs. Supported multiple-variable objects can be inspected within the documented limits. |
| Create or update observations | Create from the bundled two-group Column seed, add tables, and update observations while Prism recalculates native dependents. Each new group needs at least three numeric observations. |
| Build descriptive figures | Use native violin graphs, median/IQR companion graphs, and selected graph/axis styling controls. |
| Compose a layout | Arrange linked graphs in grids or paired overlays and add editable static text. Overlay composition replaces the selected layout's contents, including annotations. |
| Export | Ask Prism for PDF/SVG/PNG output, or separately export exact saved-table CSV. |
| Track native work | Serialize mutations, record run status, and quarantine uncertain execution until matching recovery evidence is available. |

The writer does not support arbitrary analyses, general graph creation,
unsupported storage/cell exclusions, or arbitrary binary drawing edits. Classic
drawing binaries remain opaque; the server does not reconstruct observations
from plotted coordinates. Static annotation text does not recalculate when data
changes. Read the [capability matrix](docs/CAPABILITIES.md) for narrower evidence
around placement/alignment, experimental resizing, and individual formatting
variants.

## Requirements

- **macOS.** Historical native acceptance was performed on Apple silicon.
- **GraphPad Prism 11.1.0, build 110218**, installed and licensed separately.
  The default location is `/Applications/Prism 11.app`; `PRISM_MCP_APP` can select
  another location of the same version. The targeted archive format is `1-6-1`.
- **Python 3.12 or 3.13** and [uv](https://docs.astral.sh/uv/).
- **macOS Automation and Accessibility permissions** for native control.
  Text/OCR adapters also require Screen Recording. Resolve the reported
  permission errors before attempting a native operation.
- An MCP client that can launch a local stdio server.

The server has no hosted service or LLM dependency. Data returned through MCP is
visible to the connected client, whose own processing and retention settings
still apply.

## Install and connect

```sh
git clone https://github.com/jdotc1/prism-native-mcp.git
cd prism-native-mcp
uv sync --locked --python 3.12
```

Configure your MCP client to launch the installed entry point. For clients using
an `mcpServers` configuration, the entry is:

```json
{
  "mcpServers": {
    "prism-native": {
      "command": "/absolute/path/to/prism-native-mcp/.venv/bin/prism-mcp"
    }
  }
}
```

Replace the example path with your checkout's actual absolute path. Client
configuration formats vary. [bin/prism-mcp](bin/prism-mcp) is an optional
checkout-relative launcher that resolves the checkout from its own location.

Call **`prism_capabilities` first**. Installed application, available control,
connected transport, and recorded native verification are separate states. The
check reads local application/window state; it does not run a new editing
acceptance test.

You can inspect capabilities through the diagnostic CLI too:

```sh
.venv/bin/prism-native capabilities
.venv/bin/prism-native --help
```

## Create a synthetic document

After the capability check, call **`prism_create_document`** with the following
tool arguments. Replace `path` with a **new absolute `.prism` destination in an
existing directory**:

```json
{
  "path": "/absolute/path/to/new-synthetic-example.prism",
  "tables": [
    {
      "name": "Synthetic example",
      "units": "arbitrary units",
      "columns": [
        {"label": "Group A", "values": [1.5, 2.0, 3.25, 4.0]},
        {"label": "Group B", "values": [2.0, 3.5, 4.25, 5.0]}
      ]
    }
  ],
  "layout": true
}
```

This supplies observations to the bundled synthetic two-group Column workflow.
Use original observations for your own work; a rendered figure, precomputed
density, or static annotation is not a substitute for the native analysis input.

Then use these tools as needed:

| Tool | Next step |
| --- | --- |
| `prism_inspect` | Inspect the saved document and obtain native table, analysis, graph, and layout UUIDs. |
| `prism_read_table` / `prism_read_analysis` | Review saved observations or analysis results. |
| `prism_update_table` | Update a supported table by UUID, supplying `expected_sha256` when working from an inspected revision. |
| `prism_export` | Export through Prism to a fresh output directory. Review the resulting appearance, clipping, units, and rounding. |
| `prism_operation_status` | Check the returned run ID if completion is uncertain. |

The [server definitions](src/prism_mcp/server.py) expose the full argument schema.
The [capability guide](docs/CAPABILITIES.md) defines which operations and variants
have historical native evidence.

## Preserve documents and recover safely

Work on owned copies. Ordinary mutations refuse dirty or unknown target-document
state; **`prism_save_document` explicitly saves the named document's current
edits**. Controlled operations can close/reopen clean targets. Prism can autosave,
so Save As does not undo an earlier source modification.

Native mutations share an operation lock. Preserve returned run IDs: a timeout
can leave native work running, and cooperative cancellation does not guarantee
interruption. Inspect operation status and matching native evidence before
retrying. Successful dispatch or a disappearing progress panel does not by
itself establish completion.

Recovery clears the relevant quarantine only when the required terminal evidence
matches; it does not retry a failed mutation or mark it successful. Do not delete
quarantine files or force-terminate Prism to bypass recovery. Missing or ambiguous
displays/controls are refused rather than guessed. See
[native control and recovery](docs/OPERATIONS.md).

Detailed operation records live under the current user's application-support
directory and can include document paths, values, and diagnostic text. Review
records before sharing. The integration controls Prism through Apple Events and
selected native UI adapters; it does not patch the installed application or
expose a generic shell/script execution MCP tool.

## Evidence and limits

The [verification record](docs/VERIFICATION.md) summarizes dated historical native
acceptance for template creation, observation updates, selected graph/layout
controls, exports, and selected failure/recovery cases. These are maintainer
summaries of the development baseline; detailed private records and study inputs
are not distributed. They do not qualify every exposed operation or a different
Prism version.

The public seed changes two local author-metadata fields to the maintainer's
GitHub handle and preserves its other archive members. **Native Prism runtime
acceptance has not been repeated for the public export.** Source tests and
packaging checks must not be presented as native acceptance. The
[v0.1.0 release notes](https://github.com/jdotc1/prism-native-mcp/releases/tag/v0.1.0)
record the public package checks separately.

Exact appearance matching, arbitrary inferential analyses, and new
multiple-variable graph creation are not claimed. Native distributions and
summary conventions can differ from another plotting engine or a reference
figure. Review the actual exports and any derived static labels for your use.

## Development

Synthetic/offline tests are included. They mock native dispatch where needed and
do not establish live application behavior.

```sh
uv sync --locked --group dev
.venv/bin/python -m pytest tests
```

Two synthetic acceptance runners in [experiments](experiments/) support
analysis-parameter comparisons and failure/recovery work. These are manual tools,
not automatic installation or CI steps. Read their options and source, and use
an explicitly chosen disposable native session. Reuse accepted evidence when
inputs have not changed.

| Path | Contents |
| --- | --- |
| [src/prism_mcp](src/prism_mcp/) | MCP/CLI entry points, archive inspection, native adapters, operation runtime, and synthetic seed. |
| [docs/CAPABILITIES.md](docs/CAPABILITIES.md) | Supported formats, operations, and evidence boundaries. |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Document-state rules, operation records, cancellation, quarantine, and recovery. |
| [tests](tests/) and [experiments](experiments/) | Offline tests and manual synthetic acceptance runners. |

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Use synthetic
examples in [public reports](https://github.com/jdotc1/prism-native-mcp/issues);
include the server and Prism versions and the relevant operation, without
confidential datasets, credentials, or unredacted runtime records.

Project source is [MIT-licensed](LICENSE). GraphPad Prism and dependencies retain
their own terms; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The seed is
a maintainer-created synthetic document. No Prism executable, license,
installation resource, or vendor demonstration archive is distributed.
