"""Empty the TestSprite database, in one command, through the app's own schema.

WHAT IT DOES
    Deletes data/testsprite.sqlite3 (plus its -wal/-shm companions) and rebuilds
    an EMPTY database with the real schema, so the next automated run starts
    from nothing and builds its own world through the Setup screens.

WHAT IT DELIBERATELY DOES NOT DO
    Seed anything. Not a customer, not an invoice, not a deposit. The point of
    the TestSprite instance is to exercise the Setup screens as a person would,
    and a seeded row would let a broken screen pass.

SAFETY
    The same guard as the launcher, re-checked here because this is the file
    that DELETES things: the path must end in testsprite.sqlite3, and the demo
    book is refused by name before any unlink. It prints every path it removes,
    so a mistake is visible rather than silent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "testsprite.sqlite3"


def main(argv: list[str]) -> int:
    db_path = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_DB

    # ---- the guards, before anything is touched ---------------------------
    if db_path.name != "testsprite.sqlite3":
        print(f"refused: {db_path.name} is not testsprite.sqlite3.", file=sys.stderr)
        print("         this tool deletes files; it will not delete that one.",
              file=sys.stderr)
        return 1
    demo = (ROOT / "data" / "closebook.sqlite3").resolve()
    if db_path == demo:
        print("refused: that is the DEMO book.", file=sys.stderr)
        return 1

    removed: list[str] = []
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if path.exists():
            size = path.stat().st_size
            path.unlink()
            removed.append(f"{path.name} ({size:,} bytes)")

    if removed:
        print("removed:")
        for line in removed:
            print(f"  data/{line}")
    else:
        print("nothing to remove — it was already empty")

    # ---- rebuild the schema, EMPTY ----------------------------------------
    os.environ["CLOSEBOOK_DATABASE_URL"] = f"sqlite:///{db_path}"
    sys.path.insert(0, str(ROOT / "backend"))

    from app.db import DB, make_engine  # noqa: E402
    from app.models import Base  # noqa: E402

    engine = make_engine(os.environ["CLOSEBOOK_DATABASE_URL"])
    Base.metadata.create_all(engine)

    from sqlalchemy import inspect  # noqa: E402

    tables = sorted(inspect(engine).get_table_names())
    print(f"\nrebuilt {db_path.name}: {len(tables)} tables, every one empty")
    print(f"  {', '.join(tables)}")
    print("\nNo seeded data. Start the instance and build a world through the "
          "Setup screens:\n  make testsprite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
