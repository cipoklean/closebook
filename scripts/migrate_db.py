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
from sqlalchemy import UniqueConstraint  # noqa: E402

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

        # UNIQUE INDEXES ARE PART OF THE SCHEMA, and a bare ALTER TABLE does not
        # create them. `plan_step.step_key` is only a real guarantee because the
        # database refuses a second row with the same key; adding the column
        # without the index would leave older files enforcing nothing, which is
        # exactly the difference that lets one approved payment become two
        # transfers.
        have_uniq = {u["name"] for u in insp.get_unique_constraints(table.name)}
        have_uniq |= {i["name"] for i in insp.get_indexes(table.name)}
        for con in table.constraints:
            if not isinstance(con, UniqueConstraint) or not con.name:
                continue
            if con.name in have_uniq:
                continue
            cols = ", ".join(c.name for c in con.columns)
            out.append((table.name, con.name,
                        f"CREATE UNIQUE INDEX IF NOT EXISTS {con.name} "
                        f"ON {table.name} ({cols})"))
    return out


def backfill_step_keys(engine) -> int:
    """Give existing plan steps their identity key, where it is unambiguous.

    A step's key is (action, payable_number) -- carried in the JSON payload, so
    there is nothing to copy from a column. Rows whose key COLLIDES are left
    NULL on purpose: the new unique index permits many NULLs, and nulling a
    duplicate is the honest outcome. Picking one of two rows that both claim the
    same payment would invent a winner for a duplicate nobody has resolved.
    """
    import json as _json

    with engine.begin() as conn:
        rows = conn.exec_driver_sql(
            "SELECT id, payload_json FROM plan_step WHERE step_key IS NULL"
        ).fetchall()
        by_key: dict = {}
        for rid, payload in rows:
            try:
                pl = _json.loads(payload or "{}")
            except Exception:                      # noqa: BLE001
                continue
            number, action = pl.get("payable_number"), pl.get("action")
            if not number or not action:
                continue
            by_key.setdefault(f"{action}:{number}", []).append(rid)
        written = 0
        for key, ids in by_key.items():
            if len(ids) != 1:
                continue
            conn.exec_driver_sql(
                "UPDATE plan_step SET step_key = ? WHERE id = ?", (key, ids[0]))
            written += 1
    return written


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
        # The columns can be present and the KEYS still empty (a file migrated
        # before the backfill existed), so this is not a no-op path.
        done = 0 if args.dry_run else backfill_step_keys(engine)
        print("nothing to add — the schema already matches the models")
        if done:
            print(f"backfilled {done} step key(s)")
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
    done = backfill_step_keys(engine)
    if done:
        print(f"backfilled {done} step key(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
