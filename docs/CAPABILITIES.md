# Supported scope

The implementation targets Prism 11.1.0 build 110218 and archive format `1-6-1`.
Historical evidence refers to the private development baseline described in
[VERIFICATION.md](VERIFICATION.md), not a fresh public-release native run.

| Area | Scope and limitations |
| --- | --- |
| Archive inspection | Supported JSON/CSV tables, result objects, UUIDs, and bindings. Supported multiple-variable objects can be inspected; their drawing and settings precedence are not interpreted. |
| Creation and table updates | Bundled two-group Column seed; at least three numeric observations per group. Prism calculates native dependents. Unsupported cell exclusions/storage are refused. |
| Descriptive analysis | Existing supported descriptive-analysis schema. Historical native comparisons cover confidence levels 90/95 and saved result text at 6/10 significant digits. Routine calls do not independently qualify exact CI endpoints or on-screen formatting. |
| Classic graphs | Native violin and median/IQR workflows. Selected styles and linked composition have historical evidence; not every exposed axis/formatting variant is qualified. |
| Layouts | Native grid and paired overlays. Overlay composition replaces the selected layout's contents, including annotations. Standalone placement/alignment have narrower evidence than composition; resizing remains experimental. |
| Text | Editable native static annotations. They do not automatically update derived labels or establish a native inferential analysis. General existing-label editing is not an exposed MCP capability. |
| Export | Native PDF/SVG/PNG and separate exact saved-table CSV export. Appearance, clipping, units, and rounding still require review of the actual result. |
| Lifecycle | Serialized operations, dirty-document refusals, explicit save/close, source-preserving Save As, durable run records, timeout quarantine, and evidence-based recovery. Cancellation is cooperative, not guaranteed interruption. |

Arbitrary analyses, generic multiple-variable graph creation, and arbitrary
binary drawing edits are not supported. Inspection does not bypass Prism
licensing restrictions. Prism's native distributions and summary conventions
may differ from another plotting engine or a reference figure.
