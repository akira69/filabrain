"""Build a ZIP for FilaMan's integration plugin installer."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "filabrain"
REQUIRED = {"__init__.py", "icon.svg", "page.html", "plugin.json", "router.py"}


def main() -> None:
    missing = sorted(name for name in REQUIRED if not (PACKAGE / name).is_file())
    if missing:
        raise SystemExit(f"Missing plugin files: {', '.join(missing)}")

    manifest = json.loads((PACKAGE / "plugin.json").read_text(encoding="utf-8"))
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    archive = dist / f"{manifest['plugin_key']}-{manifest['version']}.zip"
    files = [
        path for path in sorted(PACKAGE.rglob("*"))
        if path.is_file() and not any(
            part.startswith(".") or part == "__pycache__"
            for part in path.relative_to(PACKAGE).parts
        )
    ]
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, Path(manifest["plugin_key"]) / path.relative_to(PACKAGE))
        bundle.write(ROOT / "LICENSE.md", Path(manifest["plugin_key"]) / "LICENSE.md")
    print(archive)


if __name__ == "__main__":
    main()
