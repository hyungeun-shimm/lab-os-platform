"""AI-assisted scheduling: natural language → preview → commit.

Ties the Gemini parser (BYOK) to the deterministic engine. Gemini's *only* job is
turning free text into a structured ``params`` dict; every scheduling decision and
DB write goes through ``engine.py`` / ``server.upsert_event``. That split keeps the
LLM cheap (a few hundred tokens), removes any chance of hallucinated mouse IDs, and
lets the whole flow be tested without a key via ``preview_from_params``.

Gemini is called over plain HTTPS (stdlib ``urllib``) so no new dependency is added.
The key comes from the ``GEMINI_API_KEY`` env var (a free Google AI Studio key).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from . import engine

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# Schema Gemini must fill from the user's request (a subset of OpenAPI schema).
_PARAMS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": "Either 'add' for new schedule creation or 'delete' for removing existing calendar events.",
        },
        "query": {
            "type": "string",
            "description": "Short search text for delete requests, e.g. 'OKR' or 'slice recording'.",
        },
        "title_contains": {
            "type": "string",
            "description": "Text that should appear in existing event titles for delete requests.",
        },
        "cohort": {"type": "string", "description": "Primary cohort name to schedule."},
        "control_cohort": {"type": "string", "description": "Control-group cohort, if mentioned."},
        "category": {
            "type": "string",
            "description": "One of: manuscript, surgery, behavior, experiments, histology, deadlines.",
        },
        "subcategory": {"type": "string", "description": "Assay/sub-type, e.g. 'VOR / OKR'."},
        "date_from": {"type": "string", "description": "Start date, YYYY-MM-DD."},
        "date_to": {"type": "string", "description": "End date, YYYY-MM-DD."},
        "per_day": {"type": "integer", "description": "How many mice per day."},
        "weekdays_only": {"type": "boolean", "description": "Skip weekends."},
        "mix_control": {"type": "boolean", "description": "Randomly interleave control mice."},
        "start_time": {"type": "string", "description": "First slot, HH:MM (24h)."},
        "duration_min": {"type": "integer", "description": "Minutes per mouse."},
        "location": {"type": "string", "description": "Room/location, if mentioned."},
        "rig": {"type": "string", "description": "Rig number/name, if mentioned."},
        "mice": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Mouse IDs mentioned in the request.",
        },
        "project_slug": {"type": "string", "description": "Project slug/name if mentioned."},
        "assignee": {"type": "string", "description": "Assignee/team member if mentioned."},
    },
    "required": ["action", "date_from", "date_to"],
}

_SYSTEM = """You convert a lab researcher's plain-language experiment-scheduling \
request into structured parameters. Today's date is {today}; resolve all relative \
dates ("next week", "from Monday", "for two weeks") against it and output every date \
as YYYY-MM-DD. Rules:
- If the user asks to delete, remove, cancel, or clear existing calendar items, set \
action to "delete". For delete requests, cohort and per_day are optional; do not invent \
a cohort. Fill query/title_contains with the shortest meaningful text to match existing \
events, such as "OKR". A deletion still needs date_from/date_to.
- If the user asks to add, create, plan, or schedule new events, set action to "add". \
For add requests, cohort is required and per_day is required. Auto-calculate per_day when possible: \
if user specifies start_time AND end_time (or "until X"), calculate available_hours and per_day = \
floor(available_hours / (duration_min/60)). E.g., "9am until midnight, 2 hours per mouse" → \
15 hours / 2 = 7-8 per day. Only use default 4 if time range is not specified.
- category must be one of: manuscript, surgery, behavior, experiments, histology, deadlines. \
Behavioral assays (OKR, VOR, rotarod, etc.) → category "behavior" with the assay name in subcategory \
(e.g. "VOR / OKR").
- weekdays_only: true if the user says weekdays/평일 or implies a work-week cadence; else false.
- For control groups: if cohort is "X" and user mentions "control", "with control", or doesn't \
explicitly reject controls, AUTO-DETECT control_cohort as "X-con" (append "-con"). Set mix_control true \
to randomly interleave experimental and control mice.
- assignee: if the user mentions a team member name (e.g. "Your_Name", "Maxwell", initials), extract \
and include it. Otherwise leave empty.
- start_time defaults to "09:00" and duration_min to 60 unless stated.
Only output the structured parameters."""

_DELETE_WORDS = {
    "a", "all", "an", "and", "calendar", "can", "cancel", "clear", "could",
    "delete", "event", "events", "existing", "for", "from", "in", "my", "of",
    "on", "please", "remove", "schedule", "schedules", "scheduled", "the",
    "this", "to", "week", "would", "you",
}
_DELETE_RE = re.compile(
    r"\b(cancel|clear|delete|remove|drop|erase)\b|삭제|지워|취소",
    re.IGNORECASE,
)
_DATE_TOKEN_RE = re.compile(
    r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?)\b"
)
_ASSAY_ALIASES = [
    ("VOR / OKR", re.compile(r"\bvor\s*/\s*okr\b|\bvor\s+okr\b|\bokr\s+vor\b", re.I)),
    ("OKR", re.compile(r"\bokr\b", re.I)),
    ("VOR", re.compile(r"\bvor\b", re.I)),
    ("rotarod", re.compile(r"\brotarod\b", re.I)),
    ("slice", re.compile(r"\bslice\b|\bephys\b|전기생리", re.I)),
    ("histology", re.compile(r"\bhistology\b", re.I)),
    ("surgery", re.compile(r"\bsurgery\b|수술", re.I)),
]


def _gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set on the server. Add a free Google AI Studio key."
        )
    return key


def parse_request(text: str, *, today: str) -> Dict[str, Any]:
    """Call Gemini to turn ``text`` into a params dict. Raises RuntimeError on failure."""
    if not (text or "").strip():
        raise RuntimeError("Empty request.")
    body = {
        "system_instruction": {"parts": [{"text": _SYSTEM.format(today=today)}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _PARAMS_SCHEMA,
            "temperature": 0,
        },
    }
    url = _GEMINI_URL.format(model=GEMINI_MODEL) + "?key=" + _gemini_key()
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Gemini API error ({e.code}): {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Gemini API: {e.reason}")
    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(raw)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Unexpected Gemini response shape: {e}")


def _looks_like_delete(text: str) -> bool:
    return bool(_DELETE_RE.search(text or ""))


def _parse_date_token(token: str, *, today_d: date) -> date:
    parts = [int(p) for p in re.split(r"[-/]", token)]
    if len(parts) == 3 and len(token.split("-")[0].split("/")[0]) == 4:
        y, m, d = parts
    elif len(parts) == 3:
        m, d, y = parts
        if y < 100:
            raise RuntimeError("Use a 4-digit year in delete date ranges.")
    elif len(parts) == 2:
        m, d = parts
        y = today_d.year
    else:
        raise RuntimeError(f"Could not parse date: {token}")
    return date(y, m, d)


def _this_week(today_d: date) -> tuple[date, date]:
    start = today_d - timedelta(days=today_d.weekday())
    return start, start + timedelta(days=6)


def _local_delete_params(text: str, *, today: str) -> Optional[Dict[str, Any]]:
    """Parse common delete requests locally so Gemini outages cannot block deletion."""
    if not _looks_like_delete(text):
        return None
    try:
        today_d = date.fromisoformat(today)
    except ValueError:
        today_d = date.today()

    date_tokens = _DATE_TOKEN_RE.findall(text or "")
    if len(date_tokens) >= 2:
        d0 = _parse_date_token(date_tokens[0], today_d=today_d)
        d1 = _parse_date_token(date_tokens[1], today_d=today_d)
    elif len(date_tokens) == 1:
        d0 = d1 = _parse_date_token(date_tokens[0], today_d=today_d)
    elif re.search(r"\bthis\s+week\b|이번\s*주", text or "", re.I):
        d0, d1 = _this_week(today_d)
    elif re.search(r"\btoday\b|오늘", text or "", re.I):
        d0 = d1 = today_d
    elif re.search(r"\btomorrow\b|내일", text or "", re.I):
        d0 = d1 = today_d + timedelta(days=1)
    else:
        raise RuntimeError("For deletion, include a date range, e.g. 'delete OKR from 6/23 to 6/28'.")

    if d1 < d0:
        d1 = d1.replace(year=d0.year + 1)

    query = ""
    subcategory = ""
    category = ""
    for label, pattern in _ASSAY_ALIASES:
        if pattern.search(text or ""):
            query = label
            if label in {"VOR / OKR", "OKR", "VOR", "rotarod"}:
                category = "behavior"
                subcategory = label
            elif label == "histology":
                category = "histology"
            elif label == "surgery":
                category = "surgery"
            elif label == "slice":
                category = "experiments"
                subcategory = "Slice ePhys"
            break
    if not query:
        cleaned = _DATE_TOKEN_RE.sub(" ", text or "")
        cleaned = _DELETE_RE.sub(" ", cleaned)
        terms = [
            t for t in re.findall(r"[A-Za-z][A-Za-z0-9_/-]*", cleaned)
            if t.lower() not in _DELETE_WORDS
        ]
        query = " ".join(terms[:4])

    return {
        "action": "delete",
        "query": query,
        "title_contains": query,
        "category": category,
        "subcategory": subcategory,
        "date_from": d0.isoformat(),
        "date_to": d1.isoformat(),
        "per_day": 0,
    }


def _str(v: Any) -> str:
    return str(v or "").strip()


def _terms(text: str) -> List[str]:
    out: List[str] = []
    for t in re.findall(r"[A-Za-z0-9]+", (text or "").lower()):
        if len(t) <= 1 or t in _DELETE_WORDS:
            continue
        out.append(t)
    return out


def _row_text(row: Dict[str, Any]) -> str:
    mice = row.get("mice") or ""
    return " ".join(
        _str(row.get(k))
        for k in (
            "title", "category", "subcategory", "subcategory2", "cohort",
            "project_slug", "status", "notes", "location", "rig",
        )
    ).lower() + " " + _str(mice).lower()


def _event_row_to_preview(r) -> Dict[str, Any]:
    mice_raw = r["mice"] or "[]"
    try:
        mice = json.loads(mice_raw)
    except (TypeError, json.JSONDecodeError):
        mice = []
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
        "mice": mice,
        "assignee": r["assignee"] or "",
        "location": r["location"] or "",
        "rig": r["rig"] or "",
    }


def _matches_delete_filters(ev: Dict[str, Any], params: Dict[str, Any]) -> bool:
    category = _str(params.get("category")).lower()
    if category and _str(ev.get("category")).lower() != category:
        return False

    sub_terms = _terms(_str(params.get("subcategory")))
    text = _row_text(ev)
    if sub_terms and not any(t in text for t in sub_terms):
        return False

    query_text = " ".join([
        _str(params.get("query")),
        _str(params.get("title_contains")),
    ])
    query_terms = _terms(query_text)
    if query_terms and not any(t in text for t in query_terms):
        return False

    cohort = _str(params.get("cohort")).lower()
    if cohort and cohort not in _str(ev.get("cohort")).lower():
        return False

    project = _str(params.get("project_slug")).lower()
    if project and project not in _str(ev.get("project_slug")).lower():
        return False

    assignee = _str(params.get("assignee"))
    if assignee and assignee != _str(ev.get("assignee")):
        return False

    mice = [m.lower() for m in (params.get("mice") or []) if _str(m)]
    if mice:
        ev_mice = [_str(m).lower() for m in (ev.get("mice") or [])]
        if not any(m in ev_mice for m in mice):
            return False

    return True


def preview_delete_from_params(con, params: Dict[str, Any]) -> Dict[str, Any]:
    """Find existing events that would be deleted by a natural-language request."""
    for k in ("date_from", "date_to"):
        if not _str(params.get(k)):
            raise RuntimeError(f"Parsed delete request is missing {k}.")
    try:
        start = date.fromisoformat(params["date_from"])
        end_incl = date.fromisoformat(params["date_to"])
    except ValueError as e:
        raise RuntimeError(f"Parsed delete request has an invalid date: {e}")
    if end_incl < start:
        raise RuntimeError("Delete date_to is before date_from.")

    end_excl = end_incl + timedelta(days=1)
    rows = con.execute(
        """
        SELECT id, title, category, subcategory, subcategory2, cohort, project_slug,
               status, start_at, end_at, all_day, notes, mice, assignee, location, rig
          FROM events
         WHERE start_at < ? AND end_at > ?
         ORDER BY start_at, title
        """,
        (end_excl.isoformat(), start.isoformat() + "T00:00"),
    ).fetchall()
    candidates = [_event_row_to_preview(r) for r in rows]
    matches = [ev for ev in candidates if _matches_delete_filters(ev, params)]
    warnings: List[str] = []
    has_text_filter = any(
        _str(params.get(k))
        for k in ("query", "title_contains", "category", "subcategory", "cohort", "project_slug", "assignee")
    ) or bool(params.get("mice"))
    if not has_text_filter:
        warnings.append("No event filter was parsed; this matches every event in the date range.")
    if not matches:
        warnings.append("No existing events matched the delete request.")
    return {
        "action": "delete",
        "params": params,
        "events": [],
        "delete_events": matches,
        "delete_ids": [ev["id"] for ev in matches],
        "conflicts": [],
        "unscheduled": [],
        "warnings": warnings,
        "summary": {
            "matched": len(matches),
            "candidates": len(candidates),
            "days": (end_incl - start).days + 1,
            "scheduled": 0,
            "mice": 0,
            "conflicts": 0,
        },
    }


def preview_from_params(con, params: Dict[str, Any], *, assignee: str = "") -> Dict[str, Any]:
    """Run the deterministic engine on already-parsed params. No LLM — fully testable."""
    action = _str(params.get("action") or "add").lower()
    if action in {"delete", "remove", "cancel", "clear"}:
        return preview_delete_from_params(con, {**params, "action": "delete"})

    cohort = (params.get("cohort") or "").strip()
    if not cohort:
        raise RuntimeError("No cohort in parsed request.")
    for k in ("date_from", "date_to"):
        if not (params.get(k) or "").strip():
            raise RuntimeError(f"Parsed request is missing {k}.")
    control_cohort = (params.get("control_cohort") or "").strip()
    # Auto-detect control cohort if not specified: try "{cohort}-con"
    if not control_cohort:
        auto_ctrl = f"{cohort}-con"
        if engine.resolve_cohort_mice(con, auto_ctrl):
            control_cohort = auto_ctrl
    mix_control = bool(params.get("mix_control", bool(control_cohort)))

    exp_mice = engine.resolve_cohort_mice(con, cohort)
    ctrl_mice = engine.resolve_cohort_mice(con, control_cohort) if control_cohort else []

    plan = engine.build_schedule(
        exp_mice=exp_mice,
        ctrl_mice=ctrl_mice,
        date_from=params["date_from"],
        date_to=params["date_to"],
        per_day=int(params.get("per_day") or 4),
        weekdays_only=bool(params.get("weekdays_only", False)),
        mix_control=mix_control,
    )
    label = f"{cohort} (mixed)" if mix_control and ctrl_mice else cohort
    # Use assignee from params (parsed by Gemini) or fallback to function param
    assignee_name_or_id = (params.get("assignee") or "").strip() or assignee

    # Try to resolve assignee name to team_member ID
    final_assignee = assignee_name_or_id
    if assignee_name_or_id:
        # Check if it's already an ID or if it's a name that needs lookup
        team_members = con.execute("SELECT id, name FROM team_members").fetchall()
        for tm in team_members:
            # Match by name (case-insensitive) or by ID
            if tm["name"].lower() == assignee_name_or_id.lower():
                final_assignee = tm["id"]
                break
            elif tm["id"] == assignee_name_or_id:
                final_assignee = tm["id"]
                break
    events = engine.plan_to_events(
        plan["assignments"],
        category=params.get("category") or "behavior",
        subcategory=params.get("subcategory") or "",
        cohort=label,
        assignee=final_assignee,
        start_time=params.get("start_time") or "09:00",
        duration_min=int(params.get("duration_min") or 60),
        location=(params.get("location") or "").strip(),
        rig=(params.get("rig") or "").strip(),
    )
    conflicts = engine.check_conflicts(con, events, final_assignee)
    return {
        "action": "add",
        "params": params,
        "events": events,
        "delete_events": [],
        "delete_ids": [],
        "conflicts": conflicts,
        "unscheduled": plan["unscheduled"],
        "warnings": plan["warnings"],
        "summary": {
            "mice": plan["n_mice"], "days": plan["n_days"],
            "scheduled": len(events), "conflicts": len(conflicts),
        },
    }


def preview(con, text: str, *, assignee: str = "", today: str) -> Dict[str, Any]:
    """Full flow: parse natural language with Gemini, then build the preview."""
    local_delete = _local_delete_params(text, today=today)
    if local_delete:
        return preview_from_params(con, local_delete, assignee=assignee)
    params = parse_request(text, today=today)
    return preview_from_params(con, params, assignee=assignee)
