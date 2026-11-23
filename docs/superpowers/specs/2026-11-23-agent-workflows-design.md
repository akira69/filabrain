# FilaBrain agent workflows

## Agreed intent

Implement the four proposed agent uses: complete filament/spool intake, choose suitable inventory for a job, review library quality, and plan replenishment. First bring the built-in FilaMan MCP extension up to the existing application's color and spool research capabilities. The user explicitly requested both a plan and execution.

## Approach

Reuse existing research, publisher color previews, spool profile matching, grounded inventory context, selected-filament Color audit and reviewed creation. Expose focused read tools and the same services in Chat. Retain one FilaMan MCP endpoint and the existing Filament workspace; do not introduce a second agent server, generic tool framework, new dependencies, or host-app edits. Separate conversational drafts from inventory writes.

A generic autonomous write agent was considered and rejected: it would duplicate existing approvals and would depend on endpoint-specific function-calling support. A tools selector in Chat provides reliable entry points for small/local models; external MCP clients receive equivalent structured results and may use FilaMan's native write tools only after their user's review.

## Behavior

- MCP: extend research with follow-up guidance/source URL; add exact official color preview plus reusable swatches, spool profile candidates/photos, selected-filament color audit, intake, job-fit, library-health and stock-planning tools. Preserve existing research arguments and return shape.
- Intake: accept SKU, URL or clues. Return product candidates until a product is selected. A selected product yields source-backed fields, missing required fields, duplicate warnings, exact color previews and spool candidates. Never invent HEX/tare, choose a fuzzy candidate, create a color, or save inventory. Chat can hand the computed proposal into the existing New filament review without repeating research. The existing final confirmation and Add spool step remain authoritative.
- Job fit: use active spool weights and the existing grounded material/printer knowledge. Distinguish sufficient, insufficient and unknown weight; do not pool separate spools as proof one roll can finish. Accept explicit required grams and printer context. Compatibility uncertainty remains visible.
- Library health: inspect at most 50 records per page or selected IDs. Report missing important fields/swatches, duplicate SKU candidates and inconsistent naming. Optional bounded source checks distinguish missing URLs from denied/rate-limited access. Link findings to the existing filament/research review; no automatic merge or rename. Color audit uses exact references and existing shared-name conflict/usage checks.
- Stock: report known usable grams and unknown-weight spools separately. Use an explicit target or a host-provided threshold; ask for a target when absent. Suggest order quantities only with a known package weight and no unknown usable spool weights. Show measured consumption history where supported, without treating measurement corrections as consumption. No purchases or automatic stock changes.
- Chat: one compact task selector for Chat / Add filament / Choose spool / Check library / Plan stock, with explicit grams when needed. Results use existing source/inventory cards and persist in named conversations. Research/editor workspaces keep their existing controls and behavior. Read-only tools work without requiring a model completion.

## Constraints and acceptance

Support FilaMan 2.2.2+, detect optional newer host fields. No inventory application on production or LAN during verification. Preserve pending Enzo Red/Fighter Jet Blue audits. Every tool validates bounded input, plugin enablement and effective permissions; unavailable sources and partial scans are reported. Keep producer/read routes shared between HTTP and MCP. External clients remain responsible for approving native write calls; the plugin does not synchronize their conversations to app chat.

Verify input limits, authorization/revocation, unknown weights, archived/empty spools, partial scans, publisher/source failure, canonical names, duplicate handling, independent drafts/history, late responses/cancellation, no-model task access, 320px/mobile/desktop layouts and exact ZIP startup on minimum/current hosts. Add no private LAN details to public documentation.
