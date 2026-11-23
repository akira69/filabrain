# Color Assistant and Responsive Drawer Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline, with one fresh whole-change review at the end. Track steps with checkboxes.

**Goal:** Dock the assistant beside editable host forms and connect shared color assistance to native color creation/editing.
**Architecture:** A host-owned responsive drawer and validated color-form bridge, consumed by FilaBrain's existing Chat and publisher/product research. Keep host and plugin changes separate; no dependencies or automatic inventory writes.
**Tech Stack:** Existing Astro/TypeScript, native dialogs, FastAPI/Pydantic, Python/SQLAlchemy.
**Spec:** `docs/superpowers/specs/2026-11-23-color-assistant-drawer-design.md`

## Global constraints

- No upstream PR or release. Preserve unrelated work and pending LAN audits.
- User reassigned all host work to the Design plugin UI launchers thread; this branch now owns only FilaBrain lookup, review UI, a narrow host adapter, icon opt-in, tests and package. Host prototype is preserved separately and will not be pushed.
- Host minimum form width 600px, drawer minimum 384px, gap16px and preferred drawer560px; smaller windows use native modality.
- Same iframe across transitions; explicit proposal staging and ordinary native Save. Preserve opacity and reject stale edits.
- Generic host hooks, no plugin DOM injection, duplicate MCP server or added dependencies. Older hosts retain existing plugin behavior.

## Review focus

- Nested color modal and nonmodal assistant remain reachable and never make Save inert when docked.
- Resize/sidebar changes cannot oscillate layout or discard input/conversation/focus.
- Stale/cross-origin messages or a saved/switched form cannot alter a different draft.
- Unknown publishers and fuzzy products cannot become automatic saved colors or palette merges.
- Discovery/launcher behavior remains permission-scoped, legacy-compatible and independent of sidebar visibility.

### Task 1: Host drawer and launchers — transferred to host agent
Files: `frontend/src/lib/plugin-drawer.ts`, `frontend/src/components/FilamentPluginHooks.astro`, `frontend/src/lib/filament-plugin-hooks.ts`, `backend/app/api/v1/system.py`; browser and backend hook tests.
- [x] Write regression tests RED for docked interactive Save, responsive/native fallback with retained iframe, lifecycle, Create/Edit icon metadata and legacy/selection behavior.
- [x] Implement one reusable drawer helper and opt-in Create launcher support, preserving source/frame checks and focus.
- [x] Verify GREEN and commit a coherent host feature.

### Task 2: Native color-form bridge — transferred to host agent
Files: new `ColorPluginHooks.astro`, `color-plugin-hooks.ts`, discovery endpoint/tests; modify `filaments/colors.astro` and color staging browser tests.
- [ ] Write tests RED for create/update discovery, draft Name/HEX staging with preserved alpha, dirty Save, manual edit/race rejection, form switch/close/save and explicit existing-match opening.
- [ ] Implement narrowly validated host context/stage/existing-match messages and reuse the shared drawer.
- [ ] Verify GREEN and commit host color integration separately.

### Task 3: Shared FilaBrain color assistance and icon opt-in
Files: plugin manifest, focused color research handler/UI, existing Chat/task/types/main, translations/Guide, generated page and regression tests.
- [x] Write tests RED for exact official references and saved matches, discovery/selected products, unknown/blocked sources, color host context, alpha-aware staging/usage warning and no automatic writes.
- [x] Reuse existing research/color services and shared UI, expose the color task when hosted by the color editor and opt in to icon launchers. Preserve ordinary Chat/new/edit workflows.
- [x] Verify full plugin backend/browser suites, build and package. Commit logical plugin changes.

### Task 4: Plugin review and LAN — host integration transferred
- [x] Run full plugin backend/browser checks and inspect phone/desktop screenshots.
- [x] Fresh independent whole-change plugin review; fix findings with regression tests.
- [x] Verify exact ZIP on supported hosts and update only the plugin on the LAN copy, preserving inventory/pending audits.
- [x] Commit and push coherent plugin changes; leave LAN running and upstream submission held.

Final shared drawer/launcher implementation, native form integration tests, and host LAN deployment belong to **Design plugin UI launchers**. Plugin fixtures verify the agreed message contract; they do not prove the unshipped host integration.
