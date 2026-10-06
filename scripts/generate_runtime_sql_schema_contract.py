#!/usr/bin/env python3
"""Generate the runtime SQL structural contract from a disposable local database only."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SAFE_DB = re.compile(r"middleware_test_[A-Za-z0-9_]+")


def safe_database_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use PostgreSQL")
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("baseline generation requires localhost PostgreSQL")
    if parsed.query or parsed.fragment:
        raise ValueError("baseline DATABASE_URL must not contain query or fragment")
    database = unquote(parsed.path.lstrip("/"))
    if SAFE_DB.fullmatch(database) is None:
        raise ValueError("baseline database must match middleware_test_*")
    return value


async def generate(database_url: str) -> dict[str, object]:
    sys.path.insert(0, str(ROOT))
    import asyncpg

    from scripts.migrate_runtime import database_urls, migration_sets, upgrade_alembic
    from scripts.production_migration_authority import validate_authority
    from scripts.runtime_sql_schema import (
        inspect_schema,
        managed_tables,
        structure_digest,
    )

    expected, _, history_digest = validate_authority(ROOT)
    native_url, sqlalchemy_url = database_urls(database_url)
    await upgrade_alembic(sqlalchemy_url, expected)

    conn = await asyncpg.connect(
        native_url,
        command_timeout=30,
        server_settings={"search_path": "public"},
    )
    try:
        for _, migrations in migration_sets():
            for migration in migrations:
                await conn.execute(migration.read_text(encoding="utf-8"))
        names = managed_tables(ROOT)
        structures = await inspect_schema(conn, names)
    finally:
        await conn.close()

    return {
        "migration_history_sha256": history_digest,
        "schema_version": 1,
        "tables": {
            name: structure_digest(structures[name])
            for name in sorted(structures)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    try:
        database_url = safe_database_url(args.database_url)
        payload = asyncio.run(generate(database_url))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "production_target_allowed": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"RUNTIME_SQL_SCHEMA_BASELINE={output}")
    print("PRODUCTION_TARGET_ALLOWED=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
