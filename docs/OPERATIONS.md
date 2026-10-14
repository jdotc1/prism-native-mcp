# Native control and recovery

The server controls a local, separately licensed Prism application through Apple
Events and selected native UI adapters. It does not patch Prism or expose a
generic shell/script execution MCP tool.

1. Call `prism_capabilities` and resolve application-version and permission errors.
2. Use a working copy and inspect the document's native UUIDs. New documents and
   export directories must have fresh destinations.
3. Supply `expected_sha256` when updating a previously inspected revision.
4. Preserve the returned run ID and inspect operation status if completion is
   uncertain. Successful dispatch alone is not semantic completion.
5. Recover only from matching terminal native evidence. Recovery clears the
   relevant quarantine; it does not retry a failed mutation or mark it successful.

Native mutations share a lock. Unsaved or unknown target state is refused by
ordinary mutations, while explicit save intentionally persists current edits.
The server can close/reopen clean target documents during controlled operations.
Prism can autosave; Save As is not a way to undo an earlier source modification.

Completion requires the matching native token/log, sender success, and controller
cleanup. A progress panel disappearing is insufficient. If cleanup remains
uncertain, preserve open work and follow the reported recovery requirements.
Never remove quarantine or force-terminate Prism to bypass the contract.

UI adapters use the selected display identity. Missing or ambiguous displays and
controls are refused rather than guessed. Detailed operation records live under
the current user's application-support directory and can contain document paths,
values, and diagnostic text. Review records before sharing them.

For scientific use, original observations remain authoritative. Prism must
calculate summaries and graph distributions. Review new exports and derived
static labels, and use an owned verification copy for any required data-change
and restoration check. Historical source receipts are not a reason to rerun
unchanged checks.
