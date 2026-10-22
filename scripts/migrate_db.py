"""Bring an existing database up to the current schema, with a backup first.

The project uses `create_all`, which CREATES missing tables and does nothing at
all to an existing one — so a new column is silently absent and the first query
mentioning it dies with "no such column". That is exactly what happened when
`ledger_entry.created_at` was added for "paid today": the demo book failed on
read.

This is a deliberately small migration runner, not Alembic:
  * it ADDS missing columns and nothing else — no drops, no renames, no data
    rewriting, so it cannot destroy a row;
  * it backs the file up first, timestamped, and says where;
  * it is idempotent: run it twice and the second run reports nothing to do.

Usage:
    cd ~/closebook
    .venv/bin/python scripts/migrate_db.py                 # backs up, then migrates
    .venv/bin/python scripts/migrate_db.py --dry-run       # show what it would do
    CLOSEBOOK_DATABASE_URL=sqlite:////tmp/x.sqlite3 .venv/bin/python scripts/migrate_db.py
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from sqlalchemy import inspect, text  # noqa: E402

from app.db import _default_db_path, make_engine  # noqa: E402
from app.models import Base  # noqa: E402


def _backup(path: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = f"{path}.bak.{stamp}"
    shutil.copy2(path, dest)
    return dest


def plan_changes(engine) -> list[tuple[str, str, str]]:
    """[(table, column, ddl)] for every column the ORM has and the file lacks."""
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    out: list[tuple[str, str, str]] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = (f"ALTER TABLE {table.name} ADD COLUMN {col.name} "
                   f"{col.type.compile(engine.dialect)}")
            # SQLite refuses a NOT NULL column with no default. Every column
            # this runner has had to add is nullable, so leave it that way
            # rather than inventing a default value for existing rows.
            if not col.nullable and col.default is not None and col.default.is_scalar:
                ddl += f" DEFAULT {col.default.arg!r}"
            out.append((table.name, col.name, ddl))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    url = os.environ.get("CLOSEBOOK_DATABASE_URL") or f"sqlite:///{_default_db_path()}"
    print(f"database: {url}")

    engine = make_engine(url)
    changes = plan_changes(engine)
    if not changes:
        print("nothing to do — the schema already matches the models")
        return 0

    for table, column, ddl in changes:
        print(f"  + {table}.{column}")
    if args.dry_run:
        print("(dry run — nothing written)")
        return 0

    if url.startswith("sqlite:///") and ":memory:" not in url:
        path = url[len("sqlite:///"):]
        if os.path.exists(path) and not args.no_backup:
            print(f"backup: {_backup(path)}")

    with engine.begin() as conn:
        for _table, _column, ddl in changes:
            conn.execute(text(ddl))
    print(f"applied {len(changes)} change(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
