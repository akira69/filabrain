# Offline printing references

The catalog contains source records for filament grades, variants and packages. Keep a record ID with any fact used from a catalog. Verify the package before using a spool tare, especially for SpoolmanDB combinations generated from shared product data.

`guidance.json` contains short notes written from the linked guides, manufacturer documents, and exact-grade datasheets. Measurements carry specimen and method details where published. `bambu_presets.json` includes selected temperature settings reviewed against the linked A1 profiles and their inherited settings. These presets require the exact product, printer, nozzle diameter and slicer listed in the record. Check physical feed path and build plate separately.

`publisher_specs.json` contains selected grade-level values checked against the linked manufacturer references. Keep the review date and any relevant test conditions. Package size and tare are outside this list.

## Refreshing the release snapshot

1. Review and update the pinned source revisions and SpoolmanDB digest in `scripts/build_knowledge_bundle.py`. Use the source licenses at those revisions.
2. Save GitHub source archives at the pinned Open Filament Database and Polymaker commits as `ofd.tar.gz` and `polymaker.tar.gz`. Run `python3 scripts/compile_filaments.py` from the pinned SpoolmanDB checkout to produce `filaments.json`. Copy it into the input directory as `spoolman.json` and the checkout's `LICENSE` as `spoolman-LICENSE`. The compiled GitHub Pages export is also usable if it matches the recorded digest.
3. Run `python3 scripts/build_knowledge_bundle.py INPUT_DIRECTORY` from the FilaBrain repository. Review changed packages and settings, then run the knowledge tests and install the release ZIP on the target FilaMan host.

`sources.json` records the generated catalog revisions and `THIRD_PARTY_LICENSES.txt` contains the corresponding MIT notices.
