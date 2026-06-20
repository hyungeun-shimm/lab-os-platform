"""Multi-machine sync (V1) — main ↔ experiment-rig via shared cloud folder.

Architecture:
  * Each machine has an `eln_config` entry with role + machine_id + sync_root.
  * Main exports a `snapshot.json` of canonical tables (animals, users, projects,
    naming conventions). Rigs import it, wiping + reseeding those tables.
  * Rigs push new/updated eln_notes as JSONL files under `outbox/<machine_id>/`.
    Main pulls those JSONL files and imports each note row into its own DB.

Manual triggers only in V1 (admin clicks a button). Background polling is V2.

Schema of one outbox line (`notes-<ts>.jsonl`):
    {"op": "upsert_note", "ts": "...", "row": { ...eln_notes columns... }}

Schema of snapshot.json:
    {
      "schema_version": 1,
      "exported_at":    "<iso>",
      "exported_by":    "<machine_id>",
      "tables": {
        "animals":                [{...}, ...],
        "eln_users":              [{...}, ...],
        "eln_projects":           [{...}, ...],
        "eln_project_members":    [{...}, ...],
        "eln_naming_conventions": [{...}, ...],
        "custom_projects":        [{...}, ...]
      }
    }
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1

# Tables a rig needs to mirror from main (read-only on rig).
SNAPSHOT_TABLES = (
    # ELN-side
    "animals",
    "eln_users",
    "eln_projects",
    "eln_project_members",
    "eln_naming_conventions",
    "custom_projects",
    # Scheduler-side — gives the rig a read-only view of main's calendar
    "events",
    "team_members",
    "project_colors",
    "custom_categories",
    "custom_subcategories",
)

# Columns that get pushed via outbox (every column of eln_notes).
# We pull the actual column list at runtime so it survives schema additions.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Config — stored in the existing eln_config key/value table.
# Keys used: sync.role ('main'|'rig'), sync.machine_id, sync.sync_root
# ---------------------------------------------------------------------------

def _get(con: sqlite3.Connection, key: str) -> Optional[str]:
    row = con.execute("SELECT value FROM eln_config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set(con: sqlite3.Connection, key: str, value: Optional[str]) -> None:
    if value is None:
        con.execute("DELETE FROM eln_config WHERE key = ?", (key,))
    else:
        con.execute(
            "INSERT OR REPLACE INTO eln_config (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, _now_iso()),
        )


def get_config(con: sqlite3.Connection) -> dict:
    """Return current sync config dict. Empty strings if unset."""
    return {
        "role":        (_get(con, "sync.role")       or "main"),
        "machine_id":  (_get(con, "sync.machine_id") or ""),
        "sync_root":   (_get(con, "sync.sync_root")  or ""),
        "last_export": (_get(con, "sync.last_export") or ""),
        "last_import": (_get(con, "sync.last_import") or ""),
        "last_push":   (_get(con, "sync.last_push")   or ""),
        "last_pull":   (_get(con, "sync.last_pull")   or ""),
    }


def set_config(con: sqlite3.Connection, *,
               role: Optional[str] = None,
               machine_id: Optional[str] = None,
               sync_root: Optional[str] = None) -> None:
    if role is not None:
        if role not in ("main", "rig"):
            raise ValueError("role must be 'main' or 'rig'")
        _set(con, "sync.role", role)
    if machine_id is not None:
        mid = (machine_id or "").strip()
        # Allow letters/digits/dashes/underscores
        if mid and not all(c.isalnum() or c in "-_" for c in mid):
            raise ValueError("machine_id must be alphanumeric / - / _ only")
        _set(con, "sync.machine_id", mid or None)
    if sync_root is not None:
        path = (sync_root or "").strip()
        if path:
            p = Path(path).expanduser()
            if not p.exists():
                raise ValueError(f"sync_root does not exist: {p}")
            if not p.is_dir():
                raise ValueError(f"sync_root must be a directory: {p}")
            _set(con, "sync.sync_root", str(p))
        else:
            _set(con, "sync.sync_root", None)
    con.commit()


def _root(con: sqlite3.Connection) -> Path:
    cfg = get_config(con)
    if not cfg["sync_root"]:
        raise RuntimeError("sync.sync_root is not configured.")
    p = Path(cfg["sync_root"]).expanduser()
    if not p.is_dir():
        raise RuntimeError(f"sync_root not a directory: {p}")
    return p


def _outbox_dir(con: sqlite3.Connection, machine_id: Optional[str] = None) -> Path:
    cfg = get_config(con)
    mid = (machine_id or cfg["machine_id"] or "").strip()
    if not mid:
        raise RuntimeError("machine_id is not set.")
    p = _root(con) / "outbox" / mid
    p.mkdir(parents=True, exist_ok=True)
    return p


def _processed_marker(con: sqlite3.Connection) -> Path:
    """Track which outbox files this main has already consumed."""
    cfg = get_config(con)
    mid = cfg["machine_id"] or "main"
    return _root(con) / f".processed-by-{mid}.json"


def _load_processed(con: sqlite3.Connection) -> set:
    mark = _processed_marker(con)
    if not mark.exists():
        return set()
    try:
        return set(json.loads(mark.read_text()))
    except Exception:
        return set()


def _save_processed(con: sqlite3.Connection, items: set) -> None:
    mark = _processed_marker(con)
    mark.write_text(json.dumps(sorted(items), indent=2))


# ---------------------------------------------------------------------------
# SNAPSHOT — main → rig
# ---------------------------------------------------------------------------

def export_snapshot(con: sqlite3.Connection) -> dict:
    """Dump canonical tables to <sync_root>/snapshot.json (atomic).

    Returns counts per table + path.
    """
    cfg = get_config(con)
    if cfg["role"] != "main":
        raise RuntimeError("export_snapshot is only allowed on the 'main' machine.")

    root = _root(con)
    counts = {}
    tables_data = {}
    for tbl in SNAPSHOT_TABLES:
        try:
            rows = [dict(r) for r in con.execute(f"SELECT * FROM {tbl}").fetchall()]
            tables_data[tbl] = rows
            counts[tbl] = len(rows)
        except Exception as e:
            counts[tbl] = f"ERROR: {e}"
            tables_data[tbl] = []

    payload = {
        "schema_version": SCHEMA_VERSION,
        "exported_at":    _now_iso(),
        "exported_by":    cfg["machine_id"] or "main",
        "tables":         tables_data,
    }
    snap = root / "snapshot.json"
    tmp  = root / "snapshot.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, snap)
    (root / "snapshot.meta.json").write_text(json.dumps({
        "exported_at":  payload["exported_at"],
        "exported_by":  payload["exported_by"],
        "table_counts": counts,
        "schema_version": SCHEMA_VERSION,
    }, indent=2))
    _set(con, "sync.last_export", _now_iso())
    con.commit()
    return {"path": str(snap), "counts": counts, "exported_at": payload["exported_at"]}


def import_snapshot(con: sqlite3.Connection) -> dict:
    """Replace local copies of SNAPSHOT_TABLES from <sync_root>/snapshot.json.

    Only allowed on a rig — the rig's own eln_notes are preserved.
    """
    cfg = get_config(con)
    if cfg["role"] != "rig":
        raise RuntimeError("import_snapshot is only allowed on a 'rig' machine.")

    root = _root(con)
    snap = root / "snapshot.json"
    if not snap.exists():
        raise RuntimeError(f"snapshot.json not found in {root}")
    payload = json.loads(snap.read_text())
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(f"snapshot schema_version mismatch (got {payload.get('schema_version')}, expected {SCHEMA_VERSION})")

    counts = {}
    for tbl in SNAPSHOT_TABLES:
        rows = payload["tables"].get(tbl, [])
        try:
            con.execute(f"DELETE FROM {tbl}")
            if rows:
                cols = list(rows[0].keys())
                placeholders = ",".join(["?"] * len(cols))
                col_list = ",".join(cols)
                con.executemany(
                    f"INSERT INTO {tbl} ({col_list}) VALUES ({placeholders})",
                    [[r.get(c) for c in cols] for r in rows],
                )
            counts[tbl] = len(rows)
        except Exception as e:
            counts[tbl] = f"ERROR: {e}"
            con.rollback()
            raise
    _set(con, "sync.last_import", _now_iso())
    con.commit()
    return {"counts": counts, "exported_at": payload.get("exported_at"),
            "exported_by": payload.get("exported_by"), "imported_at": _now_iso()}


# ---------------------------------------------------------------------------
# OUTBOX (notes) — rig → main
# ---------------------------------------------------------------------------

def _note_columns(con: sqlite3.Connection) -> list:
    return [r["name"] for r in con.execute("PRAGMA table_info(eln_notes)").fetchall()]


def push_notes(con: sqlite3.Connection, *, since: Optional[str] = None) -> dict:
    """Dump eln_notes rows updated since the last push (or `since` ISO) into a
    new JSONL file under outbox/<machine_id>/. Returns row count + filename.
    """
    cfg = get_config(con)
    if cfg["role"] != "rig":
        raise RuntimeError("push_notes is only allowed on a 'rig' machine.")
    out_dir = _outbox_dir(con)

    cutoff = since or cfg["last_push"] or "1970-01-01T00:00:00+00:00"
    cols = _note_columns(con)
    col_list = ",".join(cols)
    rows = con.execute(
        f"SELECT {col_list} FROM eln_notes WHERE updated_at > ?",
        (cutoff,),
    ).fetchall()

    ts = _now_iso().replace(":", "-").replace(".", "-")
    fname = f"notes-{ts}.jsonl"
    fpath = out_dir / fname

    n = 0
    with fpath.open("w", encoding="utf-8") as f:
        for r in rows:
            row_dict = {c: r[c] for c in cols}
            line = {"op": "upsert_note", "ts": _now_iso(), "from": cfg["machine_id"],
                    "row": row_dict}
            f.write(json.dumps(line, default=str) + "\n")
            n += 1

    if n == 0:
        fpath.unlink()  # no point keeping an empty file
        result = {"pushed": 0, "filename": None}
    else:
        result = {"pushed": n, "filename": fname, "path": str(fpath)}

    _set(con, "sync.last_push", _now_iso())
    con.commit()
    return result


def pull_notes(con: sqlite3.Connection) -> dict:
    """Read every unprocessed outbox JSONL file across all rigs and upsert each
    note row into eln_notes locally. Conflict resolution: last `updated_at` wins.
    """
    cfg = get_config(con)
    if cfg["role"] != "main":
        raise RuntimeError("pull_notes is only allowed on the 'main' machine.")

    root = _root(con)
    outbox = root / "outbox"
    if not outbox.exists():
        return {"files_processed": 0, "notes_imported": 0, "notes_skipped_older": 0}

    processed = _load_processed(con)
    cols = _note_columns(con)
    col_list = ",".join(cols)
    placeholders = ",".join(["?"] * len(cols))

    files_done, imported, skipped_older = 0, 0, 0
    for rig_dir in sorted(outbox.iterdir()):
        if not rig_dir.is_dir():
            continue
        for fpath in sorted(rig_dir.glob("*.jsonl")):
            key = f"{rig_dir.name}/{fpath.name}"
            if key in processed:
                continue
            try:
                with fpath.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except Exception:
                            continue
                        if ev.get("op") != "upsert_note":
                            continue
                        row = ev.get("row") or {}
                        nid = row.get("id")
                        if not nid:
                            continue
                        # Conflict: skip if local copy is newer.
                        existing = con.execute(
                            "SELECT updated_at FROM eln_notes WHERE id = ?", (nid,)
                        ).fetchone()
                        incoming_ts = row.get("updated_at") or ""
                        if existing and (existing["updated_at"] or "") > incoming_ts:
                            skipped_older += 1
                            continue
                        # Upsert: REPLACE with all known columns from incoming.
                        vals = [row.get(c) for c in cols]
                        con.execute(
                            f"INSERT OR REPLACE INTO eln_notes ({col_list}) VALUES ({placeholders})",
                            vals,
                        )
                        imported += 1
                processed.add(key)
                files_done += 1
            except Exception as e:
                print(f"pull_notes: error on {fpath}: {e}")
                continue
    _save_processed(con, processed)
    _set(con, "sync.last_pull", _now_iso())
    con.commit()
    return {"files_processed": files_done,
            "notes_imported": imported,
            "notes_skipped_older": skipped_older,
            "pulled_at": _now_iso()}
