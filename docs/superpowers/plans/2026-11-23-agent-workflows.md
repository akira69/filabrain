# Agent Workflows Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans, implementing inline with one fresh whole-branch review at the end. Steps use checkbox syntax.

**Goal:** Deliver MCP and Chat access to complete intake, job-fit, library cleanup and stock planning without automatic inventory changes.
**Architecture:** Focused validated read services share existing research/color/spool/inventory helpers between HTTP, MCP and Chat. Existing Filament review applies approved drafts; FilaMan native tools retain external write ownership.
**Tech Stack:** Existing Python/FastAPI/SQLAlchemy/Pydantic and TypeScript frontend; no added dependency.
**Spec:** `docs/superpowers/specs/2026-11-23-agent-workflows-design.md`

## Global constraints

- FilaMan 2.2.2+; no host changes, duplicate MCP, new dependencies or inventory application.
- Bound selections to 50, public-link checks and per-request network work to the host MCP deadline; report partial coverage.
- Preserve existing MCP research schema/shape, permissions, standalone/editor drafts and review protections.
- Keep manufacturer names separate from canonical RAL/Pantone swatch names; unknown data stays unknown.

## Review focus

- A small/scoped client cannot read restricted inventory through a composed helper.
- Unknown remaining weights and archived/empty spools cannot produce a confident job-fit/reorder result.
- Failed/denied sources and interrupted scans cannot be presented as complete or broken product links.
- A late task response or reopening history cannot overwrite another draft or repeat research on transfer.
- Preview/stock/audit tools perform no inventory writes, including creating colors or research batches.

### Task 1: MCP parity over existing services

Files: new `filabrain/agent_tools.py`, `tests/test_agent_tools.py`; modify `filabrain/router.py`, `filabrain/color_review.py` only if extracting shared preview logic is needed.
Interfaces: bounded Pydantic arguments and async `(args, db, principal)` handlers; registry `mcp_read_tools` retains `research_filament` and adds color, spool and selected-color-audit tools.
- [x] Write route/MCP tests for exact RAL/Pantone preview/reuse, unknown references, spool candidates/photos, color audit with shared conflicts, disabled plugin, scoped access and rejected input.
- [x] Run tests RED; implement by reusing publisher previews/matching_swatches, spool helpers and color audit helpers. Preserve research arguments, add guidance/reference URL.
- [x] Verify GREEN and commit `feat: expose shared color and spool research through MCP`.

### Task 2: Inventory decision services

Files: new `filabrain/agent_inventory.py`, `tests/test_agent_inventory.py`; adjust `filabrain/chat_context.py` for explicit required-weight input without duplicating inventory selection.
Interfaces: `choose_job_spools`, `audit_library`, `plan_stock` validated read handlers produce reply/evidence plus structured results, coverage and review links.
- [x] Write tests for sufficient/insufficient/unknown weight, optional printer permissions, archived/empty exclusion, missing fields/swatches, duplicate SKU candidates, denied versus missing source pages, paged/selected coverage, target-required and package/unknown-weight reorder limits, real consumption versus correction events, and no writes.
- [x] Run RED; implement reuse/query helpers with bounded scans and optional host-field detection.
- [x] Verify GREEN and commit `feat: add grounded inventory planning and library checks`.

### Task 3: Complete intake proposal

Files: `filabrain/agent_tools.py`, new `tests/test_agent_intake.py`.
Interfaces: `plan_filament_intake` combines research with exact color previews, spool candidates, required fields, duplicate notices and read-only filament/spool drafts. All composed calls share the MCP deadline and do not share a DB session concurrently.
- [x] Write tests for SKU discovery, selected Paramount RAL/Pantone product, unavailable publisher, conflicting/fuzzy/duplicate identity, unknown tare, permissions and no database writes.
- [x] Run RED; implement and register intake plus Task 2 MCP tools using shared handlers. Add HTTP read routes for Chat.
- [x] Verify GREEN and commit `feat: assemble reviewed filament intake proposals`.

### Task 4: Chat workflow entry and review handoff

Files: new `frontend/agent-tasks.ts` if a focused UI helper is needed; modify `frontend/chat.html`, `chat.ts`, `chat.css`, `main.ts`, `research.ts`, `spool-intake.ts`, English/German/French strings and generated page. Tests: new `tests/check-agent-tasks.mjs`, existing workspace suites.
Interfaces: explicit selected task dispatches the same HTTP services; conversation persists task/options/results; intake action transfers the computed proposal to the existing New filament review rather than calling research again.
- [x] Write browser tests RED for task selection, explicit grams, no-model tasks, read-only result cards, no duplicate intake research, final confirmation, permission/failed/late response, history and 320/390/desktop layouts.
- [x] Implement compact house-style controls with clear coverage/uncertainty. Keep research-only panes unchanged. Update Guide/README/MCP allowlist examples to actual tool schemas.
- [x] Verify all browser suites GREEN; commit `feat: bring agent workflows into chat and filament review`.

### Task 5: Review and package

- [ ] Run full backend/browser suites, Ruff on changed Python, build, duplication comparison and diff checks; fix failures.
- [ ] Fresh independent full-branch review; fix important findings with regression tests and rerun affected/full verification.
- [ ] Build immutable candidate ZIP with a new preview version; exact ZIP install/upgrade/rollback/MCP smokes on minimum/current hosts.
- [ ] Update LAN preview and new tool allowlist; test live Paramount intake/color/spool research, inventory planning and source failures using temporary credentials revoked afterward. Leave all inventory and prior audit decisions unchanged.
- [ ] Keep logical commits, push a reviewable PR with concise notes and phone/desktop evidence. Keep LAN running; do not publish another release until requested.
