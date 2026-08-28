#!/usr/bin/env python3
"""Install a collection snapshot while retaining cloud-side human reviews."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sqlite3
from pathlib import Path


def reviews(path: Path) -> list[tuple[str, str | None, str | None, str]]:
    if not path.is_file():
        return []
    with sqlite3.connect(path) as db:
        return list(db.execute(
            "SELECT review_status,reviewed_at,review_note,note_id FROM notes "
            "WHERE review_status!='未审核' OR reviewed_at IS NOT NULL OR review_note!=''"
        ))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("incoming", type=Path)
    parser.add_argument("live", type=Path)
    args = parser.parse_args()
    if not args.incoming.is_file():
        raise SystemExit(f"incoming database not found: {args.incoming}")

    saved = reviews(args.live)
    args.live.parent.mkdir(parents=True, exist_ok=True)
    temp = args.live.with_suffix(".sqlite3.new")
    temp.unlink(missing_ok=True)
    with sqlite3.connect(args.incoming) as source, sqlite3.connect(temp) as target:
        source.backup(target)
        target.executemany(
            "UPDATE notes SET review_status=?,reviewed_at=?,review_note=? WHERE note_id=?",
            saved,
        )
        target.commit()
        check = target.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise SystemExit(f"database integrity check failed: {check}")

    if args.live.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(args.live, args.live.with_name(f"potential_notes.{stamp}.bak.sqlite3"))
    os.replace(temp, args.live)
    print(f"installed={args.live} preserved_reviews={len(saved)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
