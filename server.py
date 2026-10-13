"""Lab Scheduler backend.

Run:
  python3 server.py [--port 8766]

Then open http://localhost:8766/scheduler.html
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "scheduler.db"
PMS_PROJECTS_DIR = Path(os.environ.get("SCHEDULER_PMS_PATH") or (ROOT.parent / "Project_management_system" / "projects"))
PMS_PROJECTS_JSON = ROOT / "pms_projects.json"  # cloud fallback: PMS slugs synced from the Mac (tools/sync_pms_projects.py)

# ---------- Version / build info ----------
def _read_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except Exception:
        return "0.0.0"

__version__ = _read_version()
STARTED_AT = datetime.now(timezone.utc).isoformat()

def _build_info() -> dict[str, str]:
    info = {"version": __version__, "git_sha": "", "deployed_at": "", "started_at": STARTED_AT}
    bi = ROOT / "build_info.json"  # written by deploy/deploy.sh on the VM
    if bi.exists():
        try:
            data = json.loads(bi.read_text(encoding="utf-8"))
            info["version"] = data.get("version", info["version"])
            info["git_sha"] = data.get("git_sha", "")
            info["deployed_at"] = data.get("deployed_at", "")
            return info
        except Exception:
            pass
    try:  # dev fallback: live git sha
        import subprocess
        info["git_sha"] = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        pass
    return info

# ---------- DB ----------
def db_conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def migrate_db() -> None:
    """Add new columns to existing DB without dropping data."""
    with db_conn() as con:
        # events table
        cols = {r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()}
        for col, defn in [
            ("subcategory2", "TEXT"), ("cohort", "TEXT"),
            ("assignee", "TEXT"), ("delay_reason", "TEXT"), ("reschedule_to", "TEXT"),
            ("owner_id", "TEXT"),   # eln_users.id of the creator (set on create, preserved on update)
            ("location", "TEXT"), ("rig", "TEXT"),   # where / which rig (admin list + custom)
        ]:
            if col not in cols:
                con.execute(f"ALTER TABLE events ADD COLUMN {col} {defn}")

        # animals table
        acols = {r[1] for r in con.execute("PRAGMA table_info(animals)").fetchall()}
        for col, defn in [
            ("barcode", "TEXT"),   # cage card barcode / sticker number (Excel col U)
            # Safe-sync soft-remove: a mouse_id that vanishes from the source is
            # flagged here (+ audited), never hard-deleted. See modules/core/animal_sync.py.
            ("removed", "INTEGER NOT NULL DEFAULT 0"),
            ("removed_at", "TEXT"),
        ]:
            if col not in acols:
                con.execute(f"ALTER TABLE animals ADD COLUMN {col} {defn}")

        con.execute("""
            CREATE TABLE IF NOT EXISTS vor_okr_logs (
              mouse_id        TEXT PRIMARY KEY,
              dropout         INTEGER NOT NULL DEFAULT 0,
              acclimatization TEXT NOT NULL DEFAULT '["","",""]',
              trials          TEXT NOT NULL DEFAULT '[]',
              created_at      TEXT NOT NULL,
              updated_at      TEXT NOT NULL
            )
        """)
        vcols = {r[1] for r in con.execute("PRAGMA table_info(vor_okr_logs)").fetchall()}
        if "dropout" not in vcols:
            con.execute("ALTER TABLE vor_okr_logs ADD COLUMN dropout INTEGER NOT NULL DEFAULT 0")

        con.execute("""
            CREATE TABLE IF NOT EXISTS ephys_logs (
              mouse_id      TEXT PRIMARY KEY,
              cohort_number TEXT,
              exp_date      TEXT,
              memo          TEXT,
              created_at    TEXT NOT NULL,
              updated_at    TEXT NOT NULL
            )
        """)

        # Rebuild animal_with_state view so computed days_cond logic is always current.
        con.executescript("""
        DROP VIEW IF EXISTS animal_with_state;
        CREATE VIEW animal_with_state AS
        SELECT
          id, mouse_id, project, condition, experiment, cohort, strain,
          dob, sex, dos,
          date_cond1, date_cond2, date_cond3,
          CASE
            WHEN date_cond1 IS NULL THEN NULL
            WHEN date_cond2 IS NOT NULL
              THEN CAST(julianday(date_cond2) - julianday(date_cond1) AS INTEGER)
            ELSE CAST(julianday('now') - julianday(date_cond1) AS INTEGER)
          END AS days_cond1,
          CASE
            WHEN date_cond2 IS NULL THEN NULL
            WHEN date_cond3 IS NOT NULL
              THEN CAST(julianday(date_cond3) - julianday(date_cond2) AS INTEGER)
            ELSE CAST(julianday('now') - julianday(date_cond2) AS INTEGER)
          END AS days_cond2,
          CASE
            WHEN date_cond3 IS NULL THEN NULL
            ELSE CAST(julianday('now') - julianday(date_cond3) AS INTEGER)
          END AS days_cond3,
          sacrificed, sacrificed_date, notes, barcode, synced_at, assigned_to,
          removed, removed_at,
          -- current_state shows the protocol/cohort label the mouse belongs to.
          -- We display the `condition` column verbatim (e.g. DLT, 23dmr, 24dmr, DT)
          -- so the picker keeps cohorts visually distinct even when cond dates
          -- haven't been filled in yet. Only fall back to 'NR' when no condition
          -- is recorded at all. Append '-3' once the mouse passes cond3.
          CASE
            WHEN date_cond3 IS NOT NULL AND date('now') >= date_cond3
              THEN COALESCE(NULLIF(TRIM(condition), ''), 'NR') || '-3'
            ELSE COALESCE(NULLIF(TRIM(condition), ''), 'NR')
          END AS current_state,
          CASE
            WHEN date_cond3 IS NOT NULL AND date('now') >= date_cond3
              THEN CAST(julianday('now') - julianday(date_cond3) AS INTEGER)
            WHEN date_cond2 IS NOT NULL AND date('now') >= date_cond2
              THEN CAST(julianday('now') - julianday(date_cond2) AS INTEGER)
            WHEN date_cond1 IS NOT NULL AND date('now') >= date_cond1
              THEN CAST(julianday('now') - julianday(date_cond1) AS INTEGER)
            ELSE NULL
          END AS days_in_state
        FROM animals;
        """)

        # Archive table for sacrificed / ePhys animals (mirrors animals schema)
        con.execute("""
            CREATE TABLE IF NOT EXISTS animal_archive (
              id              INTEGER PRIMARY KEY AUTOINCREMENT,
              mouse_id        TEXT NOT NULL,
              project         TEXT,
              condition       TEXT,
              experiment      TEXT,
              cohort          TEXT,
              strain          TEXT,
              dob             TEXT,
              sex             TEXT,
              dos             TEXT,
              date_cond1      TEXT,
              date_cond2      TEXT,
              date_cond3      TEXT,
              sacrificed      TEXT,
              sacrificed_date TEXT,
              notes           TEXT,
              barcode         TEXT,
              synced_at       TEXT,
              archived_at     TEXT NOT NULL,
              archive_reason  TEXT,
              assigned_to     TEXT
            )
        """)

        # ---- Safe-sync audit + snapshots (ROADMAP §B v1) ----
        archive_cols = {r[1] for r in con.execute("PRAGMA table_info(animal_archive)")}
        if "assigned_to" not in archive_cols:
            con.execute("ALTER TABLE animal_archive ADD COLUMN assigned_to TEXT")
        # animal_audit: append-only per-change log (field-level for edits).
        # animal_snapshots: full pre-sync JSON of `animals` for point-in-time rollback.
        con.execute("""
            CREATE TABLE IF NOT EXISTS animal_audit (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              ts          TEXT NOT NULL,
              sync_id     TEXT,
              source      TEXT NOT NULL,        -- 'sheet_sync' | 'excel_sync' | 'app' | 'tool'
              actor       TEXT,                 -- user/tool id, nullable
              mouse_id    TEXT,
              change_type TEXT NOT NULL,        -- added|modified|removed|unremoved|rejected
              field       TEXT,
              old_value   TEXT,
              new_value   TEXT,
              detail      TEXT                  -- JSON
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_animal_audit_mid ON animal_audit(mouse_id, ts)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_animal_audit_sync ON animal_audit(sync_id)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS animal_snapshots (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              ts         TEXT NOT NULL,
              sync_id    TEXT,
              row_count  INTEGER NOT NULL,
              payload    TEXT NOT NULL          -- JSON array of all `animals` rows pre-sync
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_animal_snapshots_ts ON animal_snapshots(ts)")
        con.execute("""CREATE TABLE IF NOT EXISTS animal_sync_runs (
            sync_id TEXT PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL,
            report TEXT NOT NULL
        )""")

def init_db() -> None:
    with db_conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS events (
          id            TEXT PRIMARY KEY,
          title         TEXT NOT NULL,
          category      TEXT NOT NULL,
          subcategory   TEXT,
          subcategory2  TEXT,
          cohort        TEXT,
          project_slug  TEXT,
          status        TEXT DEFAULT 'planned',
          start_at      TEXT NOT NULL,
          end_at        TEXT NOT NULL,
          all_day       INTEGER DEFAULT 0,
          notes         TEXT,
          mice          TEXT,           -- JSON array
          recur         TEXT,           -- JSON (future)
          assignee      TEXT,
          delay_reason  TEXT,
          reschedule_to TEXT,
          created_at    TEXT NOT NULL,
          updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS team_members (
          id         TEXT PRIMARY KEY,
          name       TEXT NOT NULL,
          role       TEXT,
          color      TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at);
        CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_slug);

        CREATE TABLE IF NOT EXISTS custom_projects (
          slug       TEXT PRIMARY KEY,
          name       TEXT NOT NULL,
          color      TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_colors (
          slug  TEXT PRIMARY KEY,
          color TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS custom_categories (
          key   TEXT PRIMARY KEY,
          label TEXT NOT NULL,
          color TEXT
        );

        CREATE TABLE IF NOT EXISTS custom_subcategories (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          category_key TEXT NOT NULL,
          label        TEXT NOT NULL,
          UNIQUE(category_key, label)
        );

        -- Admin-curated event resources: 'location' (room/place) and 'rig' (rig number/name).
        -- Events may also store a free-text custom value; this table is just the dropdown list.
        CREATE TABLE IF NOT EXISTS scheduler_resources (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          kind       TEXT NOT NULL,            -- 'location' | 'rig'
          value      TEXT NOT NULL,
          sort_order INTEGER DEFAULT 0,
          created_at TEXT NOT NULL,
          UNIQUE(kind, value)
        );

        CREATE TABLE IF NOT EXISTS lab_notes (
          id           TEXT PRIMARY KEY,
          project_slug TEXT,
          title        TEXT NOT NULL DEFAULT 'Untitled',
          content      TEXT DEFAULT '',
          tags         TEXT DEFAULT '[]',
          note_date    TEXT NOT NULL,
          event_ids    TEXT DEFAULT '[]',
          mouse_ids    TEXT DEFAULT '[]',
          created_at   TEXT NOT NULL,
          updated_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notes_project ON lab_notes(project_slug);
        CREATE INDEX IF NOT EXISTS idx_notes_date ON lab_notes(note_date);

        CREATE TABLE IF NOT EXISTS animals (
          id              INTEGER PRIMARY KEY AUTOINCREMENT,
          mouse_id        TEXT NOT NULL,
          project         TEXT,
          condition       TEXT,
          experiment      TEXT,
          cohort          TEXT,
          strain          TEXT,
          dob             TEXT,
          sex             TEXT,
          dos             TEXT,
          date_cond1      TEXT,
          date_cond2      TEXT,
          date_cond3      TEXT,
          days_cond1      TEXT,
          days_cond2      TEXT,
          days_cond3      TEXT,
          sacrificed      TEXT,
          sacrificed_date TEXT,
          notes           TEXT,
          synced_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_animals_mouse_id ON animals(mouse_id);

        CREATE TABLE IF NOT EXISTS colony_cond_labels (
          condition TEXT NOT NULL,
          slot      TEXT NOT NULL,  -- 'c1' | 'c2' | 'c3'
          label     TEXT NOT NULL,
          PRIMARY KEY (condition, slot)
        );

        CREATE TABLE IF NOT EXISTS animal_config (
          key   TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS vor_okr_logs (
          mouse_id        TEXT PRIMARY KEY,
          dropout         INTEGER NOT NULL DEFAULT 0,
          acclimatization TEXT NOT NULL DEFAULT '["","",""]',
          trials          TEXT NOT NULL DEFAULT '[]',
          created_at      TEXT NOT NULL,
          updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ephys_logs (
          mouse_id      TEXT PRIMARY KEY,
          cohort_number TEXT,
          exp_date      TEXT,
          memo          TEXT,
          created_at    TEXT NOT NULL,
          updated_at    TEXT NOT NULL
        );

        -- View: replaces stored days_cond1/2/3 with live computed values.
        -- days_cond1 counts from date_cond1 → date_cond2 (freezes when cond-2 starts).
        -- days_cond2 counts from date_cond2 → date_cond3 (freezes when cond-3 starts).
        -- days_cond3 counts from date_cond3 → today (runs until sacrificed).
        -- current_state / days_in_state reflect the mouse's current active condition.
        DROP VIEW IF EXISTS animal_with_state;
        CREATE VIEW animal_with_state AS
        SELECT
          id, mouse_id, project, condition, experiment, cohort, strain,
          dob, sex, dos,
          date_cond1, date_cond2, date_cond3,
          -- days_cond1: date_cond1 → date_cond2 (frozen) or today (still counting)
          CASE
            WHEN date_cond1 IS NULL THEN NULL
            WHEN date_cond2 IS NOT NULL
              THEN CAST(julianday(date_cond2) - julianday(date_cond1) AS INTEGER)
            ELSE CAST(julianday('now') - julianday(date_cond1) AS INTEGER)
          END AS days_cond1,
          -- days_cond2: date_cond2 → date_cond3 (frozen) or today (still counting)
          CASE
            WHEN date_cond2 IS NULL THEN NULL
            WHEN date_cond3 IS NOT NULL
              THEN CAST(julianday(date_cond3) - julianday(date_cond2) AS INTEGER)
            ELSE CAST(julianday('now') - julianday(date_cond2) AS INTEGER)
          END AS days_cond2,
          -- days_cond3: date_cond3 → today (always counting until further notice)
          CASE
            WHEN date_cond3 IS NULL THEN NULL
            ELSE CAST(julianday('now') - julianday(date_cond3) AS INTEGER)
          END AS days_cond3,
          sacrificed, sacrificed_date, notes, barcode, synced_at, assigned_to,
          -- current_state: which condition the mouse is in right now
          -- current_state shows the protocol/cohort label the mouse belongs to.
          -- We display the `condition` column verbatim (e.g. DLT, 23dmr, 24dmr, DT)
          -- so the picker keeps cohorts visually distinct even when cond dates
          -- haven't been filled in yet. Only fall back to 'NR' when no condition
          -- is recorded at all. Append '-3' once the mouse passes cond3.
          CASE
            WHEN date_cond3 IS NOT NULL AND date('now') >= date_cond3
              THEN COALESCE(NULLIF(TRIM(condition), ''), 'NR') || '-3'
            ELSE COALESCE(NULLIF(TRIM(condition), ''), 'NR')
          END AS current_state,
          -- days_in_state: days in the current active condition
          CASE
            WHEN date_cond3 IS NOT NULL AND date('now') >= date_cond3
              THEN CAST(julianday('now') - julianday(date_cond3) AS INTEGER)
            WHEN date_cond2 IS NOT NULL AND date('now') >= date_cond2
              THEN CAST(julianday('now') - julianday(date_cond2) AS INTEGER)
            WHEN date_cond1 IS NOT NULL AND date('now') >= date_cond1
              THEN CAST(julianday('now') - julianday(date_cond1) AS INTEGER)
            ELSE NULL
          END AS days_in_state
        FROM animals;
        """)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------- Project source ----------
EXCLUDED_PROJECT_PREFIXES = ("_", ".")
EXCLUDED_PROJECT_NAMES = {"published"}

def discover_pms_projects() -> list[dict[str, Any]]:
    """Project slugs from the PMS. Reads the local folder when present (Mac), else
    falls back to pms_projects.json synced from the Mac (cloud — the VM has no PMS
    folder). Keep both paths so the same code runs locally and in the cloud."""
    out: list[dict[str, Any]] = []
    if PMS_PROJECTS_DIR.is_dir():
        for child in sorted(PMS_PROJECTS_DIR.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith(EXCLUDED_PROJECT_PREFIXES) or name in EXCLUDED_PROJECT_NAMES:
                continue
            out.append({"slug": name, "name": name, "source": "pms"})
        return out
    if PMS_PROJECTS_JSON.is_file():
        try:
            for item in json.loads(PMS_PROJECTS_JSON.read_text()):
                slug = item.get("slug") if isinstance(item, dict) else str(item)
                if not slug or slug.startswith(EXCLUDED_PROJECT_PREFIXES) or slug in EXCLUDED_PROJECT_NAMES:
                    continue
                name = (item.get("name") if isinstance(item, dict) else None) or slug
                out.append({"slug": slug, "name": name, "source": "pms"})
        except Exception:
            pass
    return out

def project_color_map(con: sqlite3.Connection) -> dict[str, str]:
    rows = con.execute("SELECT slug, color FROM project_colors").fetchall()
    return {r["slug"]: r["color"] for r in rows}

DEFAULT_PROJECT_PALETTE = [
    "#5AC8FA", "#FF9500", "#34C759", "#AF52DE", "#FF3B30",
    "#5856D6", "#FF2D55", "#00C7BE", "#A2845E", "#30B0C7",
]

def assign_default_color(slug: str, existing: dict[str, str]) -> str:
    if slug in existing:
        return existing[slug]
    idx = sum(ord(c) for c in slug) % len(DEFAULT_PROJECT_PALETTE)
    return DEFAULT_PROJECT_PALETTE[idx]

# ---------- App ----------
app = FastAPI(title="Lab OS")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ---------- ELN module ----------
# Tables, auth, users, projects. Routes live under /api/eln/*.
from modules.core.db_schema import init_eln_tables  # noqa: E402
from modules.core import animals as animals_dal  # noqa: E402
from modules.core import animal_sync  # noqa: E402
from modules.core import users as users_dal  # noqa: E402
from modules.core import auth as auth_helpers  # noqa: E402
from modules.core import sync as sync_helpers  # noqa: E402
from modules.autoschedule import service as autosched_service  # noqa: E402
from modules.eln.api import router as eln_router  # noqa: E402
app.include_router(eln_router)
from modules.eln.tracking import router as tracking_router
app.include_router(tracking_router)

@app.get("/api/version")
def api_version() -> dict[str, str]:
    return _build_info()

# ---------- Scheduler-API auth gate ----------
# The scheduler-side API (/api/*, excluding the ELN router which self-gates) was
# fully open on the public URL — anyone with the address could read the colony or
# read/write/delete events. Require an ELN session cookie (browsers, same-origin)
# OR the shared service token (machine callers: Slack bot/rigs, via X-Labos-Token).
# ELN routes keep their own per-route auth (and public login/register live under
# /api/eln/), so they are skipped here.
_PUBLIC_API_PATHS = {"/api/version"}

@app.middleware("http")
async def _scheduler_api_guard(request: Request, call_next):
    p = request.url.path
    if (request.method != "OPTIONS"
            and p.startswith("/api/")
            and p not in _PUBLIC_API_PATHS
            and not p.startswith("/api/eln/")):
        if not (auth_helpers.service_token_ok(request) or auth_helpers.current_user(request)):
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
    return await call_next(request)

# ---------- Events ----------
def row_to_event(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "title": r["title"],
        "category": r["category"],
        "subcategory": r["subcategory"] or "",
        "subcategory2": r["subcategory2"] or "",
        "cohort": r["cohort"] or "",
        "project_slug": r["project_slug"] or "",
        "status": r["status"] or "planned",
        "start": r["start_at"],
        "end": r["end_at"],
        "all_day": bool(r["all_day"]),
        "notes": r["notes"] or "",
        "mice": json.loads(r["mice"]) if r["mice"] else [],
        "recur": json.loads(r["recur"]) if r["recur"] else None,
        "assignee": r["assignee"] or "",
        "owner_id": r["owner_id"] or "",
        "location": r["location"] or "",
        "rig": r["rig"] or "",
        "delay_reason": r["delay_reason"] or "",
        "reschedule_to": r["reschedule_to"] or "",
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }

@app.get("/api/events")
def list_events() -> dict[str, Any]:
    with db_conn() as con:
        rows = con.execute("SELECT * FROM events ORDER BY start_at").fetchall()
    return {"events": [row_to_event(r) for r in rows]}

def upsert_event(payload: dict[str, Any], event_id: Optional[str] = None) -> dict[str, Any]:
    if event_id:
        with db_conn() as con:
            previous = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if previous:
            payload = {**row_to_event(previous), **payload}
    required = ["title", "category", "start", "end"]
    for k in required:
        if k not in payload or payload[k] in (None, ""):
            raise HTTPException(400, f"Missing field: {k}")
    from modules.eln.tracking import local_time
    try:
        if local_time(payload["end"]) <= local_time(payload["start"]):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, "End time must be after start time; use valid ISO dates.")
    if payload.get("status", "planned") not in {"planned", "in_progress", "done", "delayed", "skipped"}:
        raise HTTPException(400, "Invalid event status.")
    eid = event_id or payload.get("id") or _uid()
    now = now_iso()
    with db_conn() as con:
        existing = con.execute("SELECT id, created_at FROM events WHERE id=?", (eid,)).fetchone()
        created_at = existing["created_at"] if existing else now
        con.execute("""
            INSERT INTO events (id, title, category, subcategory, subcategory2, cohort,
                                project_slug, status, start_at, end_at, all_day,
                                notes, mice, recur, assignee, delay_reason, reschedule_to,
                                owner_id, location, rig, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              title=excluded.title, category=excluded.category,
              subcategory=excluded.subcategory, subcategory2=excluded.subcategory2,
              cohort=excluded.cohort, project_slug=excluded.project_slug,
              status=excluded.status, start_at=excluded.start_at, end_at=excluded.end_at,
              all_day=excluded.all_day, notes=excluded.notes, mice=excluded.mice,
              recur=excluded.recur, assignee=excluded.assignee,
              delay_reason=excluded.delay_reason, reschedule_to=excluded.reschedule_to,
              location=excluded.location, rig=excluded.rig,
              updated_at=excluded.updated_at
        """, (
            eid, payload["title"], payload["category"],
            payload.get("subcategory") or None,
            payload.get("subcategory2") or None,
            payload.get("cohort") or None,
            payload.get("project_slug") or None, payload.get("status") or "planned",
            payload["start"], payload["end"], 1 if payload.get("all_day") else 0,
            payload.get("notes") or None,
            json.dumps(payload.get("mice") or []),
            json.dumps(payload["recur"]) if payload.get("recur") else None,
            payload.get("assignee") or None,
            payload.get("delay_reason") or None,
            payload.get("reschedule_to") or None,
            payload.get("owner_id") or None,
            payload.get("location") or None,
            payload.get("rig") or None,
            created_at, now,
        ))
        row = con.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    return row_to_event(row)

@app.post("/api/events")
async def create_event(req: Request) -> dict[str, Any]:
    payload = await req.json()
    if not payload.get("owner_id"):
        u = auth_helpers.current_user(req)
        if u:
            payload["owner_id"] = u["id"]
    return upsert_event(payload)

@app.put("/api/events/{event_id}")
async def update_event(event_id: str, req: Request) -> dict[str, Any]:
    payload = await req.json()
    return upsert_event(payload, event_id)

@app.delete("/api/events/{event_id}")
def delete_event(event_id: str) -> dict[str, Any]:
    with db_conn() as con:
        cur = con.execute("DELETE FROM events WHERE id=?", (event_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, "Not found")
    return {"ok": True}

# ---------- AI scheduling (autoschedule, BYOK) ----------
# Natural language → Gemini params → deterministic engine → preview → commit.
# Unauthenticated for now (matches /api/events); per-user auth + keys land with
# scheduler↔ELN identity unification.

class _AISchedIn(BaseModel):
    text: str
    assignee: Optional[str] = ""

@app.post("/api/schedule/ai")
def schedule_ai_preview(payload: _AISchedIn) -> dict[str, Any]:
    from datetime import date as _date
    with db_conn() as con:
        try:
            return autosched_service.preview(
                con, payload.text, assignee=payload.assignee or "",
                today=_date.today().isoformat(),
            )
        except RuntimeError as e:
            raise HTTPException(400, str(e))

class _AICommitIn(BaseModel):
    events: list

@app.post("/api/schedule/commit")
def schedule_commit(payload: _AICommitIn, req: Request) -> dict[str, Any]:
    u = auth_helpers.current_user(req)
    owner = u["id"] if u else None
    saved = []
    for ev in payload.events:
        if owner and not ev.get("owner_id"):
            ev["owner_id"] = owner
        saved.append(upsert_event(ev))
    return {"ok": True, "count": len(saved), "events": saved}

class _AIDeleteCommitIn(BaseModel):
    event_ids: list[str]

@app.post("/api/schedule/delete")
def schedule_delete_commit(payload: _AIDeleteCommitIn) -> dict[str, Any]:
    ids = []
    seen = set()
    for raw in payload.event_ids:
        event_id = str(raw or "").strip()
        if event_id and event_id not in seen:
            ids.append(event_id)
            seen.add(event_id)
    if not ids:
        return {"ok": True, "count": 0, "deleted": [], "not_found": []}

    placeholders = ",".join("?" for _ in ids)
    with db_conn() as con:
        rows = con.execute(
            f"SELECT id, title, start_at FROM events WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        found = {r["id"] for r in rows}
        con.execute(f"DELETE FROM events WHERE id IN ({placeholders})", ids)
    return {
        "ok": True,
        "count": len(found),
        "deleted": [{"id": r["id"], "title": r["title"], "start": r["start_at"]} for r in rows],
        "not_found": [event_id for event_id in ids if event_id not in found],
    }

# ---------- Event resources (locations / rigs) — admin-curated list + free custom ----------
@app.get("/api/schedule/resources")
def list_resources() -> dict[str, Any]:
    with db_conn() as con:
        rows = con.execute(
            "SELECT id, kind, value FROM scheduler_resources ORDER BY kind, sort_order, value"
        ).fetchall()
    out: dict[str, Any] = {"locations": [], "rigs": []}
    for r in rows:
        (out["locations"] if r["kind"] == "location" else out["rigs"]).append(
            {"id": r["id"], "value": r["value"]})
    return out

class _ResourceIn(BaseModel):
    kind: str
    value: str

@app.post("/api/schedule/resources")
def add_resource(payload: _ResourceIn, req: Request) -> dict[str, Any]:
    u = auth_helpers.current_user(req)
    if not u or u.get("role") != "admin":
        raise HTTPException(403, "Admin only.")
    kind = (payload.kind or "").strip().lower()
    value = (payload.value or "").strip()
    if kind not in ("location", "rig"):
        raise HTTPException(400, "kind must be 'location' or 'rig'.")
    if not value:
        raise HTTPException(400, "value is required.")
    with db_conn() as con:
        con.execute("INSERT OR IGNORE INTO scheduler_resources (kind, value, sort_order, created_at) "
                    "VALUES (?,?,?,?)", (kind, value, 0, now_iso()))
    return {"ok": True}

@app.delete("/api/schedule/resources/{rid}")
def delete_resource(rid: int, req: Request) -> dict[str, Any]:
    u = auth_helpers.current_user(req)
    if not u or u.get("role") != "admin":
        raise HTTPException(403, "Admin only.")
    with db_conn() as con:
        con.execute("DELETE FROM scheduler_resources WHERE id=?", (rid,))
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════════════════════
# Multi-machine sync (V1 — manual triggers)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/sync/config")
def sync_get_config(_user: dict = Depends(auth_helpers.require_user)) -> dict[str, Any]:
    with db_conn() as con:
        return sync_helpers.get_config(con)

class _SyncConfigIn(BaseModel):
    role: Optional[str] = None
    machine_id: Optional[str] = None
    sync_root: Optional[str] = None

@app.put("/api/sync/config")
def sync_set_config(payload: _SyncConfigIn,
                    _user: dict = Depends(auth_helpers.require_admin)) -> dict[str, Any]:
    with db_conn() as con:
        try:
            sync_helpers.set_config(con, role=payload.role, machine_id=payload.machine_id,
                                    sync_root=payload.sync_root)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "config": sync_helpers.get_config(con)}

@app.post("/api/sync/snapshot/export")
def sync_export(_user: dict = Depends(auth_helpers.require_admin)) -> dict[str, Any]:
    with db_conn() as con:
        try:
            return sync_helpers.export_snapshot(con)
        except RuntimeError as e:
            raise HTTPException(400, str(e))

@app.post("/api/sync/snapshot/import")
def sync_import(_user: dict = Depends(auth_helpers.require_user)) -> dict[str, Any]:
    # Any signed-in user on a rig may import: it refreshes read-only mirror
    # tables (animals/users/projects/calendar) from main and never touches the
    # rig's own eln_notes. import_snapshot() still enforces role == 'rig'.
    with db_conn() as con:
        try:
            return sync_helpers.import_snapshot(con)
        except RuntimeError as e:
            raise HTTPException(400, str(e))

@app.post("/api/sync/notes/push")
def sync_push_notes(_user: dict = Depends(auth_helpers.require_user)) -> dict[str, Any]:
    with db_conn() as con:
        try:
            return sync_helpers.push_notes(con)
        except RuntimeError as e:
            raise HTTPException(400, str(e))

@app.post("/api/sync/notes/pull")
def sync_pull_notes(_user: dict = Depends(auth_helpers.require_admin)) -> dict[str, Any]:
    with db_conn() as con:
        try:
            return sync_helpers.pull_notes(con)
        except RuntimeError as e:
            raise HTTPException(400, str(e))

@app.get("/api/sync/_diag")
def sync_diag(request: Request) -> dict[str, Any]:
    """No-auth diagnostic — reports what the server actually sees from the
    browser, plus this process's identity. Use to debug 401s and to detect
    whether more than one server/DB is serving the rig (PID or db_path that
    flips between calls == duplicate instances)."""
    import os as _os
    cookie_names = list(request.cookies.keys())
    session_tok = request.cookies.get(auth_helpers.SESSION_COOKIE) or ""
    session_present = bool(session_tok)
    session_lookup = None
    token_in_sessions = None
    session_count = None
    user_count = None
    if session_present:
        try:
            with db_conn() as con:
                con.row_factory = sqlite3.Row
                # Raw existence of the token, independent of the user JOIN.
                # token_in_sessions True + session_lookup None  => orphaned user
                # token_in_sessions False                       => session is in a different DB
                row = con.execute(
                    "SELECT user_id, expires_at FROM eln_sessions WHERE token = ?",
                    (session_tok,),
                ).fetchone()
                token_in_sessions = bool(row)
                session_count = con.execute("SELECT COUNT(*) FROM eln_sessions").fetchone()[0]
                user_count = con.execute("SELECT COUNT(*) FROM eln_users").fetchone()[0]
                sess = auth_helpers.lookup_session(con, session_tok)
                if sess:
                    session_lookup = {
                        "user_id": sess["user_id"],
                        "username": sess["user"]["username"],
                        "role": sess["user"]["role"],
                        "session_mode": sess["mode"],
                        "effective_role": sess["effective_role"],
                    }
                elif row:
                    session_lookup = {
                        "orphaned": True,
                        "session_user_id": row["user_id"],
                        "user_exists": bool(con.execute(
                            "SELECT 1 FROM eln_users WHERE id = ?", (row["user_id"],)
                        ).fetchone()),
                    }
        except Exception as e:
            session_lookup = {"error": str(e)}
    return {
        "server_pid": _os.getpid(),
        "db_path": str(DB_PATH),
        "request_url": str(request.url),
        "request_origin": request.headers.get("origin"),
        "request_referer": request.headers.get("referer"),
        "request_host": request.headers.get("host"),
        "cookie_names": cookie_names,
        "session_cookie_present": session_present,
        "session_token_preview": (session_tok[:8] + "…") if session_tok else None,
        "token_in_sessions": token_in_sessions,
        "session_count": session_count,
        "user_count": user_count,
        "session_lookup": session_lookup,
    }


@app.post("/api/admin/shutdown")
def admin_shutdown(_user: dict = Depends(auth_helpers.require_admin)) -> dict[str, Any]:
    """Cleanly stop this Lab OS server. Used by the rig dashboard's 'Stop server' button."""
    import os as _os, threading as _th, time as _time
    def _kill():
        _time.sleep(0.4)   # let the HTTP response flush first
        _os._exit(0)
    _th.Thread(target=_kill, daemon=True).start()
    return {"ok": True, "message": "Server is shutting down."}


@app.post("/api/events/reassign-from-mice")
def reassign_events_from_mice(_user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    """For every event with mice, derive the project_slug from the animals'
    `project` column. Maps a free-text label (e.g. 'TeMP_tuning') to the
    canonical slug ('temp-tuning') by lowercasing and `_` → `-`.

    Updates the event only if the derived slug differs from current one AND
    matches a known custom_projects row. Mixed-project events are left alone.
    """
    with db_conn() as con:
        known_slugs = {r["slug"] for r in con.execute("SELECT slug FROM custom_projects").fetchall()}
        evs = con.execute("SELECT id, project_slug, mice FROM events WHERE mice IS NOT NULL AND mice != '[]'").fetchall()
        updated, skipped, ambiguous = 0, 0, 0
        for r in evs:
            try:
                mids = json.loads(r["mice"] or "[]")
            except Exception:
                skipped += 1; continue
            if not mids:
                skipped += 1; continue
            ph = ",".join(["?"] * len(mids))
            anim_projects = {
                (row["project"] or "").strip().lower().replace("_", "-")
                for row in con.execute(
                    f"SELECT DISTINCT project FROM animals WHERE mouse_id IN ({ph})", mids
                ).fetchall()
            }
            anim_projects.discard("")
            valid = anim_projects & known_slugs
            if len(valid) == 1:
                target = valid.pop()
                if target != r["project_slug"]:
                    con.execute("UPDATE events SET project_slug=?, updated_at=? WHERE id=?",
                                (target, now_iso(), r["id"]))
                    updated += 1
                else:
                    skipped += 1
            elif len(valid) > 1:
                ambiguous += 1
            else:
                skipped += 1
        con.commit()
    return {"ok": True, "updated": updated, "skipped": skipped, "ambiguous_multi_project": ambiguous}


@app.post("/api/events/bulk-import")
async def bulk_import(req: Request) -> dict[str, Any]:
    """Import a list of events (used for localStorage migration)."""
    payload = await req.json()
    items = payload.get("events", [])
    inserted = 0
    for ev in items:
        try:
            upsert_event(ev)
            inserted += 1
        except HTTPException:
            continue
    return {"inserted": inserted, "total": len(items)}

# ---------- Projects ----------
@app.get("/api/projects")
def list_projects() -> dict[str, Any]:
    with db_conn() as con:
        custom_rows = con.execute(
            "SELECT slug, name, short_name, color FROM custom_projects ORDER BY name"
        ).fetchall()
        colors = project_color_map(con)
    custom_by_slug = {r["slug"]: dict(r) for r in custom_rows}
    pms = discover_pms_projects()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # PM folder list is the canonical source. Enrich each with custom_projects
    # row (long name / short_name / color) when slugs match.
    for p in pms:
        slug = p["slug"]
        seen.add(slug)
        cust = custom_by_slug.get(slug, {})
        out.append({
            "slug": slug,
            "name": cust.get("name") or p["name"],
            "short_name": cust.get("short_name") or "",
            "source": "pms" if not cust else "pms+custom",
            "color": (cust.get("color") if cust else None) or colors.get(slug) or assign_default_color(slug, colors),
        })
    # Pure custom projects (no matching PM folder) — kept for legacy/orphans.
    for r in custom_rows:
        if r["slug"] in seen:
            continue
        out.append({
            "slug": r["slug"],
            "name": r["name"],
            "short_name": r["short_name"] or "",
            "source": "custom",
            "color": r["color"] or colors.get(r["slug"]) or assign_default_color(r["slug"], colors),
        })
    return {"projects": out}

@app.post("/api/projects/custom")
async def add_custom_project(req: Request,
                             _user: dict = Depends(auth_helpers.require_admin)) -> dict[str, Any]:
    payload = await req.json()
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    short_name = (payload.get("short_name") or "").strip() or None
    slug = payload.get("slug") or _slugify(short_name or name)
    color = payload.get("color") or DEFAULT_PROJECT_PALETTE[0]
    with db_conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO custom_projects (slug, name, short_name, color, created_at)"
            " VALUES (?,?,?,?,?)",
            (slug, name, short_name, color, now_iso()),
        )
    return {"slug": slug, "name": name, "short_name": short_name or "",
            "color": color, "source": "custom"}

@app.patch("/api/projects/custom/{slug}")
async def rename_custom_project(slug: str, req: Request,
                                _user: dict = Depends(auth_helpers.require_admin)) -> dict[str, Any]:
    """Rename a custom project (long name and/or short_name). Also syncs animals.project."""
    payload = await req.json()
    new_name = (payload.get("name") or "").strip()
    new_short = payload.get("short_name")
    if not new_name and new_short is None:
        raise HTTPException(400, "name or short_name required")
    with db_conn() as con:
        row = con.execute(
            "SELECT name, short_name FROM custom_projects WHERE slug=?", (slug,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Project not found")
        old_name = row["name"]
        sets, vals = [], []
        if new_name:
            sets.append("name=?"); vals.append(new_name)
        if new_short is not None:
            sets.append("short_name=?"); vals.append((new_short or "").strip() or None)
        vals.append(slug)
        con.execute(f"UPDATE custom_projects SET {', '.join(sets)} WHERE slug=?", vals)
        # Sync animals.project — only if the long name actually changed
        animals_updated = (animals_dal.rename_project(con, old_name, new_name)
                           if new_name and new_name != old_name else 0)
    # Sync Excel Lists sheet
    excel_updated = False
    try:
        cfg = {}
        with db_conn() as con:
            for row in con.execute("SELECT key,value FROM animal_config").fetchall():
                cfg[row["key"]] = row["value"]
        xl_path = cfg.get("excel_path", "")
        if xl_path and Path(xl_path).exists():
            import openpyxl
            wb = openpyxl.load_workbook(xl_path)
            if "Lists" in wb.sheetnames:
                ws = wb["Lists"]
                for row in ws.iter_rows():
                    for cell in row:
                        if cell.value == old_name:
                            cell.value = new_name
                            excel_updated = True
                wb.save(xl_path)
    except Exception as e:
        print(f"Excel sync warning: {e}")
    return {"slug": slug, "name": new_name or old_name, "old_name": old_name,
            "short_name": payload.get("short_name"),
            "animals_updated": animals_updated, "excel_updated": excel_updated}

@app.delete("/api/projects/custom/{slug}")
def delete_custom_project(slug: str,
                          _user: dict = Depends(auth_helpers.require_admin)) -> dict[str, Any]:
    with db_conn() as con:
        cur = con.execute("DELETE FROM custom_projects WHERE slug=?", (slug,))
    if cur.rowcount == 0:
        raise HTTPException(404, "Not found")
    return {"ok": True}

@app.post("/api/projects/{slug}/color")
async def set_project_color(slug: str, req: Request) -> dict[str, Any]:
    payload = await req.json()
    color = payload.get("color")
    if not color:
        raise HTTPException(400, "color required")
    with db_conn() as con:
        # If it's a custom project, update there too
        custom = con.execute("SELECT slug FROM custom_projects WHERE slug=?", (slug,)).fetchone()
        if custom:
            con.execute("UPDATE custom_projects SET color=? WHERE slug=?", (color, slug))
        con.execute(
            "INSERT OR REPLACE INTO project_colors (slug, color) VALUES (?,?)",
            (slug, color),
        )
    return {"slug": slug, "color": color}

# ---------- Team members ----------
@app.get("/api/team")
def list_team() -> dict[str, Any]:
    """Scheduler's team roster. Includes `eln_user_id` if linked to an ELN user."""
    with db_conn() as con:
        rows = users_dal.list_team(con)
    return {"members": [dict(r) for r in rows]}

@app.get("/api/team/unified")
def list_team_unified() -> dict[str, Any]:
    """Unified roster — team_members + ELN-only users in one list."""
    with db_conn() as con:
        return {"members": users_dal.list_unified(con)}

@app.get("/api/team/resolve/{ident}")
def resolve_team_member(ident: str) -> dict[str, Any]:
    """Resolve any identifier (team_id, eln_user_id, username, name) → unified user."""
    with db_conn() as con:
        u = users_dal.resolve(con, ident)
    if not u:
        raise HTTPException(404, "User not found")
    return u

@app.post("/api/team")
async def add_member(req: Request) -> dict[str, Any]:
    payload = await req.json()
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    mid = _uid()
    with db_conn() as con:
        users_dal.insert_team(
            con,
            team_id=mid,
            name=name,
            role=payload.get("role") or None,
            color=payload.get("color") or "#5AC8FA",
            eln_user_id=(payload.get("eln_user_id") or None),
        )
    return {
        "id": mid, "name": name,
        "role":  payload.get("role")  or "",
        "color": payload.get("color") or "#5AC8FA",
        "eln_user_id": payload.get("eln_user_id"),
    }

@app.put("/api/team/{mid}")
async def update_member(mid: str, req: Request) -> dict[str, Any]:
    payload = await req.json()
    fields = {k: payload[k] for k in ("name", "role", "color", "eln_user_id") if k in payload}
    if not fields:
        raise HTTPException(400, "Nothing to update")
    with db_conn() as con:
        if not users_dal.update_team(con, mid, **fields):
            raise HTTPException(404, "Not found")
        row = users_dal.get_team(con, mid)
    return dict(row)

@app.delete("/api/team/{mid}")
def delete_member(mid: str) -> dict[str, Any]:
    with db_conn() as con:
        if not users_dal.delete_team(con, mid):
            raise HTTPException(404, "Not found")
    return {"ok": True}

# ---------- Customs (categories / subcategories) ----------
@app.get("/api/customs")
def list_customs() -> dict[str, Any]:
    with db_conn() as con:
        cats = con.execute("SELECT key, label, color FROM custom_categories ORDER BY label").fetchall()
        subs = con.execute("SELECT category_key, label FROM custom_subcategories ORDER BY category_key, label").fetchall()
    sub_map: dict[str, list[str]] = {}
    for s in subs:
        sub_map.setdefault(s["category_key"], []).append(s["label"])
    return {
        "categories": [{"key": r["key"], "label": r["label"], "color": r["color"]} for r in cats],
        "subcategories": sub_map,
    }

@app.post("/api/customs/categories")
async def add_custom_category(req: Request) -> dict[str, Any]:
    payload = await req.json()
    key = payload.get("key") or _slugify(payload.get("label", ""))
    label = payload.get("label")
    if not key or not label:
        raise HTTPException(400, "key and label required")
    with db_conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO custom_categories (key, label, color) VALUES (?,?,?)",
            (key, label, payload.get("color")),
        )
    return {"key": key, "label": label, "color": payload.get("color")}

@app.post("/api/customs/subcategories")
async def add_custom_subcategory(req: Request) -> dict[str, Any]:
    payload = await req.json()
    cat = payload.get("category_key")
    label = payload.get("label")
    if not cat or not label:
        raise HTTPException(400, "category_key and label required")
    with db_conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO custom_subcategories (category_key, label) VALUES (?,?)",
            (cat, label),
        )
    return {"ok": True}

# ---------- Lab Notes ----------
def row_to_note(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id":           r["id"],
        "project_slug": r["project_slug"] or "",
        "title":        r["title"] or "Untitled",
        "content":      r["content"] or "",
        "tags":         json.loads(r["tags"]) if r["tags"] else [],
        "note_date":    r["note_date"],
        "event_ids":    json.loads(r["event_ids"]) if r["event_ids"] else [],
        "mouse_ids":    json.loads(r["mouse_ids"]) if r["mouse_ids"] else [],
        "created_at":   r["created_at"],
        "updated_at":   r["updated_at"],
    }

@app.get("/api/notes")
def list_notes(project: Optional[str] = None) -> dict[str, Any]:
    with db_conn() as con:
        if project:
            rows = con.execute(
                "SELECT * FROM lab_notes WHERE project_slug=? ORDER BY note_date DESC, updated_at DESC",
                (project,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM lab_notes ORDER BY note_date DESC, updated_at DESC"
            ).fetchall()
    return {"notes": [row_to_note(r) for r in rows]}

@app.post("/api/notes")
async def create_note(req: Request) -> dict[str, Any]:
    payload = await req.json()
    nid = payload.get("id") or _uid()
    now = now_iso()
    note_date = payload.get("note_date") or now[:10]
    title = (payload.get("title") or "Untitled").strip()
    with db_conn() as con:
        con.execute("""
            INSERT INTO lab_notes (id, project_slug, title, content, tags, note_date,
                                   event_ids, mouse_ids, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            nid,
            payload.get("project_slug") or None,
            title,
            payload.get("content") or "",
            json.dumps(payload.get("tags") or []),
            note_date,
            json.dumps(payload.get("event_ids") or []),
            json.dumps(payload.get("mouse_ids") or []),
            now, now,
        ))
        row = con.execute("SELECT * FROM lab_notes WHERE id=?", (nid,)).fetchone()
    return row_to_note(row)

@app.put("/api/notes/{note_id}")
async def update_note(note_id: str, req: Request) -> dict[str, Any]:
    payload = await req.json()
    now = now_iso()
    with db_conn() as con:
        existing = con.execute("SELECT id FROM lab_notes WHERE id=?", (note_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Note not found")
        con.execute("""
            UPDATE lab_notes SET
              project_slug=?, title=?, content=?, tags=?, note_date=?,
              event_ids=?, mouse_ids=?, updated_at=?
            WHERE id=?
        """, (
            payload.get("project_slug") or None,
            (payload.get("title") or "Untitled").strip(),
            payload.get("content") or "",
            json.dumps(payload.get("tags") or []),
            payload.get("note_date") or now[:10],
            json.dumps(payload.get("event_ids") or []),
            json.dumps(payload.get("mouse_ids") or []),
            now,
            note_id,
        ))
        row = con.execute("SELECT * FROM lab_notes WHERE id=?", (note_id,)).fetchone()
    return row_to_note(row)

@app.delete("/api/notes/{note_id}")
def delete_note(note_id: str) -> dict[str, Any]:
    with db_conn() as con:
        cur = con.execute("DELETE FROM lab_notes WHERE id=?", (note_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, "Note not found")
    return {"ok": True}

# ---------- Animals ----------
import math as _math

def _safe_str(v: Any) -> Optional[str]:
    if v is None: return None
    try:
        if isinstance(v, float) and _math.isnan(v): return None
        if isinstance(v, float) and v.is_integer(): return str(int(v))
    except TypeError: pass
    s = str(v).strip()
    return s if s not in ('', 'nan', 'NaT', 'None') else None

def _safe_date(v: Any) -> Optional[str]:
    s = _safe_str(v)
    return s[:10] if s else None

def _sync_excel(path: str) -> dict:
    try:
        import pandas as pd
    except ImportError:
        raise HTTPException(500, "pandas not installed — run: pip install pandas openpyxl")
    if not Path(path).exists():
        raise HTTPException(404, f"File not found: {path}")
    try:
        df = pd.read_excel(path, sheet_name="Sheet1", header=0)
    except Exception as e:
        raise HTTPException(500, f"Failed to read Excel: {e}")
    now = now_iso()
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        mid = _safe_str(row.get("Mice ID"))
        if not mid:
            continue
        rows.append({
            "mouse_id":        mid,
            "project":         _safe_str(row.get("Project")),
            "condition":       _safe_str(row.get("Condition")),
            "experiment":      _safe_str(row.get("Exp")),
            "cohort":          _safe_str(row.get("Cohort")),
            "strain":          _safe_str(row.get("Strain")),
            "dob":             _safe_date(row.get("DOB")),
            "sex":             _safe_str(row.get("Sex")),
            "dos":             _safe_date(row.get("DOS")),
            "date_cond1":      _safe_date(row.get("Date in cond-1")),
            "date_cond2":      _safe_date(row.get("Date in cond-2")),
            "date_cond3":      _safe_date(row.get("Date in cond-3")),
            "days_cond1":      _safe_str(row.get("Days in cond-1")),
            "days_cond2":      _safe_str(row.get("Days in cond-2")),
            "days_cond3":      _safe_str(row.get("Days in cond-3")),
            "sacrificed":      _safe_str(row.get("Sacrified (Y/N)")),
            "sacrificed_date": _safe_date(row.get("Sacrified date")),
            "notes":           _safe_str(row.get("Note")),
            "barcode":         _safe_str(row.get("Barcode")),
            "synced_at":       now,
        })
    with db_conn() as con:
        # Validating, non-destructive, audited sync (no more DELETE-all).
        report = animal_sync.run_sync(con, rows, source="excel_sync")
        con.execute("INSERT OR REPLACE INTO animal_config (key,value) VALUES ('excel_path',?)", (path,))
        con.commit()
    return report

_ANIMAL_EXCEL_COLUMNS = {
    "mouse_id": "Mice ID",
    "project": "Project",
    "condition": "Condition",
    "experiment": "Exp",
    "cohort": "Cohort",
    "strain": "Strain",
    "dob": "DOB",
    "sex": "Sex",
    "dos": "DOS",
    "date_cond1": "Date in cond-1",
    "date_cond2": "Date in cond-2",
    "date_cond3": "Date in cond-3",
    "sacrificed": "Sacrified (Y/N)",
    "sacrificed_date": "Sacrified date",
    "notes": "Note",
    "barcode": "Barcode",
}


def _parse_sheet_date(v: Any) -> Optional[str]:
    """Normalize a Google-Sheet date cell to YYYY-MM-DD. Handles ISO and
    US M/D/YYYY; ambiguous numeric forms default to US (MM/DD) per repo rules."""
    s = _safe_str(v)
    if not s:
        return None
    s = s.split(" ")[0].strip()
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$", s)
    if m:
        a, b, y = (int(x) for x in m.groups())
        mo, d = (b, a) if a > 12 and b <= 12 else (a, b)
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return s[:10]


def _norm_sheet_header_name(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(v or "").strip().lower()).strip()


_ANIMAL_SHEET_READ_ALIASES = {
    "mouse_id":        ("Mice ID", "Mouse ID", "Subject"),
    "project":         ("Project",),
    "condition":       ("Condition", "Current condition"),
    "experiment":      ("Exp", "Experiment"),
    "cohort":          ("Cohort",),
    "strain":          ("Strain",),
    "dob":             ("DOB",),
    "sex":             ("Sex",),
    "dos":             ("DOS",),
    "date_cond1":      ("Date in cond-1", "Date of transfer 1", "Date of\ntransfer 1"),
    "date_cond2":      ("Date in cond-2", "Date of transfer 2", "Date of\ntransfer 2"),
    "date_cond3":      ("Date in cond-3", "Date of transfer 3", "Date of\ntransfer 3"),
    "days_cond1":      ("Days in cond-1", "Age at transfer 1", "Age at\ntransfer 1"),
    "days_cond2":      ("Days in cond-2", "Age at transfer 2", "Age at\ntransfer 2"),
    "days_cond3":      ("Days in cond-3", "Age at transfer 3", "Age at\ntransfer 3"),
    "sacrificed":      ("Sacrified (Y/N)", "Sacrificed (Y/N)", "Sacrificed"),
    "sacrificed_date": ("Sacrified date", "Sacrificed date"),
    "notes":           ("Note", "Notes"),
    "barcode":         ("Barcode", "Cage ID", "Cage / barcode"),
}


def _sheet_header_index(headers: list[str], names: tuple[str, ...]) -> Optional[int]:
    normalized = {_norm_sheet_header_name(h): i for i, h in enumerate(headers)}
    for name in names:
        idx = normalized.get(_norm_sheet_header_name(name))
        if idx is not None:
            return idx
    return None


def _sheet_cell(rowvals: list, headers: list[str], field: str) -> Any:
    idx = _sheet_header_index(headers, _ANIMAL_SHEET_READ_ALIASES[field])
    return rowvals[idx] if idx is not None and idx < len(rowvals) else None




def _configured_animal_excel_path() -> str:
    with db_conn() as con:
        row = con.execute("SELECT value FROM animal_config WHERE key='excel_path'").fetchone()
    return (row["value"] if row else "") or ""

def _write_animal_to_excel(mouse_id: str, fields: dict[str, Any]) -> bool:
    path = _configured_animal_excel_path()
    if not path or not Path(path).exists():
        return False
    try:
        from openpyxl import load_workbook
    except ImportError:
        return False

    wb = load_workbook(path)
    ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.active
    headers = {
        str(cell.value).strip(): idx
        for idx, cell in enumerate(ws[1], start=1)
        if cell.value is not None and str(cell.value).strip()
    }

    mid_col = headers.get(_ANIMAL_EXCEL_COLUMNS["mouse_id"])
    if not mid_col:
        return False

    row_idx = None
    for idx in range(2, ws.max_row + 1):
        if _safe_str(ws.cell(idx, mid_col).value) == mouse_id:
            row_idx = idx
            break
    if row_idx is None:
        row_idx = ws.max_row + 1
        ws.cell(row_idx, mid_col).value = mouse_id

    for key, value in fields.items():
        excel_col = _ANIMAL_EXCEL_COLUMNS.get(key)
        col_idx = headers.get(excel_col or "")
        if col_idx:
            ws.cell(row_idx, col_idx).value = value

    wb.save(path)
    return True

@app.get("/api/animals")
def list_animals(include_removed: bool = False) -> dict[str, Any]:
    where = "" if include_removed else "WHERE COALESCE(removed,0)=0"
    with db_conn() as con:
        rows = con.execute(
            f"SELECT * FROM animal_with_state {where} ORDER BY project, mouse_id"
        ).fetchall()
    return {"animals": [dict(r) for r in rows]}

@app.get("/api/animals/cond-labels")
def get_cond_labels() -> dict:
    with db_conn() as con:
        rows = con.execute("SELECT condition, slot, label FROM colony_cond_labels").fetchall()
    result: dict = {}
    for r in rows:
        result.setdefault(r["condition"], {})[r["slot"]] = r["label"]
    return {"labels": result}

@app.put("/api/animals/cond-labels")
async def upsert_cond_label(req: Request,
                            _user: dict = Depends(auth_helpers.require_colony_editor)) -> dict:
    body = await req.json()
    condition = (body.get("condition") or "").strip()
    slot = (body.get("slot") or "").strip()
    label = (body.get("label") or "").strip()
    if not condition or slot not in ("c1", "c2", "c3"):
        raise HTTPException(400, "condition and slot (c1/c2/c3) required")
    with db_conn() as con:
        if label:
            con.execute(
                "INSERT OR REPLACE INTO colony_cond_labels(condition,slot,label) VALUES(?,?,?)",
                (condition, slot, label)
            )
        else:
            con.execute(
                "DELETE FROM colony_cond_labels WHERE condition=? AND slot=?",
                (condition, slot)
            )
        con.commit()
    return {"ok": True}

@app.get("/api/animals/sync-status")
def animal_sync_status() -> dict[str, Any]:
    with db_conn() as con:
        cfg = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM animal_config").fetchall()}
        count = animals_dal.count(con)
        latest = con.execute("SELECT report FROM animal_sync_runs ORDER BY ts DESC LIMIT 1").fetchone()
    sheet_id = cfg.get("sheet_id", "")
    return {"excel_path": cfg.get("excel_path", ""),
            "sheet_id": sheet_id, "sheet_name": cfg.get("sheet_name", ""),
            "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit" if sheet_id else "",
            "last_synced": cfg.get("last_synced", ""), "count": count,
            "can_sync": str(cfg.get("sheet_import_enabled", "1")).lower() not in {"0", "false", "no", "off"},
            "last_report": json.loads(latest["report"]) if latest else None}

@app.get("/api/animals/sync-history")
def animal_sync_history(limit: int = 20) -> dict[str, Any]:
    with db_conn() as con:
        rows = con.execute("SELECT report FROM animal_sync_runs ORDER BY ts DESC LIMIT ?",
                           (max(1, min(50, limit)),)).fetchall()
        reports = []
        for row in rows:
            report = json.loads(row["report"])
            report["changes"] = [dict(r) for r in con.execute(
                "SELECT mouse_id, change_type, field, old_value, new_value FROM animal_audit "
                "WHERE sync_id=? ORDER BY id LIMIT 200", (report["sync_id"],)).fetchall()]
            reports.append(report)
    return {"reports": reports}

@app.post("/api/animals/config")
async def set_animal_config(req: Request,
                            _user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    payload = await req.json()
    sheet_id = (payload.get("sheet_id") or "").strip()
    sheet_name = (payload.get("sheet_name") or "").strip()
    path = (payload.get("excel_path") or "").strip()
    if not (sheet_id or path):
        raise HTTPException(400, "sheet_id or excel_path required")
    with db_conn() as con:
        if sheet_id:
            con.execute("INSERT OR REPLACE INTO animal_config (key,value) VALUES ('sheet_id',?)", (sheet_id,))
            con.execute("INSERT OR REPLACE INTO animal_config (key,value) VALUES ('sheet_name',?)", (sheet_name or _DEFAULT_ANIMAL_SHEET_TAB,))
        if path:
            con.execute("INSERT OR REPLACE INTO animal_config (key,value) VALUES ('excel_path',?)", (path,))
    return {"ok": True, "sheet_id": sheet_id, "sheet_name": sheet_name or _DEFAULT_ANIMAL_SHEET_TAB, "excel_path": path}

@app.post("/api/animals/sync")
async def sync_animals(req: Request,
                       _user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    try: payload = await req.json()
    except: payload = {}
    with db_conn() as con:
        cfg = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM animal_config").fetchall()}
    # Sheet-as-master: Google Sheet import is enabled by default. Set
    # animal_config 'sheet_import_enabled'='0' only for emergency freeze/manual DB
    # work. The safe-sync path is validating, audited, and soft-removes vanished
    # animals instead of hard-deleting them.
    if str(cfg.get("sheet_import_enabled", "1")).lower() in {"0", "false", "no", "off"}:
        raise HTTPException(409,
            "Sheet import is disabled for this animal DB. Set animal_config "
            "'sheet_import_enabled'='1' to re-enable Google Sheet sync.")
    path = (payload.get("excel_path") or cfg.get("excel_path") or "").strip()
    if not path:
        raise HTTPException(400, "No sheet_id or excel_path configured — call POST /api/animals/config first")
    report = _sync_excel(path)
    return _sync_response(report)


def _sync_response(report: dict) -> dict:
    """Wrap a safe-sync report with the legacy keys the frontend reads
    (`count`, `last_synced`) so existing callers keep working."""
    with db_conn() as con:
        count = animals_dal.count(con)
    return {"ok": True, "count": count, "last_synced": report.get("ts"),
            "source": report.get("source"), "report": report}

_ANIMAL_EDITABLE = frozenset({
    "project", "condition", "cohort", "strain", "dob", "sex",
    "date_cond1", "date_cond2", "date_cond3",
    "dos", "sacrificed", "sacrificed_date", "experiment", "notes", "barcode",
})


# ---------- Cage-card scan: photo → GPT vision → colony ----------
# Mobile flow: photo → GPT-4o-mini vision → confirm → write. Production writes to
# the Google-Sheet master then syncs (the PRIVATE blocks below); the generic build
# writes straight to the DB.

_CAGE_CARD_FIELDS = _ANIMAL_EDITABLE | {"mouse_id"}


def _write_animals_to_db(animals: list[dict]) -> dict:
    """Generic colony write — upsert each animal straight into the DB.
    Used when there is no Google-Sheet master (e.g. the open-source build)."""
    written, errors = [], []
    with db_conn() as con:
        for a in animals:
            mid = str(a.get("mouse_id") or "").strip()
            if not mid:
                errors.append({"mouse_id": "", "error": "missing mouse_id"})
                continue
            fields = {k: v for k, v in a.items()
                      if k in _ANIMAL_EDITABLE and v not in (None, "")}
            if fields.get("strain"):
                fields["strain"] = animal_sync.canon_strain(fields["strain"])
            try:
                if animals_dal.exists(con, mid, case_insensitive=False):
                    animals_dal.update(con, mid, **fields)
                    written.append({"mouse_id": mid, "action": "updated"})
                else:
                    animals_dal.insert(con, mid, **fields)
                    written.append({"mouse_id": mid, "action": "created"})
            except Exception as e:
                errors.append({"mouse_id": mid, "error": str(e)})
    return {"written": written, "errors": errors}




class _CageExtractIn(BaseModel):
    image_b64: str
    mime: str = "image/jpeg"


@app.post("/api/animals/cage-card/extract")
def cage_card_extract(payload: _CageExtractIn,
                      _user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    """Cage-card photo (base64) → GPT-4o-mini vision → editable animal dicts.
    No write happens here — the client confirms before committing."""
    if not payload.image_b64:
        raise HTTPException(400, "image_b64 required")
    try:
        import animal_updater
    except Exception as e:
        raise HTTPException(500, f"vision module unavailable: {e}")
    animals = animal_updater.parse_cage_card_b64(payload.image_b64, payload.mime or "image/jpeg")
    if animals is None:
        raise HTTPException(503, "Cage-card AI not available — set OPENAI_API_KEY on the server, "
                                 "or the vision call failed (check server logs).")
    return {"ok": True, "animals": animals, "model": "gpt-4o-mini"}


@app.post("/api/animals/cage-card/commit")
async def cage_card_commit(req: Request,
                           _user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    """Write confirmed cage-card animals to the colony. Production writes to the
    Google-Sheet master and syncs the DB; the generic build writes to the DB
    directly. Strain aliases are canonicalised on the production sync path."""
    payload = await req.json()
    raw = payload.get("animals") or []
    clean = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        keep = {k: v for k, v in a.items() if k in _CAGE_CARD_FIELDS}
        if str(keep.get("mouse_id") or "").strip():
            clean.append(keep)
    if not clean:
        raise HTTPException(400, "No animals with a mouse_id to write.")
    result, sync = None, None
    if result is None:                       # generic build: no Sheet master
        result = _write_animals_to_db(clean)
    return {"ok": True, **result, "sync": sync}


# ---------- DB → Excel writeback (manual, separate from Excel→DB sync) ----------

def _norm_for_compare(v: Any) -> str:
    """Normalize a value for diff comparison: None/empty → '', date-ish → YYYY-MM-DD."""
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    if len(s) >= 10 and s[4] == '-' and s[7] == '-':
        return s[:10]
    return s


def _compute_excel_diff() -> dict[str, Any]:
    """Compare DB animals against Excel and return what would change on push.
    Does NOT modify the Excel file. Used to preview before _apply_excel_push()."""
    path = _configured_animal_excel_path()
    if not path or not Path(path).exists():
        raise HTTPException(400, "Excel path not configured or file not found")
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise HTTPException(500, "openpyxl not installed")

    wb = load_workbook(path, data_only=True)
    ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.active
    headers = {
        str(cell.value).strip(): idx
        for idx, cell in enumerate(ws[1], start=1)
        if cell.value is not None and str(cell.value).strip()
    }
    mid_col = headers.get(_ANIMAL_EXCEL_COLUMNS["mouse_id"])
    if not mid_col:
        raise HTTPException(500, "Mouse ID column not found in Excel")

    excel_state: dict[str, dict[str, Any]] = {}
    for idx in range(2, ws.max_row + 1):
        mid = _safe_str(ws.cell(idx, mid_col).value).strip()
        if not mid:
            continue
        row = {"_row_idx": idx}
        for db_key, ex_col in _ANIMAL_EXCEL_COLUMNS.items():
            cidx = headers.get(ex_col)
            if cidx:
                row[db_key] = _safe_str(ws.cell(idx, cidx).value)
        excel_state[mid] = row

    with db_conn() as con:
        db_rows = [dict(r) for r in animals_dal.list_all(con)]

    adds: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    seen_db: set[str] = set()
    duplicates: list[str] = []
    compared_fields = [k for k in _ANIMAL_EXCEL_COLUMNS if k != "mouse_id"]

    for db_row in db_rows:
        mid = (db_row.get("mouse_id") or "").strip()
        if not mid:
            continue
        if mid in seen_db:
            duplicates.append(mid)
            continue
        seen_db.add(mid)
        if mid not in excel_state:
            adds.append({"mouse_id": mid,
                         "preview": {k: db_row.get(k) for k in compared_fields if db_row.get(k) not in (None, "")}})
            continue
        changes = []
        for k in compared_fields:
            db_v = _norm_for_compare(db_row.get(k))
            xl_v = _norm_for_compare(excel_state[mid].get(k))
            if db_v != xl_v:
                changes.append({"field": k, "db": db_v, "excel": xl_v})
        if changes:
            updates.append({"mouse_id": mid, "row": excel_state[mid]["_row_idx"], "changes": changes})

    excel_only = sorted(set(excel_state.keys()) - seen_db)
    return {
        "excel_path": path,
        "summary": {"adds": len(adds), "updates": len(updates),
                    "excel_only": len(excel_only), "duplicates_in_db": len(duplicates)},
        "adds": adds, "updates": updates,
        "excel_only_preview": excel_only[:20],
        "duplicates_in_db": duplicates,
    }


def _apply_excel_push(allow_excel_only: bool = False) -> dict[str, Any]:
    """Apply DB → Excel writeback: update existing rows + append new ones.
    `allow_excel_only=False` means Excel-only rows (in Excel but not DB) are left untouched."""
    path = _configured_animal_excel_path()
    if not path or not Path(path).exists():
        raise HTTPException(400, "Excel path not configured or file not found")
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise HTTPException(500, "openpyxl not installed")

    wb = load_workbook(path)
    ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.active
    headers = {
        str(cell.value).strip(): idx
        for idx, cell in enumerate(ws[1], start=1)
        if cell.value is not None and str(cell.value).strip()
    }
    mid_col = headers.get(_ANIMAL_EXCEL_COLUMNS["mouse_id"])
    if not mid_col:
        raise HTTPException(500, "Mouse ID column not found in Excel")

    mid_to_row: dict[str, int] = {}
    for idx in range(2, ws.max_row + 1):
        mid = _safe_str(ws.cell(idx, mid_col).value).strip()
        if mid:
            mid_to_row[mid] = idx

    with db_conn() as con:
        db_rows = [dict(r) for r in animals_dal.list_all(con)]

    updated, added, fields_changed = 0, 0, 0
    seen: set[str] = set()
    for db_row in db_rows:
        mid = (db_row.get("mouse_id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        target = mid_to_row.get(mid)
        is_new = target is None
        if is_new:
            target = ws.max_row + 1
            ws.cell(target, mid_col).value = mid
            added += 1
        else:
            updated += 1
        for db_key, ex_col in _ANIMAL_EXCEL_COLUMNS.items():
            if db_key == "mouse_id":
                continue
            cidx = headers.get(ex_col)
            if not cidx:
                continue
            new_val = db_row.get(db_key)
            cur = ws.cell(target, cidx).value
            if _norm_for_compare(new_val) != _norm_for_compare(cur):
                ws.cell(target, cidx).value = new_val if new_val not in (None, "") else None
                fields_changed += 1

    wb.save(path)
    return {"ok": True, "updated_rows": updated, "added_rows": added,
            "fields_changed": fields_changed, "excel_path": path}


@app.get("/api/animals/excel-diff")
def animals_excel_diff() -> dict[str, Any]:
    return _compute_excel_diff()


@app.post("/api/animals/excel-push")
def animals_excel_push(_user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    return _apply_excel_push()

def _save_animals_source(rows: list[dict]) -> bool:
    with db_conn() as con:
        cfg = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM animal_config")}
    if not cfg.get("sheet_id") or str(cfg.get("sheet_import_enabled", "1")).lower() in {"0", "false", "no", "off"}:
        return False
    writer = globals().get("_write_animals_to_sheet")
    if not writer:
        raise HTTPException(503, "This installation cannot write to the configured Sheet.")
    result = writer(rows)
    if result.get("errors") or len(result.get("written", [])) != len(rows):
        raise HTTPException(502, "Source Sheet save was incomplete. Synchronize animals to reconcile before retrying.")
    return True

def _save_animal_source(mouse_id: str, fields: dict) -> bool:
    return _save_animals_source([{"mouse_id": mouse_id, **fields}])

def require_animal_removal_source() -> None:
    with db_conn() as con:
        cfg = {r["key"]:r["value"] for r in con.execute("SELECT key,value FROM animal_config")}
    if cfg.get("sheet_id") and str(cfg.get("sheet_import_enabled", "1")).lower() not in {"0", "false", "no", "off"}:
        raise HTTPException(409, "Archive or remove this animal in the source Sheet, then synchronize animals. A cache-only change would be undone by the next sync.")

@app.post("/api/animals")
async def create_animal(req: Request,
                        _user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    payload = await req.json()
    mouse_id = (payload.get("mouse_id") or "").strip()
    if not mouse_id:
        raise HTTPException(400, "mouse_id required")
    fields = {k: v for k, v in payload.items() if k in _ANIMAL_EDITABLE}
    with db_conn() as con:
        if animals_dal.exists(con, mouse_id, case_insensitive=False):
            raise HTTPException(409, f"Mouse '{mouse_id}' already exists")
        sheet_written = _save_animal_source(mouse_id, fields)
        animals_dal.insert(con, mouse_id, **fields)
        row = con.execute("SELECT * FROM animal_with_state WHERE mouse_id=?", (mouse_id,)).fetchone()
    if not sheet_written:
        try:
            _write_animal_to_excel(mouse_id, {"mouse_id": mouse_id, **fields})
        except Exception as e:
            print(f"Animal Excel write skipped for {mouse_id}: {e}")
    return dict(row)

@app.put("/api/animals/{mouse_id}")
async def update_animal(mouse_id: str, req: Request,
                        _user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    payload = await req.json()
    fields = {k: v for k, v in payload.items() if k in _ANIMAL_EDITABLE}
    if not fields:
        raise HTTPException(400, "No editable fields provided")
    with db_conn() as con:
        if not animals_dal.exists(con, mouse_id, case_insensitive=False):
            raise HTTPException(404, f"Mouse '{mouse_id}' not found")
        sheet_written = _save_animal_source(mouse_id, fields)
        animals_dal.update(con, mouse_id, **fields)
        row = con.execute("SELECT * FROM animal_with_state WHERE mouse_id=?", (mouse_id,)).fetchone()
    if not sheet_written:
        try:
            _write_animal_to_excel(mouse_id, fields)
        except Exception as e:
            print(f"Animal Excel write skipped for {mouse_id}: {e}")
    return dict(row)

@app.patch("/api/animals/bulk")
async def bulk_update_animals(req: Request,
                              _user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    payload = await req.json()
    mouse_ids = [
        str(mid).strip()
        for mid in (payload.get("mouse_ids") or [])
        if str(mid).strip()
    ]
    fields = {
        k: v for k, v in (payload.get("fields") or {}).items()
        if k in _ANIMAL_EDITABLE
    }
    if not mouse_ids:
        raise HTTPException(400, "mouse_ids required")
    if not fields:
        raise HTTPException(400, "No editable fields provided")

    updated: list[str] = []
    with db_conn() as con:
        targets = [mid for mid in mouse_ids if animals_dal.exists(con, mid, case_insensitive=False)]
        sheet_written = _save_animals_source([{"mouse_id": mid, **fields} for mid in targets]) if targets else False
        for mid in mouse_ids:
            if animals_dal.update(con, mid, **fields):
                updated.append(mid)

    sheet_updated = len(updated) if sheet_written else 0
    if not sheet_updated:
        for mid in updated:
            try:
                _write_animal_to_excel(mid, fields)
            except Exception as e:
                print(f"Animal Excel write skipped for {mid}: {e}")

    return {
        "ok": True,
        "requested": len(mouse_ids),
        "updated": len(updated),
        "sheet_updated": sheet_updated,
    }

@app.delete("/api/animals/{mouse_id}")
def delete_animal(mouse_id: str,
                  _user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    require_animal_removal_source()
    with db_conn() as con:
        if not animals_dal.delete(con, mouse_id):
            raise HTTPException(404, f"Mouse '{mouse_id}' not found")
    return {"ok": True, "mouse_id": mouse_id}

@app.post("/api/animals/assign")
async def assign_animals(req: Request,
                         user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    """Bulk-set animals.assigned_to. Empty/null to_user_id = unassign.
    Logs each non-no-op change to eln_mouse_transfers."""
    import uuid as _uuid, json as _json
    payload = await req.json()
    mouse_ids = [str(m).strip() for m in (payload.get("mouse_ids") or []) if str(m).strip()]
    to_uid    = payload.get("to_user_id") or None
    note      = (payload.get("note") or "").strip() or None
    if not mouse_ids:
        raise HTTPException(400, "mouse_ids required")

    with db_conn() as con:
        con.row_factory = sqlite3.Row
        # Recipient must exist if not null
        if to_uid is not None:
            row = con.execute(
                "SELECT id FROM eln_users WHERE id = ? AND active = 1", (to_uid,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Recipient user not found")

        assigned, skipped = 0, 0
        now = now_iso()
        for mid in mouse_ids:
            m = animals_dal.get(con, mid, case_insensitive=False)
            if not m:
                skipped += 1
                continue
            prev = m["assigned_to"]
            if prev == to_uid:
                skipped += 1
                continue
            animals_dal.reassign(con, m["mouse_id"], to_uid, case_insensitive=False)
            con.execute(
                """INSERT INTO eln_mouse_transfers
                     (id, mouse_id, from_user_id, to_user_id, transferred_by, note, transferred_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (_uuid.uuid4().hex, m["mouse_id"], prev, to_uid, user["id"], note, now),
            )
            auth_helpers.audit(con, user["id"], "assign_mouse", "mouse", m["mouse_id"],
                               _json.dumps({"from": prev, "to": to_uid, "note": note}))
            assigned += 1
        con.commit()
    return {"ok": True, "assigned": assigned, "skipped": skipped, "to_user_id": to_uid}


@app.post("/api/animals/{mouse_id}/archive")
async def archive_animal(mouse_id: str, req: Request,
                         _user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    """Move animal from active table to archive. Called when sacrificed or sent to ePhys."""
    require_animal_removal_source()
    payload = await req.json()
    reason = (payload.get("reason") or "sacrificed").strip()
    now = now_iso()
    with db_conn() as con:
        row = animals_dal.get(con, mouse_id, case_insensitive=False)
        if not row:
            raise HTTPException(404, f"Mouse '{mouse_id}' not found")
        a = dict(row)
        con.execute("""
            INSERT INTO animal_archive
              (mouse_id, project, condition, experiment, cohort, strain,
               dob, sex, dos, date_cond1, date_cond2, date_cond3,
               sacrificed, sacrificed_date, notes, barcode, synced_at, assigned_to,
              archived_at, archive_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            a["mouse_id"], a.get("project"), a.get("condition"), a.get("experiment"),
            a.get("cohort"), a.get("strain"), a.get("dob"), a.get("sex"), a.get("dos"),
            a.get("date_cond1"), a.get("date_cond2"), a.get("date_cond3"),
            a.get("sacrificed"), a.get("sacrificed_date"),
            a.get("notes"), a.get("barcode"), a.get("synced_at"), a.get("assigned_to"), now, reason,
        ))
        animals_dal.delete(con, mouse_id)
    return {"ok": True, "mouse_id": mouse_id, "archived_at": now, "reason": reason}

@app.post("/api/animals/archive-batch")
async def archive_animals_batch(req: Request,
                                _user: dict = Depends(auth_helpers.require_colony_editor)) -> dict[str, Any]:
    """Move several animals into the archive at once (e.g. all mice on a finalized
    slice-recording note). Marks them sacrificed with the given date. Mice not in
    the active table (already archived / unknown) are skipped, not errored."""
    require_animal_removal_source()
    payload = await req.json()
    mice = payload.get("mouse_ids") or []
    reason = (payload.get("reason") or "sacrificed").strip()
    sdate = (payload.get("sacrificed_date") or "").strip() or None
    now = now_iso()
    archived: list[str] = []
    skipped: list[str] = []
    with db_conn() as con:
        for raw in mice:
            mid = (raw or "").strip()
            if not mid:
                continue
            row = animals_dal.get(con, mid, case_insensitive=True)
            if not row:
                skipped.append(mid)
                continue
            a = dict(row)
            con.execute("""
                INSERT INTO animal_archive
                  (mouse_id, project, condition, experiment, cohort, strain,
                   dob, sex, dos, date_cond1, date_cond2, date_cond3,
                   sacrificed, sacrificed_date, notes, barcode, synced_at,
                   archived_at, archive_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                a["mouse_id"], a.get("project"), a.get("condition"), a.get("experiment"),
                a.get("cohort"), a.get("strain"), a.get("dob"), a.get("sex"), a.get("dos"),
                a.get("date_cond1"), a.get("date_cond2"), a.get("date_cond3"),
                "Y", (sdate or a.get("sacrificed_date")),
                a.get("notes"), a.get("barcode"), a.get("synced_at"),
                now, reason,
            ))
            animals_dal.delete(con, a["mouse_id"])
            archived.append(a["mouse_id"])
    return {"ok": True, "archived": archived, "skipped": skipped, "archived_at": now}

@app.get("/api/animals/archive")
def list_archive(cohort: str = "") -> dict[str, Any]:
    with db_conn() as con:
        if cohort:
            rows = con.execute(
                "SELECT * FROM animal_archive WHERE cohort LIKE ? ORDER BY archived_at DESC",
                (f"%{cohort}%",)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM animal_archive ORDER BY archived_at DESC"
            ).fetchall()
    return {"animals": [dict(r) for r in rows]}

@app.get("/api/animals/cohort-report")
def cohort_report(cohort: str = "") -> dict[str, Any]:
    """Return active + archived animals for a cohort. Used for Slack reports."""
    with db_conn() as con:
        p = (f"%{cohort}%",) if cohort else ()
        w = " WHERE cohort LIKE ?" if cohort else ""
        active   = [dict(r) for r in con.execute(
            f"SELECT * FROM animal_with_state{w} ORDER BY mouse_id", p).fetchall()]
        archived = [dict(r) for r in con.execute(
            f"SELECT * FROM animal_archive{w} ORDER BY mouse_id", p).fetchall()]
    return {
        "cohort": cohort,
        "active": active,
        "archived": archived,
        "total_active": len(active),
        "total_archived": len(archived),
    }


# ---------- Static ----------
# SECURITY: removed `app.mount("/static", StaticFiles(directory=str(ROOT)))` — it
# served the entire repo root (scheduler.db, source, and secrets/) UNAUTHENTICATED,
# bypassing the /api session-or-token gate. Nothing referenced it. PWA assets are
# served by the explicit /pwa/*, /manifest.webmanifest and /sw.js routes. Do NOT
# re-add a broad mount; add narrow per-file routes for any public asset.

@app.get("/labos-nav.js")
def serve_labos_nav() -> FileResponse:
    return FileResponse(
        ROOT / "modules" / "core" / "labos_nav.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )

@app.get("/labos-ui.js")
def serve_labos_ui() -> FileResponse:
    return FileResponse(ROOT / "modules" / "core" / "labos_ui.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})

@app.get("/scheduler.html")
def serve_scheduler() -> FileResponse:
    return FileResponse(ROOT / "modules" / "scheduler" / "scheduler.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/m")
def serve_mobile() -> FileResponse:
    # Dedicated mobile scheduler (iCal-style Agenda/Day/Week). Self-gates via
    # /api/eln/auth/me. scheduler.html redirects phones here.
    return FileResponse(ROOT / "modules" / "scheduler" / "mobile.html",
                        headers={"Cache-Control": "no-store"})

# ---------- PWA (installable "app") — public, served from root scope ----------
@app.get("/manifest.webmanifest")
def serve_manifest() -> FileResponse:
    return FileResponse(ROOT / "static" / "pwa" / "manifest.webmanifest",
                        media_type="application/manifest+json",
                        headers={"Cache-Control": "no-store"})

@app.get("/sw.js")
def serve_sw() -> FileResponse:
    # Served from root so its scope covers the whole app.
    return FileResponse(ROOT / "static" / "pwa" / "sw.js",
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})

# Served at /pwa/* (NOT /static/*, which the StaticFiles mount above owns).
_PWA_ICONS = {"icon-192.png", "icon-512.png", "apple-touch-icon.png"}
@app.get("/pwa/{fname}")
def serve_pwa_icon(fname: str) -> FileResponse:
    if fname not in _PWA_ICONS:
        raise HTTPException(404, "Not found")
    return FileResponse(ROOT / "static" / "pwa" / fname, media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})



# ---------- ELN routes ----------
from fastapi.responses import RedirectResponse  # noqa: E402

@app.get("/eln")
def eln_root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/eln/", status_code=302)

@app.get("/eln/")
def eln_app() -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "notebook.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/m")
def eln_home_mobile() -> FileResponse:
    # Mobile ELN home (project picker). Self-gates; /eln/ redirects phones here.
    return FileResponse(ROOT / "modules" / "eln" / "home_mobile.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/login")
def eln_login() -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "login.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/cage-card")
def eln_cage_card_page() -> FileResponse:
    # Mobile: snap a cage card → GPT vision → confirm → write to colony Sheet.
    return FileResponse(ROOT / "modules" / "eln" / "cage_card.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/project/{slug}")
def eln_project_page(slug: str) -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "project.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/project/{slug}/slice")
def eln_slice_page(slug: str) -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "slice.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/project/{slug}/behavior")
def eln_behavior_page(slug: str) -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "behavior.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/project/{slug}/overview")
def eln_overview_page(slug: str) -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "overview.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/dashboard")
def eln_dashboard_page() -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "dashboard.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/mouse/{mouse_id}")
def eln_mouse_profile_page(mouse_id: str) -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "mouse_profile.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/audit")
def eln_audit_page() -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "audit.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/permissions")
def eln_permissions_page() -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "permissions.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/members")
def eln_members_page() -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "members.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/sync")
def eln_sync_page() -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "sync.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/log")
def eln_log_page() -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "log.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/analysis")
def eln_analysis_page() -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "analysis.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/breeders")
def eln_breeders_page() -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "breeders.html",
                        headers={"Cache-Control": "no-store"})

@app.get("/eln/colony")
def eln_colony_page() -> FileResponse:
    return FileResponse(ROOT / "modules" / "eln" / "colony.html",
                        headers={"Cache-Control": "no-store"})

# ── Legacy pages (read-only archive) ───────────────────────────────────
# The Scheduler-side colony page is superseded by /eln/colony. Redirect old
# bookmarks; the legacy VOR/OKR + ePhys log pages live under /eln/legacy/* now.
@app.get("/colony.html")
def serve_colony_legacy() -> RedirectResponse:
    return RedirectResponse(url="/eln/colony", status_code=302)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "modules" / "scheduler" / "scheduler.html",
                        headers={"Cache-Control": "no-store"})

# ---------- Helpers ----------
import re
def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip().lower()).strip("-")
    return s or "untitled"

def _uid() -> str:
    import secrets
    return secrets.token_hex(5)

# ---------- Entrypoint ----------
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    init_db()
    migrate_db()
    with db_conn() as _con:
        init_eln_tables(_con)
    try:
        with db_conn() as _con:
            _cfg = {r["key"]: r["value"] for r in _con.execute("SELECT key,value FROM animal_config").fetchall()}
        _sheet_id = _cfg.get("sheet_id", "")
        _xl_path = _cfg.get("excel_path", "")
        # Sheet-as-master: startup sync is ON by default so Google Sheet edits
        # become the DB cache after a restart/deploy. Set animal_config
        # 'auto_sync_on_startup'='0' only for emergency freeze/manual DB work.
        _autosync = str(_cfg.get("auto_sync_on_startup", "1")).lower() not in {"0", "false", "no", "off"}
        _synced = False
        if not _autosync:
            print("Animal auto-sync on startup: DISABLED (DB-as-master). "
                  "Import from the Sheet manually via POST /api/animals/sync.")
        if _autosync and not _synced and _xl_path and Path(_xl_path).exists():
            _rep = _sync_excel(_xl_path)
            _c = _rep["counts"]
            print(f"Auto-sync ({Path(_xl_path).name}): +{_c['added']} ~{_c['modified']} "
                  f"-{_c['removed']} ✗{_c['rejected']} ={_c['unchanged']}")
    except Exception as _e:
        print(f"Animal auto-sync skipped: {_e}")

    # Daily per-user schedule-digest emails (07:00 lab time). Only meaningful when
    # SMTP is configured; runs in this single process (reload off → no duplicates).
    try:
        from modules.core import mailer as _mailer
        if _mailer.smtp_configured():
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            from modules.notify import digest as _digest

            def _run_daily_digest() -> None:
                try:
                    with db_conn() as _c:
                        _c.row_factory = sqlite3.Row
                        print(f"[digest] {_digest.send_digests(_c)}")
                except Exception as _e2:
                    print(f"[digest] run failed: {_e2}")

            _sched = BackgroundScheduler(timezone=_digest.LAB_TZ)
            _sched.add_job(_run_daily_digest, CronTrigger(hour=7, minute=0), id="daily_digest")
            _sched.start()
            print(f"Daily schedule-digest scheduled 07:00 {_digest.LAB_TZ}")
        else:
            print("Daily digest disabled (SMTP not configured)")
    except Exception as _e:
        print(f"Digest scheduler not started: {_e}")

    # Ephys analysis background worker — disabled; re-enable when ready
    # try:
    #     from modules.eln import analysis_worker as _aw
    #     _aw.start_worker()
    #     print("Ephys analysis worker started.")
    # except Exception as _e:
    #     print(f"Analysis worker not started: {_e}")

    print(f"Lab Scheduler running at http://{args.host}:{args.port}/scheduler.html")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")

if __name__ == "__main__":
    main()
