"""Measure field accuracy and coverage on FilaBrain's sourced product benchmark."""

import argparse
import asyncio
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests" / "fixtures" / "research_benchmark.json"


def equal(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(equal(value, actual.get(key)) for key, value in expected.items())
    if type(expected) in (int, float):
        return type(actual) in (int, float) and math.isclose(expected, actual, rel_tol=1e-6)
    return expected == actual


def score(case, proposal):
    suggestions = {item["field"]: item for item in proposal.get("suggestions", [])}
    actual = {field: item["value"] for field, item in suggestions.items()}
    conflict = case.get("source_conflict", {})
    wrong = [
        field for field, expected in case["expected"].items()
        if field in actual and not equal(expected, actual[field])
        and not (field == conflict.get("field") and any(equal(value, actual[field]) for value in conflict["values"]))
    ]
    wrong.extend(field for field in case.get("forbidden_fields", []) if field in actual)
    missing = [
        field for field in case["expected"]
        if field not in actual or field not in wrong and not suggestions[field].get("sources")
    ]
    issue_names = [re.sub(r"\W", "", str(item.get("name", ""))).casefold() for item in proposal.get("issues", [])]
    missing.extend(issue for issue in case.get("expected_issue_texts", [])
                   if not any(re.sub(r"\W", "", issue).casefold() in name for name in issue_names))
    if conflict := case.get("source_conflict"):
        alternatives = [
            option["value"] for item in proposal.get("issues", [])
            if item.get("field") == conflict["field"] and item["code"] in {"conflict", "source_disagreement"}
            for option in item.get("options", []) if option.get("sources")
        ]
        if not all(any(equal(value, found) for found in alternatives) for value in conflict["values"]):
            missing.append("source_conflict:" + conflict["field"])
    return {
        "id": case["id"],
        "source": case["source"],
        "wrong": wrong,
        "mismatches": {field: {
            "expected": case["expected"].get(field), "actual": actual[field],
            "sources": [source.get("url") for source in suggestions[field].get("sources", [])],
        } for field in wrong},
        "missing": missing,
        "proposed": len(actual),
        "issues": sorted({item["code"] for item in proposal.get("issues", [])}),
    }


async def run(host, scratch, cases, workers, timeout, offline):
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{scratch / 'benchmark.db'}"
    os.environ["PLUGINS_DIR"] = str(scratch / "plugins")
    sys.path[:0] = [str(host / "backend"), str(ROOT)]

    from app.core.database import async_session_maker, engine
    from app.models import Base, InstalledPlugin, Manufacturer
    from filabrain import proposals
    from filabrain.db import Base as PluginBase

    if offline:
        async def unreachable(*_args, **_kwargs):
            raise TimeoutError

        proposals.fetch_supplier = unreachable
        proposals.fetch_archived_supplier = unreachable
        proposals.supplier_sitemap_urls = unreachable
        proposals.catalog_candidates = unreachable
        proposals.ofd_candidates = unreachable
        proposals.search_web = unreachable
        proposals.paramount_product_urls = unreachable
        proposals.request_extraction = unreachable

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(PluginBase.metadata.create_all)
        async with async_session_maker() as db:
            db.add(InstalledPlugin(plugin_key="filabrain", name="FilaBrain", version="0.1.18",
                                   plugin_type="integration", is_active=True))
            sites = {
                "Paramount 3D": "https://www.paramount-3d.com",
                "Bambu Lab": "https://bambu-lab-global.myshopify.com",
                "Polymaker": "https://polymaker.com",
                "Prusament": "https://prusament.com",
            }
            db.add_all(Manufacturer(name=name, url=sites.get(name)) for name in sorted(
                {row["identity"]["manufacturer"] for row in cases}
            ))
            await db.commit()
        semaphore = asyncio.Semaphore(workers)

        async def check(case):
            async with semaphore, async_session_maker() as db:
                try:
                    result = await proposals.research_products(
                        db, proposals.Identity(**case["identity"]),
                        product_url=case.get("product_url"), guidance=case.get("guidance", ""),
                        release_read=True, deadline=asyncio.get_running_loop().time() + timeout,
                    )
                except Exception as exc:
                    return {**score(case, {}), "error": type(exc).__name__}
                return score(case, result)

        return await asyncio.gather(*(check(case) for case in cases))
    finally:
        await engine.dispose()


async def run_saved(host, scratch, source, ids, workers, timeout, baseline):
    from contextlib import closing

    database = scratch / "benchmark.db"
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(database)) as copy,
    ):
        original.backup(copy)
    os.chmod(database, 0o600)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database}"
    os.environ["PLUGINS_DIR"] = str(scratch / "plugins")
    sys.path[:0] = [str(host / "backend"), str(ROOT)]
    migration = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "alembic", "upgrade", "head",
        cwd=host / "backend", env={**os.environ, "PYTHONPATH": str(host / "backend")},
    )
    if await migration.wait():
        raise RuntimeError("Could not migrate the temporary copy.")

    from app.core.database import async_session_maker, engine
    from app.models import Filament
    from filabrain.db import ensure_schema
    from filabrain.proposals import (
        filament_snapshot,
        research_products,
        snapshot_identity,
    )
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    try:
        async with async_session_maker() as db:
            await ensure_schema(db)
            rows = await db.scalars(select(Filament).order_by(Filament.id).options(selectinload(Filament.manufacturer)))
            snapshots = [filament_snapshot(row) for row in rows if not ids or row.id in ids]
        if not snapshots or ids and set(ids) != {row["id"] for row in snapshots}:
            raise ValueError("The selected filament IDs must exist in the backup.")

        capacity = asyncio.Semaphore(workers)

        async def check(snapshot):
            async with capacity, async_session_maker() as db:
                started = time.monotonic()
                try:
                    async with asyncio.timeout(timeout):
                        result = await research_products(
                            db, snapshot_identity(snapshot), snapshot=snapshot, release_read=True,
                            deadline=asyncio.get_running_loop().time() + timeout, investigate_missing=not baseline,
                        )
                except TimeoutError:
                    result = {"suggestions": [], "issues": [{"code": "benchmark_timeout"}]}
                return {"id": snapshot["id"], "seconds": round(time.monotonic() - started, 3), **result}

        results = await asyncio.gather(*(check(snapshot) for snapshot in snapshots))
    finally:
        await engine.dispose()

    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(database)) as copy,
    ):
        for table in ("manufacturers", "filaments", "spools", "colors", "filament_colors"):
            fields = [row[1] for row in original.execute(f"PRAGMA table_info({table})")]
            cols = ", ".join('"' + field.replace('"', '""') + '"' for field in fields)
            query = f"SELECT {cols} FROM {table} ORDER BY {cols}"
            if original.execute(query).fetchall() != copy.execute(query).fetchall():
                raise ValueError(f"Inventory rows changed in {table}.")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", type=Path, help="Stock FilaMan checkout")
    parser.add_argument("--case", action="append", default=[], help="Run this case ID, repeat as needed")
    parser.add_argument("--offline", action="store_true", help="Disable network sources and models")
    parser.add_argument("--workers", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--require-coverage", action="store_true", help="Fail when an expected field is missing")
    parser.add_argument("--sqlite-backup", type=Path, help="Research all saved filaments on a temporary copy")
    parser.add_argument("--saved-id", type=int, action="append", default=[], help="Limit the saved run to these IDs")
    parser.add_argument("--baseline", action="store_true", help="Skip model-led investigation in a saved run")
    args = parser.parse_args()
    host = args.host.resolve(strict=True)
    if args.sqlite_backup:
        if args.case or args.offline or args.require_coverage:
            parser.error("Use only saved-record options with --sqlite-backup.")
        source = args.sqlite_backup.resolve(strict=True)
        with TemporaryDirectory(prefix="filabrain-library-benchmark-") as scratch:
            os.chdir(scratch)
            results = asyncio.run(run_saved(
                host, Path(scratch), source, args.saved_id, args.workers, args.timeout, args.baseline,
            ))
        print(json.dumps({"mode": "baseline" if args.baseline else "investigation", "results": results}, indent=2))
        return int(any(i["code"] == "benchmark_timeout" for item in results for i in item["issues"]))
    if args.saved_id or args.baseline:
        parser.error("--saved-id and --baseline require --sqlite-backup.")
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    ids = {row["id"] for row in cases}
    if set(args.case) - ids:
        parser.error(f"Unknown cases: {', '.join(sorted(set(args.case) - ids))}")
    if args.case:
        cases = [row for row in cases if row["id"] in args.case]
    with TemporaryDirectory(prefix="filabrain-benchmark-") as scratch:
        os.chdir(scratch)
        results = asyncio.run(run(host, Path(scratch), cases, args.workers, args.timeout, args.offline))
    print(json.dumps({
        "results": results,
        "fully_covered": sum(not item["wrong"] and not item["missing"] for item in results),
        "wrong_fields": sum(len(item["wrong"]) for item in results),
    }, indent=2))
    return int(any(item["wrong"] or "error" in item or args.require_coverage and item["missing"] for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
