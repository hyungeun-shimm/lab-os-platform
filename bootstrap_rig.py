#!/usr/bin/env python3
"""One-shot setup script for a fresh experiment rig.

Usage (on a freshly-installed rig, run from the Lab_OS folder):
    source .venv/bin/activate
    python3 bootstrap_rig.py \
        --machine-id slice-rig \
        --sync-root "/Users/<you>/Library/CloudStorage/.../My Drive/ELN_sync"

What it does:
  1. Creates all tables (scheduler + ELN side) on this rig's local scheduler.db
  2. Sets sync config: role=rig + machine_id + sync_root
  3. Reads snapshot.json from sync_root and imports animals/users/projects/NC
     into the rig's local DB

After it succeeds you should be able to:
  * Start the server normally:  python3 server.py --port 8766
  * Open http://localhost:8766/eln/ in a browser
  * Sign in with the credentials of any user that exists on main
    (since they got copied in via the snapshot)
  * If you're an admin on main, you'll be admin here too. Switch to admin
    mode and open the Sync page for ongoing Import/Push operations.

This script never starts the web server, never opens a port, and never asks
for a password. It only touches the local DB and the sync folder.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Make the repo root importable.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def init_all_tables(db_path: Path) -> None:
    """Run both the scheduler-side and ELN-side init that server.py runs at startup.

    server.py's init_db() + migrate_db() create animals + events + project tables.
    init_eln_tables() creates the eln_* tables + the sync columns.

    These only run inside server.py's `if __name__ == "__main__":` block, so we
    have to call them explicitly here after importing the module.
    """
    # Importing server defines the FastAPI app and routes but does NOT trigger
    # the `__main__` block, so no tables get created by import alone.
    import server
    from modules.core.db_schema import init_eln_tables

    # Scheduler-side: events, animals, team_members, custom_projects, …
    server.init_db()
    server.migrate_db()

    # ELN-side: eln_users, eln_projects, eln_notes, naming conventions, audit,
    # plus all later migrations (sync columns, can_edit_colony, short_name, …)
    with server.db_conn() as con:
        init_eln_tables(con)


def main() -> int:
    ap = argparse.ArgumentParser(description="Bootstrap a Lab OS rig from a snapshot.")
    ap.add_argument("--machine-id", required=True,
                    help="Unique ID for this rig, e.g. 'slice-rig' or 'behavior-rig'.")
    ap.add_argument("--sync-root", required=True,
                    help="Absolute path to the shared sync folder (Google Drive folder).")
    ap.add_argument("--db", default=str(ROOT / "scheduler.db"),
                    help="Path to the local DB (defaults to scheduler.db in the repo).")
    args = ap.parse_args()

    db_path  = Path(args.db).expanduser().resolve()
    sync_root = Path(args.sync_root).expanduser().resolve()
    machine_id = args.machine_id.strip()

    # Validate inputs early — friendlier errors than what the API would give.
    if not sync_root.is_dir():
        print(f"❌ Sync folder does not exist or is not a directory:\n   {sync_root}", file=sys.stderr)
        return 2
    snap = sync_root / "snapshot.json"
    if not snap.exists():
        print(f"❌ snapshot.json not found in sync folder:\n   {snap}\n"
              f"   Run Export now on the main machine first, and wait for Drive to finish syncing.",
              file=sys.stderr)
        return 2

    print(f"→ Local DB:    {db_path}")
    print(f"→ Sync folder: {sync_root}")
    print(f"→ Machine ID:  {machine_id}")
    print()

    # 1. Create all tables locally
    print("[1/3] Creating tables…")
    init_all_tables(db_path)

    # 2. Set sync config
    print("[2/3] Writing sync config (role=rig)…")
    from modules.core import sync as sync_helpers
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        sync_helpers.set_config(
            con,
            role="rig",
            machine_id=machine_id,
            sync_root=str(sync_root),
        )

        # 3. Import the snapshot — this seeds animals, users, projects, NCs.
        print("[3/3] Importing snapshot…")
        result = sync_helpers.import_snapshot(con)
    finally:
        con.close()

    print()
    print("✅ Done. Imported:")
    for tbl, n in result["counts"].items():
        print(f"     {tbl:<28} {n}")
    print()
    print("Next steps:")
    print("  1. Start the server:")
    print("       python3 server.py --port 8766")
    print("  2. Open http://localhost:8766/eln/ in your browser.")
    print("  3. Sign in with the credentials you already use on the main machine.")
    print("  4. (optional) Switch to admin mode and open the Sync page to push notes back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
