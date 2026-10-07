#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import asyncpg


ROOT = Path(__file__).resolve().parents[1]
LINEAGE_MANIFEST = ROOT / "config" / "migration-lineage.v1.json"
ALEMBIC_VERSION_TABLE = "public.alembic_version"


def authorized_revisions() -> set[str]:
    try:
        value = json.loads(LINEAGE_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"RUNTIME_MIGRATION_LINEAGE=FAIL cannot load lineage manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("RUNTIME_MIGRATION_LINEAGE=FAIL lineage manifest root must be an object")
    if value.get("schema_version") != "1.0":
        raise SystemExit("RUNTIME_MIGRATION_LINEAGE=FAIL unsupported lineage manifest schema")
    if value.get("authority") != "reviewed-git-source":
        raise SystemExit("RUNTIME_MIGRATION_LINEAGE=FAIL invalid lineage authority")
    if value.get("alembic_version_table") != ALEMBIC_VERSION_TABLE:
        raise SystemExit("RUNTIME_MIGRATION_LINEAGE=FAIL Alembic version-table authority drift")
    rows = value.get("revisions")
    if not isinstance(rows, list) or not rows:
        raise SystemExit("RUNTIME_MIGRATION_LINEAGE=FAIL lineage manifest has no revisions")

    graph: dict[str, tuple[str, ...]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"revision", "down_revisions"}:
            raise SystemExit(
                f"RUNTIME_MIGRATION_LINEAGE=FAIL invalid lineage revision shape at index {index}"
            )
        revision = row["revision"]
        parents = row["down_revisions"]
        if not isinstance(revision, str) or not revision:
            raise SystemExit(
                f"RUNTIME_MIGRATION_LINEAGE=FAIL invalid revision id at index {index}"
            )
        if revision in graph:
            raise SystemExit(
                f"RUNTIME_MIGRATION_LINEAGE=FAIL duplicate authorized revision {revision}"
            )
        if not isinstance(parents, list) or not all(isinstance(parent, str) and parent for parent in parents):
            raise SystemExit(
                f"RUNTIME_MIGRATION_LINEAGE=FAIL invalid parent list for revision {revision}"
            )
        graph[revision] = tuple(parents)

    missing = sorted({parent for parents in graph.values() for parent in parents if parent not in graph})
    if missing:
        raise SystemExit(
            "RUNTIME_MIGRATION_LINEAGE=FAIL manifest references missing parent revision(s): "
            + ",".join(missing)
        )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(revision: str) -> None:
        if revision in visited:
            return
        if revision in visiting:
            raise SystemExit(
                f"RUNTIME_MIGRATION_LINEAGE=FAIL cycle detected at revision {revision}"
            )
        visiting.add(revision)
        for parent in graph[revision]:
            visit(parent)
        visiting.remove(revision)
        visited.add(revision)

    for revision in graph:
        visit(revision)
    return set(graph)


async def verify_database_lineage(conn: asyncpg.Connection) -> tuple[str, ...]:
    allowed = authorized_revisions()
    table = await conn.fetchval("SELECT to_regclass('public.alembic_version')::text")
    if table is None:
        print("RUNTIME_MIGRATION_LINEAGE=PASS ALEMBIC_VERSION_TABLE=ABSENT")
        return ()

    rows = await conn.fetch(f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE} ORDER BY version_num")
    observed = tuple(sorted({str(row["version_num"]).strip() for row in rows if str(row["version_num"]).strip()}))
    if not observed:
        raise SystemExit("RUNTIME_MIGRATION_LINEAGE=FAIL Alembic version table contains no revision")
    unknown = tuple(revision for revision in observed if revision not in allowed)
    if unknown:
        raise SystemExit(
            "RUNTIME_MIGRATION_LINEAGE=FAIL database reports unknown Alembic revision(s): "
            + ",".join(unknown)
            + "; restore exact historical migration source before any stamp, upgrade, or runtime migration"
        )
    print("RUNTIME_MIGRATION_LINEAGE=PASS DATABASE_REVISIONS=" + ",".join(observed))
    return observed


async def main() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is required")
    conn = await asyncpg.connect(url, command_timeout=30)
    try:
        await verify_database_lineage(conn)

        migrations = sorted((ROOT / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))
        if not migrations:
            raise SystemExit("no runtime migrations found")
        for migration in migrations:
            await conn.execute(migration.read_text(encoding="utf-8"))
            print(f"RUNTIME_MIGRATION_APPLIED={migration.name}")
    finally:
        await conn.close()
    print("RUNTIME_MIGRATION=PASS")


if __name__ == "__main__":
    asyncio.run(main())
