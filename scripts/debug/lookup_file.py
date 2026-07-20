"""Inspect a file in the local index by file_key: raw SQL + via DbRepo.

Example: .venv/bin/python scripts/debug/lookup_file.py 1e99bca377ae --folder main
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from televault.db.repo import DbRepo  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file_key")
    parser.add_argument("--folder", default="main")
    parser.add_argument("--db", default="var/data/index.sqlite3")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    print("--- raw SQL: batch_members ---")
    rows = conn.execute(
        "SELECT * FROM batch_members WHERE file_key=?", (args.file_key,)
    ).fetchall()
    for row in rows:
        print(dict(row))
    if not rows:
        print("not found in batch_members")

    print("--- raw SQL: objects ---")
    rows = conn.execute(
        "SELECT * FROM objects WHERE file_key=?", (args.file_key,)
    ).fetchall()
    for row in rows:
        print(dict(row))
    if not rows:
        print("not found in objects")

    repo = DbRepo(conn)

    print("--- DbRepo.list_objects_unified ---")
    found = next(
        (
            o
            for o in repo.list_objects_unified(args.folder)
            if o.file_key == args.file_key
        ),
        None,
    )
    print(f"Found: {found}")

    print("--- DbRepo.get_parts_for_object ---")
    parts = repo.get_parts_for_object(args.folder, args.file_key)
    print(f"Parts: {parts}")


if __name__ == "__main__":
    main()
