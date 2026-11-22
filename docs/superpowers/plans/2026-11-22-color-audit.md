# Selected-filament Color Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Audit selected library filaments for RAL/Pantone naming and present source-backed, explicitly reviewed corrections.

**Architecture:** Add `colors` to the existing batch modes. Reuse existing product research and swatch review; keep shared palette renaming separate from the existing per-filament swatch assignment action.

**Tech Stack:** Existing Python/FastAPI/SQLAlchemy, TypeScript and browser tests; no new dependencies.

**Spec:** docs/superpowers/specs/2026-11-22-color-audit-design.md

## Global Constraints

- Research library selection only, at most 50 filaments; no host changes.
- Official RAL/Pantone previews only. Preserve the full reference and manufacturer marketing name.
- Explicit review for all changes. Shared rename requires colors:update and a stale-state/usage token.
- Shared rename never changes HEX, assignments or filament manufacturer names. No automatic merging.
- Existing fill/standardize behavior, session ownership, cancellation, retention and version compatibility remain intact.

## Review Focus

- A shared color also used by unselected filaments: show total usage and confirm global scope.
- A new assignment or palette edit after preview: reject stale rename without overwriting data.
- Complete filament or code-only manufacturer name: audit still runs, retains reference when correcting marketing name.
- Several references or multi-color assignments: no forced equivalence or ambiguous shared rename.
- Missing publishers or revoked permissions: read-only evidence/diagnostic, no inferred name or unauthorized mutation.

### Task 1: Color audit mode and naming actions

**Files:** filabrain/proposals.py, filabrain/color_standards.py, filabrain/batch_api.py, filabrain/batch_worker.py; a focused filabrain/color_audit.py helper if needed; tests/test_color_audit.py.

**Interfaces:**
- BatchMode gains `colors`; MODE_FIELDS['colors'] allows manufacturer_color_name only.
- Existing GET `/batches/{batch}/items/{item}/swatches` retains its response and may add `can_update: boolean` and per-reference `name_changes: Array<{color_id:number,current_name:string,proposed_name:string,usage_count:number,token:string}>`.
- Existing POST swatches supports `action:'rename'` with code, nested name-change token, color_id and color_name (the current name). Server derives proposed_name; caller cannot supply an arbitrary replacement. Existing keep/reuse/create tokens and requests remain compatible.

- [x] Add failing API/worker regressions covering the five review-focus inputs, publisher RAL/Pantone naming, allowed mode fields and code retention.
- [x] Add `colors` research using shared helpers, explicit saved references and verified manufacturer evidence; prevent unrelated proposals and preserve marketing names.
- [x] Add optional naming proposals, canonical name helper shared with existing batch creation, permission checks and transactional rename/token validation. Preserve all unrelated data and emit colors_changed/cache invalidation.
- [x] Run focused tests and complete backend suite; review the entire batch/color diff, not just new helper functions.

### Task 2: Library mode and reviewed naming UI

**Files:** frontend/page.html, frontend/shared/filabrain-batches.ts, frontend/shared/swatch-review.ts, frontend/i18n/{en,de,fr}.json, relevant shared CSS, tests/check-frontend.mjs and focused layout checks.

**Interfaces:** Consume Task 1's optional response fields; use `BatchMode = 'fill'|'standardize'|'colors'`. Renaming sends only `{code,token,action:'rename',color_id,color_name}` to the existing batch swatch endpoint. Draft swatch adapters have no rename actions.

- [x] Add failing browser regressions for Color audit selection, proposed name/current HEX/usage display, confirmation/cancel/submit, no request before confirm, stale/error feedback and mode history labels.
- [x] Add the translated radio and compact shared rename controls using existing components/theme/typography. Show the official source and explicitly label shared scope/name-only change; retain existing HEX reuse/create choices.
- [x] Keep long names, source links and confirmation content usable at 320px, 390px and desktop widths; test keyboard operation and layouts.
- [x] Build and run browser suites, including unchanged standalone/editor palette flows.

### Task 3: Integrate, review and LAN preview

**Files:** README.md, frontend/docs.html, filabrain/page.html; plugin.json only when preparing a distinct candidate ZIP.

- [x] Review both tasks against the spec and run full backend/browser verification with a fresh reviewer; fix concrete issues and recheck affected behavior.
- [x] Document audit scope, shared rename implications, retained marketing names and separate HEX correction. Use a new candidate version before installing changed ZIP content.
- [x] Build/package, verify archive bytes and installer limits, test on minimum supported host and LAN copy, leave suggestions unapplied.
- [ ] Fold fixes into coherent feature commits and push a dedicated feature PR; keep the released fix version/catalog submission separate.
