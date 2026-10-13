"""Schedule-to-notebook links and the daily work queue."""
from __future__ import annotations

import calendar
import json
import secrets
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from modules.core import auth

router = APIRouter(prefix="/api/eln")
LAB_TZ = ZoneInfo("America/Los_Angeles")


def _con():
    from server import db_conn
    return db_conn()


def local_time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(LAB_TZ) if parsed.tzinfo else parsed.replace(tzinfo=LAB_TZ)


def note_url(note):
    page = {"slice_recording": "slice", "behavior": "behavior"}.get(note["experiment_type"], "overview")
    from urllib.parse import urlencode, quote
    return "/eln/project/" + quote(note["project_slug"], safe="") + "/" + page + "?" + urlencode(
        {"date": note["note_date"], "note": note["id"]})


def visible_notes(con, user, event_id=None):
    clause, args = ("", []) if user.get("effective_role") == "admin" else (
        " AND n.user_id=? AND n.project_slug IN "
        "(SELECT project_slug FROM eln_project_members WHERE user_id=?)", [user["id"], user["id"]])
    link = "JOIN eln_event_notes l ON l.note_id=n.id" if event_id else ""
    if event_id:
        clause += " AND l.event_id=?"
        args.append(event_id)
    rows = con.execute("SELECT n.id,n.project_slug,n.experiment_type,n.note_date,n.status,n.user_id,"
                       "n.approved_by,n.updated_at,u.display_name AS author "
                       "FROM eln_notes n LEFT JOIN eln_users u ON u.id=n.user_id "
                       "JOIN eln_projects p ON p.slug=n.project_slug " + link +
                       " WHERE p.archived=0" + clause + " ORDER BY n.note_date DESC,n.updated_at DESC", args).fetchall()
    return [{**dict(r), "url": note_url(r)} for r in rows]


@router.get("/events/{event_id}/notes")
def linked_notes(event_id: str, user: dict = Depends(auth.require_user)):
    with _con() as con:
        return {"notes": visible_notes(con, user, event_id)}


class EventNoteIn(BaseModel):
    project_slug: str
    experiment_type: str
    occurrence_date: Optional[str] = None


@router.post("/events/{event_id}/note")
def event_note(event_id: str, payload: EventNoteIn, user: dict = Depends(auth.require_user)):
    from modules.eln.api import _check_project_access, _row_to_note
    if payload.experiment_type not in {"slice_recording", "behavior"}:
        raise HTTPException(400, "Choose Slice recording or Behavior.")
    with _con() as con:
        role = _check_project_access(con, payload.project_slug, user)
        if role == "read":
            raise HTTPException(403, "This project is read-only.")
        ev = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not ev:
            raise HTTPException(404, "Event not found.")
        try:
            day = date.fromisoformat(payload.occurrence_date).isoformat() if payload.occurrence_date else local_time(ev["start_at"]).date().isoformat()
        except ValueError:
            raise HTTPException(400, "Date must be YYYY-MM-DD.")
        previous = con.execute("SELECT n.* FROM eln_event_notes l JOIN eln_notes n ON n.id=l.note_id "
                               "WHERE l.event_id=? AND n.user_id=? AND n.note_date=? "
                               "AND n.project_slug=? AND n.experiment_type=?", (
                                   event_id, user["id"], day, payload.project_slug, payload.experiment_type)).fetchone()
        if previous:
            return {"note": _row_to_note(previous), "url": note_url(previous), "created": False}
        now = datetime.now(LAB_TZ).isoformat()
        nid = secrets.token_hex(8)
        entries = []
        for mid in json.loads(ev["mice"] or "[]"):
            animal = con.execute("SELECT * FROM animals WHERE LOWER(mouse_id)=LOWER(?) "
                                 "AND COALESCE(removed,0)=0 LIMIT 1", (mid,)).fetchone()
            entry = {"mouse_id": mid, "cohort": ev["cohort"] or "", "entry_notes": ""}
            if animal:
                entry["mouse_id"] = animal["mouse_id"]
                entry["mouse"] = {k: animal[k] for k in ("strain", "sex", "dob", "condition")}
                entry["cohort"] = ev["cohort"] or animal["cohort"] or ""
            entries.append(entry)
        data = {"entries": entries, "schedule_context": {"event_id": event_id, "title": ev["title"],
                "notes": ev["notes"] or "", "location": ev["location"] or "", "rig": ev["rig"] or ""}}
        con.execute("INSERT INTO eln_notes(id,project_slug,user_id,experiment_type,note_date,payload,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,'draft',?,?)", (nid, payload.project_slug, user["id"], payload.experiment_type,
                                                       day, json.dumps(data), now, now))
        con.execute("INSERT INTO eln_event_notes VALUES(?,?,?,?)", (event_id, nid, user["id"], now))
        auth.audit(con, user["id"], "create_note", "note", nid, json.dumps({"event_id": event_id, "date": day}))
        note = con.execute("SELECT * FROM eln_notes WHERE id=?", (nid,)).fetchone()
    return {"note": _row_to_note(note), "url": note_url(note), "created": True}


@router.get("/mice/{mouse_id}/tracking")
def mouse_tracking(mouse_id: str, user: dict = Depends(auth.require_user)):
    mid = mouse_id.strip().casefold()
    with _con() as con:
        team = {r["id"] for r in con.execute("SELECT id FROM team_members WHERE eln_user_id=?", (user["id"],))}
        events = []
        for ev in con.execute("SELECT id,title,start_at,status,mice,assignee,owner_id FROM events ORDER BY start_at DESC"):
            if user.get("effective_role") != "admin" and ev["owner_id"] != user["id"] and ev["assignee"] not in team and ev["assignee"] != user["id"]:
                continue
            if mid in {str(m).strip().casefold() for m in json.loads(ev["mice"] or "[]")}:
                events.append({"id": ev["id"], "title": ev["title"], "date": local_time(ev["start_at"]).date().isoformat(),
                               "status": ev["status"], "url": "/scheduler.html?event=" + ev["id"]})
        visible = {n["id"]: n for n in visible_notes(con, user)}
        note_ids = set()
        for row in con.execute("SELECT id,payload FROM eln_notes"):
            if row["id"] not in visible:
                continue
            payload = json.loads(row["payload"] or "{}")
            if any(str(e.get("mouse_id", "")).strip().casefold() == mid for e in payload.get("entries", [])):
                note_ids.add(row["id"])
        jobs = [{"id": r["id"], "note_id": r["note_id"], "type": r["analysis_type"], "status": r["status"],
                 "date": r["created_at"], "url": "/eln/analysis"} for r in con.execute(
                     "SELECT id,note_id,analysis_type,status,created_at FROM eln_analysis_jobs ORDER BY created_at DESC")
                if r["note_id"] in note_ids]
    return {"events": events, "analyses": jobs, "notes": [visible[n] for n in note_ids]}


def occurrences(ev, lower, upper):
    start, end = local_time(ev["start_at"]), local_time(ev["end_at"])
    duration = end - start
    recur = json.loads(ev["recur"] or "null") or {}
    until = recur.get("until")
    freq = recur.get("freq")
    for _ in range(20000):
        if start > upper or (until and start.date().isoformat() > until):
            break
        if start + duration >= lower:
            yield start, start + duration
        if freq in {"daily", "weekly", "biweekly"}:
            start += timedelta(days={"daily": 1, "weekly": 7, "biweekly": 14}[freq])
        elif freq == "monthly":
            month, year = (1, start.year + 1) if start.month == 12 else (start.month + 1, start.year)
            start = start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))
        else:
            break


@router.get("/workbench")
def workbench(day: Optional[str] = None, user: dict = Depends(auth.require_user)):
    now = datetime.now(LAB_TZ)
    try:
        today = date.fromisoformat(day) if day else now.date()
    except ValueError:
        raise HTTPException(400, "Date must be YYYY-MM-DD.")
    lower = datetime.combine(today - timedelta(days=30), datetime.min.time(), LAB_TZ)
    upper = datetime.combine(today + timedelta(days=1), datetime.min.time(), LAB_TZ)
    is_admin = user.get("effective_role") == "admin"
    with _con() as con:
        team = {r["id"] for r in con.execute("SELECT id FROM team_members WHERE eln_user_id=?", (user["id"],))}
        today_events, overdue = [], []
        for ev in con.execute("SELECT * FROM events ORDER BY start_at"):
            if not is_admin and ev["owner_id"] != user["id"] and ev["assignee"] not in team and ev["assignee"] != user["id"]:
                continue
            try:
                for start, end in occurrences(ev, lower, upper):
                    item = {"id": ev["id"], "title": ev["title"], "start": start.isoformat(), "end": end.isoformat(),
                            "status": ev["status"], "mice": json.loads(ev["mice"] or "[]"), "cohort": ev["cohort"],
                            "project_slug": ev["project_slug"], "rig": ev["rig"], "location": ev["location"],
                            "url": "/scheduler.html?event=" + ev["id"]}
                    if start.date() <= today <= end.date() and start < upper:
                        today_events.append(item)
                    if ev["status"] in {"planned", "in_progress", "delayed"} and end < now and end.date() < today:
                        overdue.append(item)
            except (ValueError, TypeError):
                continue
        notes = visible_notes(con, user)
        latest = con.execute("SELECT report FROM animal_sync_runs ORDER BY ts DESC LIMIT 1").fetchone()
        synced = con.execute("SELECT value FROM animal_config WHERE key='last_synced'").fetchone()
        animals = con.execute("SELECT COUNT(*) FROM animals WHERE COALESCE(removed,0)=0 AND assigned_to=?", (user["id"],)).fetchone()[0]
    submitted = [n for n in notes if n["status"] == "submitted"]
    drafts = [n for n in notes if n["status"] == "draft"]
    return {"day": today.isoformat(), "is_admin": is_admin,
            "today": sorted(today_events, key=lambda e: e["start"]), "overdue": overdue[-100:],
            "submitted": submitted[:50], "drafts": drafts[:50], "my_animals": animals,
            "counts": {"today": len(today_events), "overdue": len(overdue), "submitted": len(submitted), "drafts": len(drafts)},
            "last_synced": synced[0] if synced else None, "sync_report": json.loads(latest[0]) if latest else None}
