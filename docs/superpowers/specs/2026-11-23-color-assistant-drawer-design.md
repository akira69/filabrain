# Docked assistant and native color-form integration

The user approved implementing color-form assistance, consistent icon launchers and a drawer that leaves the host form usable whenever space permits. No upstream PR or release.

Host implementation is now owned by the **Design plugin UI launchers** topic. This plugin branch consumes that shared contract and does not publish the preserved host prototype. The final manifest opt-in uses `ui_launchers` for `colors.editor`, retaining legacy `filament_ui` for older hosts.

## Chosen approach

Extend the host's existing installed-plugin form bridge with a shared responsive drawer helper and a narrowly scoped color context/stage contract. Reuse FilaBrain's exact publisher lookups, product discovery, matched swatches and Chat. Keep host changes in a separate branch; plugin retains compatibility with older hosts. An always-modal drawer prevents concurrent Save, while plugin DOM injection would bypass the host's lifecycle and staging protections; neither fits the requested workflow.

## Behavior and boundaries

- Dock when the host work area can retain 600 CSS pixels for its form plus a 384-pixel drawer and 16-pixel gap. Preferred drawer width is 560 pixels. Shrink the drawer first; below the minimum use the native modal overlay. Resizing never replaces the iframe or its conversation.
- Docked filament forms remain interactive, including Save and validation. A color form already in a native dialog can expand into a two-pane workspace; only the assistant becomes an additional modal on small screens. Close/navigation/successful color Save clears its iframe and reservations.
- Honor validated icon-launcher metadata on both Create and Edit; keep selection actions and legacy text launchers unchanged. FilaBrain opts in to the existing icon style, without enabling automatic opening.
- Discover `ui_launchers` entries for `colors.editor` using effective create/update permissions. Same-origin/current-frame/current-document checks protect the color context and stage messages. Only Name and normalized HEX may be staged; no record IDs or arbitrary properties are writable through the bridge.
- Color assistance accepts an exact RAL/Pantone reference or product clues, shows verified publisher RGB/name/source plus saved matches, and stages an explicitly chosen proposal into the unsaved native form. Preserve existing opacity when changing RGB. Reuse the existing source research services; do not repeat spool research for a color-only request.
- Editing a shared swatch shows its usage count. Opening an existing match is explicit and observes dirty-form discard confirmation; it does not merge swatches or reassign filament links. Failed/unknown lookups never invent a HEX.
- No automatic inventory writes. Ordinary host Save remains authoritative. Reject stale proposals after manual form changes, close, record switches, navigation or save in progress. Keep the assistant conversation intact across dock/modal transitions.

## Verification

Test validated discovery/permissions and Create/Edit launchers; dock/modal transitions, focus/close, independent scrolling and reachable Save at 320/390/768/1024/1280/1440/1920 widths; color context/staging, alpha, dirty Save, stale/manual edits, nested dialogs and lifecycle; exact RAL/Pantone and product-choice flows with unavailable sources. Review both branches together, verify a package on minimum/current hosts and update only the LAN copy, preserving pending audits.
