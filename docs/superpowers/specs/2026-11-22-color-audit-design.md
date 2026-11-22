# Selected-filament color audit

## Intent and agreed behavior

Add Color audit to Research library for the user's selected filaments (up to the existing limit of 50). Find explicit RAL/Pantone references in manufacturer evidence and saved data; propose consistent color naming without erasing manufacturer marketing names. The user selected the existing Research library workflow over a separate palette-wide screen.

The shared Color.name contains the publisher name and full reference, e.g. `Purple violet (RAL 4007)` or `Pantone 7449 C`. Filament.manufacturer_color_name retains `Decepticon Purple`, or receives an independently reviewed manufacturer-name correction when supported by verified product evidence. A code in a manufacturer-name field is retained as reference metadata if that field is corrected. Color.hex_code is a screen preview, not a measured match. RAL and Pantone references do not imply a multi-color filament.

## Approaches

- Selected-filament research mode, chosen by the user: reuse batch selection, source lookup, review queue and swatch comparisons; manufacturer claims can be checked in product context.
- A separate palette-wide screen: broader scanning, but often lacks the product context needed to establish which standard a swatch represents. Not selected.

## Implementation calls

Use batch mode `colors`, retaining existing permissions, ownership, cancellation, retention and stale-record protections. This mode proposes only manufacturer_color_name changes and color-reference information; it does not offer unrelated filament/specification edits. It runs even when normal fill fields are complete. Read explicit references from attached palette names/custom metadata and saved filament metadata as well as current manufacturer research. Never infer a standard code merely from nearest RGB. If an attached palette already has an explicit reference and the filament has a genuine marketing name, the audit may use saved-code provenance plus the official publisher preview without another product/model call. A manufacturer-name correction still requires verified product evidence. Current product identity constraints remain enforced; a standard code masquerading as a manufacturer color name may be omitted only as a marketing-name search constraint, while its code is retained as evidence.

Reuse the existing swatch review component. A batch color preview may include optional shared-name changes with a canonical suggested name, current name/HEX, official preview/source, affected filament count and a token. Show a confirmation before renaming a shared swatch, explicitly saying the rename applies everywhere that swatch is used. Rename is an explicit action requiring colors:update; it changes Color.name and preserves metadata, HEX, assignments and manufacturer color names. Use a token binding the preview, palette state and all linked assignments, with transactional revalidation before applying. Reject stale, unrelated, ambiguous/multicolor, canceled, expired or unauthorized requests.

HEX correction stays a separate choice through the existing keep/reuse/create swatch flow, which changes the selected filament's assignment rather than recoloring every user of a shared swatch. A shared swatch with an explicit conflicting standard must not be silently relabeled. If publisher preview/source is unavailable, show the reference and diagnostic without inventing a name/HEX or offering a destructive correction.

## Scope and verification

No FilaMan host changes or new dependencies. No automatic renames, merges, global HEX updates, color-mode changes or live inventory application during tests. Existing fill/standardize modes and standalone/editor color workflows remain compatible. Cover canonical RAL/Pantone names and suffixes, saved-reference discovery, complete filaments, marketing-name preservation/correction, reference retention, shared usage, authorization/ownership, stale previews/assignments, duplicate names, multi-color ambiguity, unavailable publishers, keyboard/mobile layout and all existing batch actions. Leave live suggestions unapplied.
