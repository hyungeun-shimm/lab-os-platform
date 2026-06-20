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
import urllib.error
import urllib.request
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
    },
    "required": ["cohort", "category", "date_from", "date_to", "per_day"],
}

_SYSTEM = """You convert a lab researcher's plain-language experiment-scheduling \
request into structured parameters. Today's date is {today}; resolve all relative \
dates ("next week", "from Monday", "for two weeks") against it and output every date \
as YYYY-MM-DD. Rules:
- category must be one of: manuscript, surgery, behavior, experiments, histology, deadlines. \
Behavioral assays (OKR, VOR, rotarod, etc.) → category "behavior" with the assay name in subcategory \
(e.g. "VOR / OKR").
- per_day is required; if the user doesn't say, infer a sensible default of 4.
- weekdays_only: true if the user says weekdays/평일 or implies a work-week cadence; else false.
- mix_control: true if the user asks to mix/interleave a control group; set control_cohort to that group.
- start_time defaults to "09:00" and duration_min to 60 unless stated.
Only output the structured parameters."""


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


def preview_from_params(con, params: Dict[str, Any], *, assignee: str = "") -> Dict[str, Any]:
    """Run the deterministic engine on already-parsed params. No LLM — fully testable."""
    cohort = (params.get("cohort") or "").strip()
    if not cohort:
        raise RuntimeError("No cohort in parsed request.")
    for k in ("date_from", "date_to"):
        if not (params.get(k) or "").strip():
            raise RuntimeError(f"Parsed request is missing {k}.")
    control_cohort = (params.get("control_cohort") or "").strip()
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
    events = engine.plan_to_events(
        plan["assignments"],
        category=params.get("category") or "behavior",
        subcategory=params.get("subcategory") or "",
        cohort=label,
        assignee=assignee,
        start_time=params.get("start_time") or "09:00",
        duration_min=int(params.get("duration_min") or 60),
        location=(params.get("location") or "").strip(),
        rig=(params.get("rig") or "").strip(),
    )
    conflicts = engine.check_conflicts(con, events, assignee)
    return {
        "params": params,
        "events": events,
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
    params = parse_request(text, today=today)
    return preview_from_params(con, params, assignee=assignee)
