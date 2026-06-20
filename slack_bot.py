"""Lab Assistant Slack Bot.

Runs alongside server.py as a background service.
Uses Slack Bolt SDK with Socket Mode (no public URL needed).

Setup:
  1. Create Slack app at https://api.slack.com/apps
  2. Enable Socket Mode, get App-Level Token (xapp-...)
  3. Add Bot Token Scopes: chat:write, channels:read, im:read, im:write, im:history
  4. Enable Event Subscriptions: message.im, message.channels
  5. Install app to workspace, get Bot Token (xoxb-...)
  6. Copy .env.example → .env, fill in tokens
  7. python3 slack_bot.py

Commands (send as DM or in a channel where bot is present):
  /today           — Today's schedule brief
  /week            — This week's schedule
  /schedule        — Auto-schedule from cohort + condition entry
  /animals <query> — Search animal DB
  /help            — Show all commands
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# ---------- Env ----------
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SCHEDULER_API   = os.environ.get("SCHEDULER_API", "http://127.0.0.1:8766")
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
SLACK_POLL_FALLBACK = os.environ.get("SLACK_POLL_FALLBACK", "1").strip().lower() not in {"0", "false", "no"}
SLACK_POLL_CHANNELS = os.environ.get("SLACK_POLL_CHANNELS", "your-slack-channel")
SLACK_POLL_INTERVAL = int(os.environ.get("SLACK_POLL_INTERVAL", "5"))

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "scheduler.db"
POLL_STATE_PATH = ROOT / "slack_poll_state.json"

# ---------- DB helpers ----------
def db_conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def today_str() -> str:
    return date.today().isoformat()


def _load_poll_state() -> dict:
    if not POLL_STATE_PATH.exists():
        return {"seen_ts": []}
    try:
        return json.loads(POLL_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"seen_ts": []}


def _save_poll_state(state: dict) -> None:
    seen = list(dict.fromkeys(state.get("seen_ts", [])))[-2000:]
    POLL_STATE_PATH.write_text(json.dumps({"seen_ts": seen}, indent=2), encoding="utf-8")

# ---------- Shared pipeline (OCR → parse → diff → upsert) ----------
from animal_updater import (
    openai_chat as _openai_chat,
    parse_with_gpt_vision,
    parse_with_gpt as parse_cage_card_with_gpt,
    build_diff as _build_ocr_diff,
    upsert as _db_upsert,
    validate as _validate_animals,
    SAFE_UPDATE_FIELDS as _SAFE_UPDATE_FIELDS,
)

# ---------- Slack-specific NL parser (GPT fallback) ----------

_NL_SYS = """You are a lab assistant Slack bot command parser for a neuroscience lab.
Parse the user message into an intent JSON. Output ONLY valid JSON, no prose.

Available actions (pick the closest match):
{"action":"today"}          — today's schedule
{"action":"week"}           — this week's schedule
{"action":"upcoming","days":<int>}
{"action":"animals","query":"","cohort":"","condition":"","project":""}
{"action":"schedule","cohort":"","condition":"","cond_ref":<0|1|2|3>,"ref":"",
  "start_date":"YYYY-MM-DD","end_date":"YYYY-MM-DD","no_animals":<true|false>,
  "days":[<int>...],"task":"","control":"","per_day":<int>,"project":""}
                            — `start_date` + optional `end_date` creates calendar
                              events on those literal dates (consecutive days). Use
                              for "6/12-13", "6/12부터 6/14까지", "starting June 12".
                              Set ref="start" when you populate start_date.
                            — `no_animals=true` when the user says the animals aren't
                              in the DB yet ("동물 db에 없어", "no animals registered",
                              "마우스 정보 없음"). Creates placeholder events without
                              mouse lists.
                            — `ref` selects which date column to use as D+0 reference:
                                "dos"   = surgery date (dos column)
                                          Triggers: "수술일 기준", "수술 후", "post-surgery",
                                          "after surgery", "dos+N", "수술 N일 뒤".
                                "cond1" = date_cond1 (dark-treatment / NR / DT entry)
                                "cond2" = date_cond2 (Dark→Light transition / DLT / 23DMR / 24DMR)
                                          Triggers: "dark→light", "dark to light", "DLT",
                                          "빛으로 옮긴", "light 전환", "cond-2".
                                "cond3" = date_cond3
                              If `ref` is set, leave cond_ref=0. Otherwise use cond_ref (1/2/3).
                              For "acclimatization", "handling", "habituation" tasks the
                              reference is usually surgery → set ref="dos".
                              Task keyword controls the calendar category automatically:
                                OKR/VOR → experiments (Behavior / VOR-OKR)
                                acclimatization/handling/habituation → behavior
                                surgery → surgery,  histology/perfusion → histology,
                                slice ePhys/ephys → experiments (Slice ePhys)
{"action":"add_event","category":"surgery","mouse_id":"","date":"YYYY-MM-DD",
  "time":"HH:MM","title":"","project":"","subcategory":"","notes":""}
                            — add a calendar event. category: surgery/behavior/experiments/histology/deadlines/manuscript
                              e.g. "WT3546 수술 6/15 9시", "schedule behavior for WT3546 on June 20"
{"action":"delete_event","event_id":"","date":"YYYY-MM-DD","category":"","mouse_id":""}
                            — delete/cancel an event. Provide as many criteria as available.
{"action":"cohort_status","cohort":""}
                            — show all mice + current state + upcoming events for a cohort
{"action":"sacrifice_mouse","mouse_id":"","date":"YYYY-MM-DD"}
                            — mark mouse as sacrificed. e.g. "WT3546 sacrifice 오늘", "WT3546 죽었어"
{"action":"mark_ephys","mouse_id":"","date":"YYYY-MM-DD","notes":""}
                            — mark mouse as used for ePhys recording
                              e.g. "WT3546 ePhys에 사용", "WT3546 went to ephys"
{"action":"cohort_report","cohort":""}
                            — generate CSV report for a cohort (active + archived). uploads as file.
{"action":"archive_cohort","cohort":""}
                            — move all sacrificed mice in cohort to archive
{"action":"reschedule","cohort":"","task":"OKR","new_days":[<int>...],"cond_ref":<0|1|2|3>,"project":""}
                            — find and replace existing scheduled events for a task/cohort with new day offsets
                              e.g. "OKR 스케쥴 30, 60일로 바꿔줘", "change dA cohort OKR from 20/40/60 to 30/60 days"
                              task field: "OKR", "behavior", "ePhys", "surgery", or any keyword from the title/subcategory
{"action":"status"}         — system/API status check
{"action":"update_condition","mouse_id":"WT2595","condition":"DLT","date":"2026-06-15","cond_num":2}
{"action":"help"}
{"action":"unknown","message":"<reason in Korean>"}

Notes:
- condition codes: DT, NR, DLT, 23dmr, 24dmr
- dates: convert M/D/YY or M/D/YYYY to YYYY-MM-DD. "오늘"/"today" → omit (use empty string)
- Korean and English both OK.
- The "unknown" message field must be written in Korean."""


def parse_nl_with_gpt(text: str) -> dict | None:
    """GPT-4o-mini fallback for NL command parsing."""
    if not OPENAI_API_KEY:
        return None
    try:
        raw = _openai_chat(
            [{"role": "system", "content": _NL_SYS},
             {"role": "user",   "content": text}],
            json_mode=True,
        )
        intent = json.loads(raw)
        if isinstance(intent, dict) and intent.get("action"):
            print(f"[GPT NL] action={intent['action']}")
            return intent
    except Exception as e:
        print(f"[GPT NL] error: {e}")
    return None


# ---------- Scheduler API helpers ----------
import urllib.request
import urllib.error

def _api(method: str, path: str, body: Any = None) -> Any:
    url = f"{SCHEDULER_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    _tok = os.environ.get("LABOS_API_TOKEN", "").strip()
    if _tok:
        headers["X-Labos-Token"] = _tok  # scheduler API now requires session OR token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API {method} {path} → {e.code}: {e.read().decode()}")

def get_events(start: str, end: str) -> list[dict]:
    try:
        data = _api("GET", f"/api/events")
        events = data.get("events", [])
        return [e for e in events
                if e.get("start", "") >= start and e.get("start", "") < end]
    except Exception:
        return []

def get_animals(query: str = "", condition: str = "", project: str = "",
                cohort: str = "", current_state: str = "") -> list[dict]:
    """Query animal_with_state view directly from SQLite."""
    try:
        with db_conn() as con:
            rows = con.execute(
                "SELECT * FROM animal_with_state ORDER BY project, mouse_id"
            ).fetchall()
        results = []
        for row in rows:
            a = dict(row)
            if (a.get("sacrificed") or "").upper() == "Y":
                continue
            if query:
                q = query.lower()
                searchable = " ".join(filter(None, [
                    a.get("mouse_id"), a.get("project"), a.get("cohort"),
                    a.get("condition"), a.get("current_state"), a.get("strain"),
                ])).lower()
                if q not in searchable:
                    continue
            # condition = protocol group label (e.g. "DT")
            if condition and (a.get("condition") or "").lower() != condition.lower():
                continue
            # current_state = computed live state (e.g. "NR", "DT")
            if current_state and (a.get("current_state") or "").lower() != current_state.lower():
                continue
            if project and project.lower() not in (a.get("project") or "").lower():
                continue
            if cohort and cohort.lower() not in (a.get("cohort") or "").lower():
                continue
            results.append(a)
        return results
    except Exception as e:
        print(f"get_animals error: {e}")
        return []

def create_event(ev: dict) -> dict:
    return _api("POST", "/api/events", ev)

# ---------- Formatting helpers ----------
STATUS_EMOJI = {
    "planned": "📋", "in_progress": "🔄", "done": "✅",
    "delayed": "⏸️", "skipped": "⏭️",
}
CAT_EMOJI = {
    "surgery": "🔬", "experiments": "🧪", "behavior": "🐭",
    "histology": "🔬", "manuscript": "📝", "deadlines": "⏰",
}

def fmt_event(ev: dict, show_date: bool = False) -> str:
    cat = ev.get("category", "")
    icon = CAT_EMOJI.get(cat, "📌")
    status = STATUS_EMOJI.get(ev.get("status", "planned"), "📋")
    start = ev.get("start", "")
    time_part = ""
    if start and not ev.get("all_day"):
        try:
            dt = datetime.fromisoformat(start)
            time_part = dt.strftime("%H:%M")
            if show_date:
                time_part = dt.strftime("%m/%d %H:%M")
        except Exception:
            pass
    elif show_date and start:
        time_part = start[:10]
    title = ev.get("title", "Untitled")
    project = ev.get("project_slug", "")
    proj_str = f" [{project}]" if project else ""
    return f"{status}{icon} {time_part} *{title}*{proj_str}"

STATE_EMOJI = {
    "NR": "🏠", "DT": "🌑", "DLT": "🌓",
    "23dmr": "🔅", "24dmr": "🔅",
}

def fmt_animal(a: dict) -> str:
    sex_icon = "♂" if (a.get("sex") or "").upper() == "M" else "♀" if (a.get("sex") or "").upper() == "F" else "?"
    cohort  = a.get("cohort") or ""
    project = a.get("project") or ""
    dob     = a.get("dob") or ""

    age_str = ""
    if dob:
        try:
            age_days = (date.today() - date.fromisoformat(dob[:10])).days
            age_str = f" {age_days//7}w"
        except Exception:
            pass

    # current_state with days counter
    cs = a.get("current_state") or a.get("condition") or "?"
    days_in = a.get("days_in_state")
    state_str = f"{STATE_EMOJI.get(cs, '●')}*{cs}*"
    if days_in is not None:
        state_str += f" D+{days_in}"

    return f"• `{a['mouse_id']}` {sex_icon}{age_str} | {state_str} | {cohort} | {project}"

# ---------- Brief builders ----------
def build_today_brief() -> str:
    today = today_str()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    events = get_events(today, tomorrow)
    if not events:
        return f"📅 *Today — {today}*\nNo scheduled events. Clear day! 🎉"
    lines = [f"📅 *Today — {today}* ({len(events)} events)\n"]
    for ev in sorted(events, key=lambda e: e.get("start", "")):
        lines.append(fmt_event(ev))
    return "\n".join(lines)

def build_week_brief() -> str:
    today = date.today()
    # Start from Monday
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=7)
    events = get_events(monday.isoformat(), sunday.isoformat())
    if not events:
        return f"📅 *Week of {monday.strftime('%b %d')}*\nNo scheduled events this week."
    lines = [f"📅 *Week of {monday.strftime('%b %d')} — {sunday.strftime('%b %d')}* ({len(events)} events)\n"]
    # Group by day
    by_day: dict[str, list] = {}
    for ev in events:
        day = (ev.get("start") or "")[:10]
        by_day.setdefault(day, []).append(ev)
    for day in sorted(by_day):
        dt = date.fromisoformat(day)
        day_label = dt.strftime("%a %m/%d")
        lines.append(f"\n*{day_label}*")
        for ev in sorted(by_day[day], key=lambda e: e.get("start", "")):
            lines.append(fmt_event(ev))
    return "\n".join(lines)

def build_upcoming_brief(days: int = 3) -> str:
    today = date.today()
    end = (today + timedelta(days=days)).isoformat()
    events = get_events(today.isoformat(), end)
    if not events:
        return f"📆 No events in the next {days} days."
    lines = [f"📆 *Next {days} days*\n"]
    by_day: dict[str, list] = {}
    for ev in events:
        day = (ev.get("start") or "")[:10]
        by_day.setdefault(day, []).append(ev)
    for day in sorted(by_day):
        dt = date.fromisoformat(day)
        day_label = dt.strftime("%a %m/%d")
        lines.append(f"\n*{day_label}*")
        for ev in sorted(by_day[day], key=lambda e: e.get("start", "")):
            lines.append(fmt_event(ev))
    return "\n".join(lines)

# ---------- Auto-scheduling engine ----------
import secrets

def _uid() -> str:
    return secrets.token_hex(5)

# Natural language parser for scheduling commands
# Examples:
#   "dA cohort 컨디션2 진입 뒤 20, 40, 60일 뒤 OKR 측정, dA-control이랑 섞어서 하루 3-5마리"
#   "schedule cohort dB_control after cond2 entry: 20d, 40d, 60d OKR, mix with dB_nr, 4 mice/day"
#   "/schedule cohort=dA condition=DT days=20,40,60 task=OKR control=dA-control per_day=4"

SCHEDULE_HELP = """*Auto-scheduling format:*
```
/schedule
  cohort=<name>         cohort name (partial match OK)
  condition=<cond>      NR | DT | 23dmr | 24dmr | DLT
  cond_date=<YYYY-MM-DD>  date mice entered condition (defaults to date_cond1/2/3)
  days=20,40,60         days after condition entry to schedule
  task=OKR              experiment task name
  sub=<subcategory>     subcategory (default: VOR / OKR)
  control=<cohort>      control cohort to mix in
  per_day=4             mice per day (default: 4)
  project=<slug>        project slug
```
*Example:*
`/schedule cohort=dA condition=DT days=20,40,60 task=OKR control=dA-control per_day=4 project=metaplasticity-dark`"""

def parse_schedule_args(text: str) -> dict:
    """Parse key=value args from schedule command text."""
    params: dict[str, Any] = {}
    # Try key=value pairs
    for m in re.finditer(r'(\w+)=([^\s]+)', text):
        k, v = m.group(1).lower(), m.group(2)
        params[k] = v
    # days: parse comma-separated
    if "days" in params:
        try:
            params["days"] = [int(x) for x in str(params["days"]).split(",")]
        except Exception:
            params["days"] = []
    if "per_day" in params:
        try:
            params["per_day"] = int(params["per_day"])
        except Exception:
            params["per_day"] = 4
    return params

CONDITION_COND_MAP = {
    "NR": "date_cond1",    # Normal Rearing → cond1
    "DT": "date_cond1",    # Dark Treated → cond1 (first transition)
    "DLT": "date_cond2",   # Dark-Light Transition → cond2
    "23DMR": "date_cond2",
    "24DMR": "date_cond2",
}

TASK_CATEGORY_MAP = {
    # task keyword (lowercase substring) → (category, subcategory, subcategory2)
    "okr":             ("experiments", "Behavior", "VOR / OKR"),
    "vor":             ("experiments", "Behavior", "VOR / OKR"),
    "acclimat":        ("behavior",    "",         ""),
    "handling":        ("behavior",    "",         ""),
    "habituat":        ("behavior",    "",         ""),
    "surgery":         ("surgery",     "",         ""),
    "histology":       ("histology",   "",         ""),
    "perfus":          ("histology",   "",         ""),
    "ephys":           ("experiments", "Slice ePhys", ""),
    "slice ephys":     ("experiments", "Slice ePhys", ""),
}

def _resolve_category(task: str, default_sub: str = "VOR / OKR") -> tuple[str, str, str]:
    """Map task name → (category, subcategory, subcategory2)."""
    tl = (task or "").lower()
    for kw, triple in TASK_CATEGORY_MAP.items():
        if kw in tl:
            return triple
    # Fallback: keep legacy experiments+sub behavior
    return ("experiments", "", "")

def _is_control_cohort(cohort_name: str) -> bool:
    """Convention: cohort name containing '-con' (e.g. 'dA-con', 'DLTA-con',
    'dB-control') marks control animals. Used to label mice as 'con-<id>'
    on event cards so users can tell exp vs ctrl at a glance."""
    return '-con' in (cohort_name or '').lower()


def _label_mouse(m: dict) -> str:
    """Return mouse_id with 'con-' prefix if its cohort is a control cohort."""
    mid = m.get("mouse_id", "")
    if not mid or mid.startswith("con-"):
        return mid
    if _is_control_cohort(m.get("cohort", "")):
        return f"con-{mid}"
    return mid


def _is_eligible_for_behavior(m: dict) -> bool:
    """True if mouse should be included in behavior/experiment schedules.
    Excludes ePhys-reassigned and dropped (excluded) animals."""
    exp = (m.get("experiment") or "").lower()
    if "ephys" in exp:
        return False
    notes = (m.get("notes") or "").lower()
    for kw in ("exclud", "bad signal", "no signal", "dropped", "drop ", "to ephys"):
        if kw in notes:
            return False
    return True

def do_auto_schedule(params: dict) -> str:
    cohort_q   = params.get("cohort", "")
    condition  = params.get("condition", "").upper()   # protocol label filter (optional)
    cond_ref   = int(params.get("cond_ref", 0))        # 1/2/3 → date_cond1/2/3 as reference
    ref        = (params.get("ref") or "").lower()     # "dos" | "cond1/2/3" | "" (auto)
    days_list: list[int] = params.get("days", [])
    task       = params.get("task", "experiment")
    sub        = params.get("sub", "VOR / OKR")
    control_q  = params.get("control", "")
    per_day    = int(params.get("per_day", 4))
    project    = params.get("project", "")
    cond_date_override = params.get("cond_date", "")
    filter_eligible = bool(params.get("filter_eligible", True))
    no_animals      = bool(params.get("no_animals", False))   # skip DB lookup; placeholder events
    end_date_override = (params.get("end_date") or "").strip()

    # In absolute start-date mode, days_list defaults to [0] (single round starting on start_date).
    if (ref == "start" or params.get("start_date")) and not days_list:
        days_list = [0]

    # If user gave an end_date, build a consecutive-day range from start_date..end_date.
    if (ref == "start" or params.get("start_date")) and end_date_override:
        try:
            s = date.fromisoformat((params.get("start_date") or "").strip())
            e = date.fromisoformat(end_date_override)
            if e >= s:
                days_list = list(range((e - s).days + 1))
        except Exception:
            pass

    if not cohort_q or not days_list:
        return "❌ Missing required params. " + SCHEDULE_HELP

    # Fetch mice — unless user explicitly says animals aren't in the DB yet
    if no_animals:
        exp_mice, ctrl_mice = [], []
    else:
        exp_mice  = get_animals(cohort=cohort_q, condition=condition if condition else "")
        ctrl_mice = get_animals(cohort=control_q) if control_q else []

        if not exp_mice and not ctrl_mice:
            total = len(get_animals())
            hint = (f"\n_DB에 전체 {total}마리 있음. `animals cohort={cohort_q}` 로 확인해보세요._"
                    f"\n_동물이 아직 DB에 없으면 '동물 db에 없음' / 'no animals' 키워드를 포함하세요._"
                    if total else "\n_DB가 비어있어요. 먼저 Excel sync를 해주세요._")
            cond_info = f" (condition={condition})" if condition else ""
            return f"❌ *{cohort_q}* 코호트 동물을 찾지 못했어요{cond_info}.{hint}"

    # Apply behavior eligibility filter (drops ePhys-reassigned + 'excluded' notes)
    # Skip filter if the task itself is ephys/surgery/histology.
    task_lower = (task or "").lower()
    skip_filter = any(k in task_lower for k in ("ephys", "surgery", "수술", "histology", "조직", "perfus"))
    excluded_ids: list[str] = []
    if filter_eligible and not skip_filter:
        excluded_ids = [m.get("mouse_id", "") for m in (exp_mice + ctrl_mice)
                        if not _is_eligible_for_behavior(m)]
        exp_mice  = [m for m in exp_mice  if _is_eligible_for_behavior(m)]
        ctrl_mice = [m for m in ctrl_mice if _is_eligible_for_behavior(m)]

    # Resolve reference date column.
    # Priority: explicit ref="dos"/"cond1/2/3"/"start" → cond_ref → condition→column → date_cond1.
    start_date_override = (params.get("start_date") or "").strip()
    if ref == "start" or start_date_override:
        # Absolute start date mode: schedule sequentially starting from start_date.
        # days_list is interpreted as ROUND offsets from start_date (default [0]).
        cond_col = "__start__"
    elif ref == "dos":
        cond_col = "dos"
    elif ref in ("cond1", "cond-1", "1"):
        cond_col = "date_cond1"
    elif ref in ("cond2", "cond-2", "2"):
        cond_col = "date_cond2"
    elif ref in ("cond3", "cond-3", "3"):
        cond_col = "date_cond3"
    elif cond_ref:
        cond_col = COND_REF_COL.get(cond_ref, "date_cond1")
    elif condition and condition in CONDITION_COND_MAP:
        cond_col = CONDITION_COND_MAP[condition]
    else:
        cond_col = "date_cond1"

    def get_entry_date(m: dict) -> Optional[date]:
        if cond_col == "__start__":
            try: return date.fromisoformat(start_date_override)
            except Exception: return None
        if cond_date_override:
            try: return date.fromisoformat(cond_date_override)
            except Exception: pass
        v = m.get(cond_col)
        if v:
            try: return date.fromisoformat(v[:10])
            except Exception: pass
        return None

    # Build schedule: for each day offset, assign per_day mice (mix exp+ctrl)
    created_events: list[str] = []
    errors: list[str] = []

    all_mice = exp_mice + ctrl_mice
    # Sort mice by mouse_id for deterministic assignment
    all_mice.sort(key=lambda m: m.get("mouse_id", ""))

    # Group mice by their condition entry date (mice with same entry date share schedule)
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    ungrouped: list[dict] = []
    for m in all_mice:
        ed = get_entry_date(m)
        if ed:
            groups[ed.isoformat()].append(m)
        else:
            ungrouped.append(m)

    if not groups and ungrouped:
        return (f"⚠️ Found {len(ungrouped)} mice but none have a condition entry date "
                f"(column: `{cond_col}`). Set dates in your Excel and sync.")

    # No-animals mode: synthesize a single placeholder "group" from start_date so
    # the regular day-offset loop still runs (with empty mouse lists).
    if no_animals and not groups:
        anchor = start_date_override or date.today().isoformat()
        groups[anchor] = []  # single empty group; entry_date used for D+0 anchor

    total_scheduled = 0
    schedule_lines: list[str] = []

    for entry_iso in sorted(groups.keys()):
        group_mice = groups[entry_iso]
        entry_date = date.fromisoformat(entry_iso)

        for day_offset in days_list:
            target_date = entry_date + timedelta(days=day_offset)

            # no_animals mode: single placeholder event per day (no batches, no mice).
            if no_animals:
                batches = [[]]
            else:
                # Split ALL group mice into per_day batches, spread across ±5 days of target
                batches = [group_mice[i:i+per_day] for i in range(0, len(group_mice), per_day)]

            for batch_idx, batch in enumerate(batches):
                # Spread: 0→target, 1→target+1, 2→target+2, ...
                # If we exceed +5, wrap back to target-1, target-2, ... (stay within ±5)
                if batch_idx <= 5:
                    sched_date = target_date + timedelta(days=batch_idx)
                else:
                    sched_date = target_date - timedelta(days=(batch_idx - 5))

                # Label control-cohort mice with "con-" prefix so they're visually
                # distinct on event cards (e.g. "con-WT3119" vs "WT3547").
                mouse_ids = [_label_mouse(m) for m in batch]
                if not mouse_ids and not no_animals:
                    continue

                if no_animals:
                    if len(days_list) > 1:
                        title = f"{task} – {cohort_q} – day {day_offset+1}/{len(days_list)}"
                    else:
                        title = f"{task} – {cohort_q}"
                else:
                    title = f"{task} – {cohort_q} D+{day_offset}"
                cat, sub_default, sub2_default = _resolve_category(task)
                ev_sub  = sub_default if sub_default else ("Behavior" if ("okr" in task.lower() or "vor" in task.lower()) else sub)
                ev_sub2 = sub2_default if sub2_default else (sub if ("okr" in task.lower() or "vor" in task.lower()) else "")
                ref_label = ("surgery (dos)" if cond_col == "dos"
                             else f"{condition} entry" if condition
                             else cond_col)
                if no_animals:
                    note_lines = [f"Auto-scheduled: {task} for {cohort_q}. "
                                  f"Animals not in DB — register later and update event."]
                else:
                    note_lines = [f"Auto-scheduled: {task} at D+{day_offset} after {ref_label} ({entry_iso})."]
                    if control_q:
                        note_lines.append(f"Control cohort: {control_q}")
                    if mouse_ids:
                        note_lines.append(f"Mice: {', '.join(mouse_ids)}")
                    if excluded_ids:
                        note_lines.append(f"Excluded (ePhys/dropped): {excluded_ids}")
                ev = {
                    "id": _uid(),
                    "title": title,
                    "category": cat,
                    "subcategory": ev_sub,
                    "subcategory2": ev_sub2,
                    "cohort": cohort_q,
                    "project_slug": project,
                    "status": "planned",
                    "start": f"{sched_date.isoformat()}T09:00:00",
                    "end": f"{sched_date.isoformat()}T12:00:00",
                    "all_day": False,
                    "notes": "\n".join(note_lines),
                    "mice": mouse_ids,
                    "recur": None,
                    "assignee": "",
                    "delay_reason": "",
                    "reschedule_to": "",
                }
                try:
                    create_event(ev)
                    total_scheduled += 1
                    offset_str = f"D+{day_offset}" + (f"+{batch_idx}" if batch_idx > 0 else "")
                    line_tail = ', '.join(mouse_ids) if mouse_ids else "(no animals — placeholder)"
                    schedule_lines.append(
                        f"  • {sched_date.strftime('%m/%d')} {offset_str}: {line_tail}"
                    )
                except Exception as e:
                    errors.append(f"  ✗ {sched_date} D+{day_offset}: {e}")

    if not total_scheduled:
        msg = "❌ No events were created."
        if errors:
            msg += "\n" + "\n".join(errors[:5])
        return msg

    lines = [
        f"✅ *Auto-scheduled {total_scheduled} events*",
        f"Cohort: `{cohort_q}` | Task: `{task}` | Days: {days_list}",
        f"Mice per day: {per_day} | Project: {project or '(none)'}",
        "",
    ] + schedule_lines[:20]
    if len(schedule_lines) > 20:
        lines.append(f"  … and {len(schedule_lines)-20} more")
    if errors:
        lines.append("\n⚠️ Some errors:")
        lines.extend(errors[:3])
    lines.append(f"\n→ <{SCHEDULER_API}/scheduler.html|Open Scheduler>")
    return "\n".join(lines)

# ---------- Natural language parser ----------
# ---------- Rule-based NL parser ----------

# Condition aliases — protocol GROUP LABELS only (not cond-N references)
# "cond-1/2/3" = date reference number, handled separately by _extract_cond_ref()
_COND_ALIASES: dict[str, str] = {
    "dt": "DT", "dark treatment": "DT", "dark-treated": "DT", "다크": "DT",
    "dlt": "DLT", "dark-light": "DLT", "darklight": "DLT",
    "nr": "NR", "normal rearing": "NR", "노말": "NR",
    "23dmr": "23dmr", "dim23": "23dmr", "dim 23": "23dmr",
    "24dmr": "24dmr", "dim24": "24dmr", "dim 24": "24dmr",
}

def _extract_condition(text: str) -> str:
    """Extract protocol group label (DT / NR / DLT). Does NOT match cond-N numbers.
    Uses word boundaries so 'DLTA' / 'DLT-A' don't get matched as condition='DLT'."""
    tl = text.lower()
    for alias, code in sorted(_COND_ALIASES.items(), key=lambda x: -len(x[0])):
        # Require non-word boundary (or start/end of string) on both sides
        if re.search(rf'(?:^|[^\w]){re.escape(alias)}(?:$|[^\w])', tl):
            return code
    return ""

# cond_ref: which transition date to use as scheduling reference
# "cond-1 이후" / "date in cond-1" → 1  →  date_cond1
# "cond-2 이후" / "date in cond-2" → 2  →  date_cond2
COND_REF_COL = {1: "date_cond1", 2: "date_cond2", 3: "date_cond3"}

def _extract_cond_ref(text: str) -> int:
    """Extract cond reference number (1/2/3) from 'cond-2 이후', 'date in cond-1' etc."""
    tl = text.lower()
    # "date in cond-2", "cond-2 이후", "cond2 이후", "컨디션2 이후"
    m = re.search(r'(?:date\s+in\s+)?(?:cond(?:ition)?[-_\s]?|컨디션\s?)([123])', tl)
    if m:
        return int(m.group(1))
    return 0  # 0 = not specified → fall back to cond1

def _extract_days(text: str) -> list[int]:
    """Extract day numbers from text like '20, 40, 60일' or '20d 40d 60d'.
    Excludes numbers that are part of per-day counts (e.g. '3-5마리', '하루 4마리')."""
    # Remove per-day and cond-N patterns before parsing
    clean = re.sub(r'\d+\s*[-~]\s*\d+\s*마리', '', text)        # "3-5마리"
    clean = re.sub(r'하루\s*에?\s*\d+\s*[-~]?\s*\d*\s*마리', '', clean)  # "하루(에) N(-M)마리"
    clean = re.sub(r'\d+\s*마리\s*[/]?\s*(?:day|일|씩|per)?', '', clean) # "5마리씩", "4마리/day"
    clean = re.sub(r'\d+\s*mice?\s*(?:per|/)\s*(?:day|일)', '', clean, flags=re.I)
    clean = re.sub(r'cond[-_]?\d', '', clean, flags=re.I)       # "cond-2", "cond1"
    clean = re.sub(r'condition[-_]?\d', '', clean, flags=re.I)  # "condition2"
    clean = re.sub(r'컨디션\s*\d', '', clean)                    # "컨디션2"
    # Two passes: prefer numbers with explicit day suffix; fall back to bare digits.
    explicit = re.findall(r'(?<!\d)(\d{1,3})\s*(?:일|d(?:ay)?s?)\b', clean)
    bare     = re.findall(r'(?<![\d.])(\d{1,3})(?![\d.])', clean) if not explicit else []
    matches = explicit if explicit else bare
    days = [int(m) for m in matches if 1 <= int(m) <= 365]
    return sorted(set(days)) if days else []

def _extract_per_day(text: str) -> int:
    """Extract mice per day from '하루 3마리', '4마리/day', '3-5마리', '5 mice per day'."""
    # Prefer ranges first ("3-5마리" → avg, "3-5 mice" → avg)
    m = re.search(r'(\d+)\s*[-~]\s*(\d+)\s*(?:마리|mice?)', text, re.I)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (lo + hi) // 2 or lo
    # English "N mice per day" / "N mice/day"
    m = re.search(r'(\d+)\s*mice?\s*(?:per|/)\s*(?:day|일)', text, re.I)
    if m: return int(m.group(1))
    # Korean "하루(에) N(마리)" — but only if not followed by a "-M" range
    m = re.search(r'하루\s*에?\s*(\d+)(?!\s*[-~]\s*\d)', text)
    if m: return int(m.group(1))
    # "N마리/day", "N마리씩"
    m = re.search(r'(\d+)\s*마리\s*(?:/?\s*(?:day|일)|씩)', text)
    if m: return int(m.group(1))
    return 4

def _extract_task(text: str) -> str:
    tl = text.lower()
    for kw in ["okr", "vor", "surgery", "수술", "histology", "조직", "behavior", "행동", "ephys", "이펙스"]:
        if kw in tl:
            return kw.upper() if kw in ("okr","vor","ephys") else kw
    return "experiment"

_KO_PARTICLES = r'(?:이랑|이고|이랑|이에서|이라|이라고|이다|이랑|은|는|이|가|을|를|의|에|로|으로|과|와|도|만|랑|이며)?'

def _strip_ko(s: str) -> str:
    """Strip trailing Korean particles from a word."""
    return re.sub(r'(?:이랑|이고|이라|이다|이에서|은|는|이|가|을|를|의|에|로|으로|과|와|도|만|랑|이며)+$', '', s)

def _extract_cohort_and_control(text: str) -> tuple[str, str]:
    """Return (cohort, control) from text.
    Heuristics:
      • 'X and X-con' / 'X와 X-con' / 'X / X-con' → cohort=X, control=X-con
      • 'control=Y', 'mix with Y', 'Y이랑 섞어서' → control=Y
      • '<name> cohort' / 'cohort=<name>' → cohort  (ignores generic words like 'animal')
    """
    GENERIC = {"animal","animals","mouse","mice","쥐","동물","day","days","date","dt","dlt","cohort","코호트"}
    cohort = ""
    control = ""

    # 1. Auto-pair X + X-con/X-control
    # Matches "DLTA and DLTA-con", "dA / dA-con", "dB와 dB-con", "dA + dA-con",
    # "dA cohort + dA-con", "dA 코호트 + dA-con" etc.
    m_pair = re.search(
        r'([A-Za-z][\w]{0,12})'
        r'(?:\s*(?:cohort|코호트|그룹|group))?'
        r'\s*(?:[\s/+,]+(?:and|와|과|및|,)?)\s*'
        r'\1[\-_](?:con|ctrl|control)\b',
        text, re.I)
    if m_pair:
        base = m_pair.group(1)
        cohort = base
        control = f"{base}-con"  # canonical DB form

    # 2. Explicit control= / mix with / 섞어서
    if not control:
        m = re.search(r'control\s*[은는=:이]\s*([\w\-_]+)', text, re.I)
        if m: control = _strip_ko(m.group(1))
    if not control:
        m = re.search(r'([\w\-_]+)(?:이랑|랑)\s*섞어', text)
        if m: control = _strip_ko(m.group(1))
    if not control:
        m = re.search(r'mix\s+with\s+([\w\-_]+)', text, re.I)
        if m: control = _strip_ko(m.group(1))

    # 3. cohort name from 'X cohort' / 'cohort X' / 'cohort=X'
    if not cohort:
        m = re.search(r'cohort\s*[=:]\s*([\w\-_]+)', text, re.I)
        if m: cohort = m.group(1)
    if not cohort:
        m = re.search(r'([\w\-_]+)\s+(?:cohort|코호트|그룹|group)\b', text, re.I)
        if m and m.group(1).lower() not in GENERIC:
            cohort = m.group(1)
    if not cohort:
        m = re.search(r'(?:cohort|코호트)\s+([\w\-_]+)', text, re.I)
        if m and m.group(1).lower() not in GENERIC:
            cohort = m.group(1)

    # 4. Standalone cohort-like token (dA, DLTA, H-con, etc.)
    if not cohort:
        for m in re.finditer(r'\b([A-Z][A-Za-z0-9_\-]{0,12}|d[A-Za-z0-9_\-]{1,12})\b', text):
            cand = m.group(1)
            if cand.lower() in GENERIC: continue
            if cand.lower() in ("okr","vor","ephys","nr","dt","dlt","dlta"):
                if cand.lower() == "dlta": cohort = cand; break
                continue
            cohort = cand
            break

    return cohort, control

def _extract_mouse_query(text: str) -> str:
    """Extract a mouse ID like WT-3723."""
    m = re.search(r'\b([A-Za-z]{1,4}[-_]?\d{3,6})\b', text)
    return m.group(1) if m else ""

def parse_nl(text: str) -> dict:
    """Rule-based Korean/English NL parser → action dict."""
    t  = text.strip()
    tl = t.lower()

    # ── TODAY ─────────────────────────────────────────────
    if re.search(r'오늘|today|오늘\s*일정', tl):
        # Don't confuse "오늘 스케쥴링해줘" with today-brief
        if not re.search(r'스케쥴|schedule|등록|추가|만들', tl):
            return {"action": "today"}

    # ── WEEK ──────────────────────────────────────────────
    if re.search(r'이번\s*주|주간|this\s*week|\bweek\b', tl):
        if not re.search(r'스케쥴|schedule|등록|추가|만들', tl):
            return {"action": "week"}

    # ── UPCOMING N days ───────────────────────────────────
    m = re.search(r'(?:향후|앞으로|next)\s*(\d+)\s*(?:일|days?)', tl)
    if m:
        return {"action": "upcoming", "days": int(m.group(1))}

    # ── HELP ──────────────────────────────────────────────
    if re.search(r'^(help|도움말|명령어|뭐 할 수 있어|뭐할수있어)$', tl):
        return {"action": "help"}

    # ── STATUS ────────────────────────────────────────────
    if re.search(r'상태|status|시스템|연결됐|연결 됐|연결됩|api.*연결|연결.*api'
                 r'|openai.*됐|잘\s*돼|작동|살아있', tl):
        return {"action": "status"}

    # ── SCHEDULE (auto-create events) ────────────────────
    # Triggers: 스케쥴, schedule, 등록, 일정 만들어, 추가해줘 + day numbers
    has_schedule_verb = re.search(
        r'스케쥴|스케줄|schedule|일정\s*(?:만들|예약|추가|넣)|'
        r'예약해|추가해|넣어줘|등록해|배정|배당|배치|잡아\s*줘',
        tl)
    has_days          = bool(_extract_days(t))
    has_ref_keyword   = bool(re.search(
        r'뒤|후|after|d\+|수술일|수술\s*기준|dos\s*[+\-]?\d*|post[\s-]*surg|'
        r'dark[\s-]*(?:to|→|->)?[\s-]*light|빛으로|light\s*전환|전환|dlt\b|'
        r'cond[\s-]?[123]|컨디션\s?\d|부터|이후|starting|from\s+\d',
        tl))
    has_date_range    = bool(re.search(r'\d{1,2}[/.\-]\d{1,2}\s*(?:-|~|—|–|to|까지)', t))

    if has_schedule_verb or (has_days and has_ref_keyword) or has_date_range:
        cohort, control = _extract_cohort_and_control(t)
        cond_ref        = _extract_cond_ref(t)          # 1/2/3 — which date column
        cond            = _extract_condition(t)          # protocol label filter (DT/NR/etc)
        days            = _extract_days(t)
        task            = _extract_task(t)
        per_day         = _extract_per_day(t)

        # Detect surgery-based reference: "수술일 기준", "수술 후", "dos+N", "post-surgery"
        ref = ""
        start_date = ""
        if re.search(r'수술일|수술\s*(?:날짜|기준|뒤|후|이후)|dos\s*[+\-]?\d*|post[\s-]*surg|after\s+surgery', tl):
            ref = "dos"
        elif re.search(
            r'dark[\s-]*(?:to|→|->)?[\s-]*light|d\s*->\s*l|빛으로|light\s*전환|'
            r'다크[\s-]?(?:to|투|→|->)?[\s-]?라이트|다크라이트|cond[\s-]?2|컨디션\s?2',
            tl):
            ref = "cond2"
        # "일주일 뒤" / "1 week after" with surgery context → default ref to dos if missing days
        if ref == "dos" and not days and re.search(r'일주일|1\s*week|7\s*일', tl):
            days = [7]

        # Absolute date range: "6/12 - 13", "6/12 ~ 6/14", "6/12 - 6/13까지"
        end_date = ""
        m_range = re.search(
            r'(\d{1,2})[/.\-](\d{1,2})\s*(?:-|~|—|–|to|까지)\s*(?:(\d{1,2})[/.\-])?(\d{1,2})',
            t)
        if m_range:
            yr = date.today().year
            try:
                mo1, d1 = int(m_range.group(1)), int(m_range.group(2))
                mo2 = int(m_range.group(3)) if m_range.group(3) else mo1
                d2 = int(m_range.group(4))
                s = date(yr, mo1, d1); e = date(yr, mo2, d2)
                if e >= s:
                    start_date = s.isoformat(); end_date = e.isoformat()
                    ref = "start"; days = []
            except Exception:
                pass

        # Absolute start date: "from 6/6", "6/6부터", "starting 6/6", "6/6 이후"
        if not start_date:
            m_start = re.search(
                r'(?:from|starting|시작|부터|이후로?)\s*(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?'
                r'|(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?\s*(?:부터|이후|시작|에서|에 시작)',
                t, re.I)
            if m_start:
                g = m_start.groups()
                mo = int(g[0] or g[3]); da = int(g[1] or g[4])
                yr_raw = g[2] or g[5]
                yr = int(yr_raw) if yr_raw else date.today().year
                if yr < 100: yr += 2000
                try:
                    start_date = date(yr, mo, da).isoformat()
                    ref = "start"
                    days = []
                except Exception:
                    pass

        # "동물 db에 없음" / "no animals" / "DB에 등록 안" / "without mice"
        no_animals = bool(re.search(
            r'(?:db|디비|database)\s*(?:에)?\s*(?:등록\s*안|없|미등록|not\s*(?:in|reg))'
            r'|동물\s*(?:이|은|는)?\s*(?:db\s*에)?\s*(?:없|미등록|등록\s*안)'
            r'|no\s*(?:animals?|mice)'
            r'|without\s*(?:animals?|mice|db)'
            r'|마우스\s*(?:정보)?\s*없',
            tl))

        # "sampling" / "perfusion" / "TRAP" type tasks
        if re.search(r'\bsampling\b|sample\s*수집|perfusion|perfus|관류', tl):
            task = "sampling"
        elif re.search(r'\btrap\b', tl) and not any(k in tl for k in ('okr','vor','behavior')):
            task = "TRAP"

        # Acclimatization keyword → task
        if re.search(r'acclimat|habituat|adapt|handling', tl):
            task = "acclimatization"

        proj_m  = re.search(r'project[=:\s]+([\w\-_]+)', t, re.I)
        project = proj_m.group(1) if proj_m else ""

        # In absolute-date mode, days is allowed to be empty
        days_ok = bool(days) or ref == "start"
        # In no_animals mode we still need cohort + a date.
        if cohort and days_ok:
            return {
                "action":     "schedule",
                "cohort":     cohort,
                "condition":  cond,
                "cond_ref":   cond_ref,
                "ref":        ref,         # "dos" | "cond1/2/3" | "start" | ""
                "start_date": start_date,  # YYYY-MM-DD when ref="start"
                "end_date":   end_date,    # YYYY-MM-DD when date range given
                "days":       days,
                "task":       task,
                "control":    control,
                "per_day":    per_day,
                "project":    project,
                "no_animals": no_animals,
            }
        # Partial match — ask for missing info
        missing = []
        if not cohort: missing.append("코호트 이름")
        if not days:   missing.append("측정 일정 (예: 20, 40, 60일)")
        return {"action": "unknown",
                "message": f"스케쥴 만들려면 {', '.join(missing)}이 필요해요."}

    # ── CONDITION UPDATE: "WT2595 DLT 6/15/26"  ──────────
    # Pattern: mouse_id + condition code + optional date
    cond_update = re.search(
        r'\b([A-Za-z]{1,4}\d{3,6})\b.*?\b(DT|NR|DLT|23dmr|24dmr)\b'
        r'(?:.*?(\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?))?',
        t, re.I
    )
    if cond_update and re.search(r'옮겼|이동|넣었|cond|transition|업데이트|update', tl):
        mid   = cond_update.group(1).upper()
        cond  = cond_update.group(2).upper()
        raw_d = cond_update.group(3) or ""
        # Parse date → YYYY-MM-DD
        parsed_date = ""
        if raw_d:
            parts = re.split(r'[/\-]', raw_d)
            try:
                if len(parts) == 3:
                    m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
                    y = 2000 + y if y < 100 else y
                    parsed_date = f"{y:04d}-{m:02d}-{d:02d}"
                elif len(parts) == 2:
                    m, d = int(parts[0]), int(parts[1])
                    parsed_date = f"2026-{m:02d}-{d:02d}"
            except Exception:
                pass
        # cond_num: 2 if "cond2"/"date2" in text, else default 2
        cond_num = 3 if re.search(r'cond.?3|date.?3', tl) else 2
        return {"action": "update_condition", "mouse_id": mid,
                "condition": cond, "date": parsed_date, "cond_num": cond_num}

    # ── ANIMALS: specific mouse ID ────────────────────────
    mouse_id = _extract_mouse_query(t)
    if mouse_id:
        return {"action": "animals", "query": mouse_id}

    # ── ANIMALS: cohort/condition filter ──────────────────
    if re.search(r'쥐|동물|mouse|mice|animal|코호트|cohort', tl):
        cohort, _ = _extract_cohort_and_control(t)
        cond      = _extract_condition(t)
        query     = ""
        # plain name search if no cohort extracted
        if not cohort and not cond:
            query = re.sub(r'(쥐|동물|mouse|mice|animal|보여줘|알려줘|목록|정보)', '', t, flags=re.I).strip()
        return {"action": "animals", "cohort": cohort, "condition": cond, "query": query}

    # ── FALLBACK ──────────────────────────────────────────
    return {"action": "unknown",
            "message": "이해하지 못했어요. `help`로 사용 가능한 명령어를 확인해보세요."}


def dispatch(intent: dict, say, client=None, channel: str = "") -> None:
    """Execute an action from a parsed intent dict."""
    action = intent.get("action", "unknown")

    if action == "help":
        say(HELP_TEXT)

    elif action == "today":
        say(build_today_brief())

    elif action == "week":
        say(build_week_brief())

    elif action == "upcoming":
        days = int(intent.get("days", 3))
        say(build_upcoming_brief(days))

    elif action == "schedule":
        params = {
            "cohort":     intent.get("cohort", intent.get("_args", "")),
            "condition":  intent.get("condition", ""),
            "cond_ref":   int(intent.get("cond_ref", 0)),
            "ref":        intent.get("ref", ""),
            "start_date": intent.get("start_date", ""),
            "end_date":   intent.get("end_date", ""),
            "no_animals": bool(intent.get("no_animals", False)),
            "days":       intent.get("days", []),
            "task":       intent.get("task", "experiment"),
            "control":    intent.get("control", ""),
            "per_day":    int(intent.get("per_day", 4)),
            "project":    intent.get("project", ""),
            "cond_date":  intent.get("cond_date", ""),
            "sub":        intent.get("sub", "VOR / OKR"),
        }
        # Also support old key=value format via _args
        if intent.get("_args"):
            params.update(parse_schedule_args(intent["_args"]))
        say(do_auto_schedule(params))

    elif action == "animals":
        query   = intent.get("query",  intent.get("_args", ""))
        cohort  = intent.get("cohort",  "")
        cond    = intent.get("condition", "")
        project = intent.get("project", "")
        animals = get_animals(query=query, condition=cond, cohort=cohort, project=project)
        if not animals:
            say(f"🐭 동물을 찾지 못했어요 (`{query or cohort or cond or '전체'}`)")
        else:
            lines = [f"🐭 *{len(animals)}마리 검색됨*\n"]
            for a in animals[:20]:
                lines.append(fmt_animal(a))
            if len(animals) > 20:
                lines.append(f"_…외 {len(animals)-20}마리_")
            say("\n".join(lines))

    elif action == "status":
        try:
            s = _api("GET", "/api/animals/sync-status")
            ev_count = len(_api("GET", "/api/events").get("events", []))
            last = s.get("last_synced", "")[:16] if s.get("last_synced") else "없음"
            scheduler_ok = "✅ 온라인"
        except Exception:
            s = {}
            ev_count = 0
            last = "없음"
            scheduler_ok = "❌ 오프라인"

        # OpenAI API ping
        if OPENAI_API_KEY:
            try:
                _openai_chat([{"role": "user", "content": "hi"}])
                openai_ok = "✅ 연결됨"
            except Exception as e:
                openai_ok = f"❌ 오류 ({e})"
        else:
            openai_ok = "⚠️ 키 없음"

        say(
            f"*시스템 상태*\n"
            f"• Scheduler API: {scheduler_ok}\n"
            f"• OpenAI API: {openai_ok}\n"
            f"• 이벤트: {ev_count}개\n"
            f"• 동물 DB: {s.get('count', 0)}마리 (마지막 싱크: {last})"
        )

    elif action == "update_condition":
        mid      = intent.get("mouse_id", "")
        cond     = intent.get("condition", "")
        cond_num = int(intent.get("cond_num", 2))
        d        = intent.get("date", "")
        if not mid or not cond:
            say("❌ mouse_id와 condition이 필요해요. 예: `WT2595 DLT 6/15/26`")
            return
        date_col = f"date_cond{cond_num}"
        try:
            with db_conn() as con:
                row = con.execute("SELECT id FROM animals WHERE mouse_id=?", (mid,)).fetchone()
                if not row:
                    say(f"❌ `{mid}` 를 DB에서 찾을 수 없어요.")
                    return
                updates = {"condition": cond}
                if d:
                    updates[date_col] = d
                set_clause = ", ".join(f"{k}=?" for k in updates)
                con.execute(
                    f"UPDATE animals SET {set_clause}, synced_at=? WHERE mouse_id=?",
                    [*updates.values(), now_iso(), mid]
                )
            date_str = f", {date_col}={d}" if d else ""
            say(f"✅ `{mid}` condition → `{cond}`{date_str} 업데이트 완료\n"
                f"_Excel 반영은 다음 sync 시 적용됩니다._")
        except Exception as e:
            say(f"❌ 업데이트 실패: `{e}`")

    # ── 2. Add event ──────────────────────────────────────────────
    elif action == "add_event":
        cat    = (intent.get("category") or "experiments").lower()
        mid    = intent.get("mouse_id", "").strip()
        date_s = intent.get("date", "") or today_str()
        time_s = intent.get("time", "09:00") or "09:00"
        proj   = intent.get("project", "")
        sub    = intent.get("subcategory", "")
        notes  = intent.get("notes", "")
        title  = intent.get("title", "")
        if not title:
            title = f"{cat.capitalize()} – {mid}" if mid else cat.capitalize()
        dur_h  = {"surgery": 2, "behavior": 2, "histology": 3}.get(cat, 1)
        try:
            sh, sm = int(time_s[:2]), int(time_s[3:5]) if len(time_s) > 4 else 0
            eh = sh + dur_h
            start_dt = f"{date_s}T{sh:02d}:{sm:02d}:00"
            end_dt   = f"{date_s}T{eh:02d}:{sm:02d}:00"
        except Exception:
            start_dt = f"{date_s}T09:00:00"
            end_dt   = f"{date_s}T10:00:00"
        ev = {"title": title, "category": cat, "subcategory": sub,
              "start": start_dt, "end": end_dt, "all_day": False,
              "mice": [mid] if mid else [], "project_slug": proj,
              "status": "planned", "notes": notes}
        try:
            result = create_event(ev)
            say(f"✅ Event added: *{result['title']}*\n"
                f"📅 {date_s} {time_s}  🐭 {mid or '—'}  📁 {proj or '—'}")
        except Exception as e:
            say(f"❌ Failed to add event: `{e}`")

    # ── 3. Delete event ───────────────────────────────────────────
    elif action == "delete_event":
        event_id = intent.get("event_id", "")
        date_s   = intent.get("date", "")
        cat      = intent.get("category", "")
        mid      = intent.get("mouse_id", "")
        if event_id:
            try:
                _api("DELETE", f"/api/events/{event_id}")
                say(f"✅ Event `{event_id}` deleted.")
            except Exception as e:
                say(f"❌ Delete failed: `{e}`")
            return
        try:
            all_evs = _api("GET", "/api/events").get("events", [])
        except Exception:
            all_evs = []
        matches = [e for e in all_evs
                   if (not date_s or e.get("start", "").startswith(date_s))
                   and (not cat   or e.get("category", "").lower() == cat.lower())
                   and (not mid   or mid in (e.get("mice") or []))]
        if not matches:
            say("🔍 No matching events found. Try specifying date, category, or mouse ID.")
        elif len(matches) == 1:
            ev = matches[0]
            try:
                _api("DELETE", f"/api/events/{ev['id']}")
                say(f"✅ Deleted: *{ev['title']}* ({ev['start'][:16]})")
            except Exception as e:
                say(f"❌ Delete failed: `{e}`")
        else:
            lines = [f"🔍 Found {len(matches)} events — reply with the ID to delete:"]
            for ev in matches[:6]:
                lines.append(f"• `{ev['id']}` {ev['title']} — {ev['start'][:16]}")
            if len(matches) > 6:
                lines.append(f"_…and {len(matches)-6} more_")
            say("\n".join(lines))

    # ── 4. Cohort status ──────────────────────────────────────────
    elif action == "cohort_status":
        cohort = (intent.get("cohort") or intent.get("_args", "")).strip()
        if not cohort:
            say("Please specify a cohort name. e.g. `dA cohort status`")
            return
        # Include sacrificed animals too
        try:
            with db_conn() as con:
                rows = con.execute(
                    "SELECT * FROM animal_with_state WHERE cohort LIKE ? ORDER BY mouse_id",
                    (f"%{cohort}%",)
                ).fetchall()
            animals_all = [dict(r) for r in rows]
        except Exception as e:
            say(f"❌ DB error: `{e}`"); return
        if not animals_all:
            say(f"🐭 No animals found in cohort `{cohort}`."); return
        from collections import Counter
        active  = [a for a in animals_all if (a.get("sacrificed") or "").upper() != "Y"]
        sac     = [a for a in animals_all if (a.get("sacrificed") or "").upper() == "Y"]
        states  = Counter(a.get("current_state", "NR") for a in active)
        today   = today_str()
        all_mids = [a["mouse_id"] for a in active]
        try:
            evs = _api("GET", "/api/events").get("events", [])
            upcoming = [e for e in evs
                        if e.get("start", "") >= today
                        and any(m in (e.get("mice") or []) for m in all_mids)][:5]
        except Exception:
            upcoming = []
        lines = [f"🐭 *Cohort {cohort}* — {len(active)} active / {len(sac)} sacrificed"]
        lines.append("State: " + "  ".join(f"*{s}* {n}" for s, n in states.most_common()))
        lines.append("")
        for a in active:
            st = a.get("current_state", "NR")
            d  = a.get("days_in_state")
            lines.append(f"• `{a['mouse_id']}` {'♂' if a.get('sex')=='M' else '♀' if a.get('sex')=='F' else '?'} — *{st}*{f' ({d}d)' if d is not None else ''}")
        if upcoming:
            lines.append("")
            lines.append(f"📅 *Upcoming events:*")
            for e in upcoming:
                lines.append(f"• {e['start'][:10]}  {e['title']}")
        say("\n".join(lines))

    # ── 5. Sacrifice mouse ────────────────────────────────────────
    elif action == "sacrifice_mouse":
        mid  = intent.get("mouse_id", "").strip()
        date = intent.get("date", "") or today_str()
        if not mid:
            say("❌ Mouse ID required. e.g. `WT3546 sacrificed today`"); return
        try:
            _api("PUT", f"/api/animals/{mid}", {"sacrificed": "Y", "sacrificed_date": date})
            say(f"🪦 `{mid}` marked as sacrificed ({date})")
        except Exception as e:
            say(f"❌ Update failed: `{e}`")

    # ── 6. Mark ePhys ─────────────────────────────────────────────
    elif action == "mark_ephys":
        mid   = intent.get("mouse_id", "").strip()
        date  = intent.get("date", "") or today_str()
        notes = intent.get("notes", "")
        if not mid:
            say("❌ Mouse ID required. e.g. `WT3546 went to ePhys`"); return
        try:
            with db_conn() as con:
                if not con.execute("SELECT 1 FROM animals WHERE mouse_id=?", (mid,)).fetchone():
                    say(f"❌ `{mid}` not found in DB."); return
                now = now_iso()
                existing = con.execute("SELECT 1 FROM ephys_logs WHERE mouse_id=?", (mid,)).fetchone()
                if existing:
                    con.execute("UPDATE ephys_logs SET exp_date=?, memo=?, updated_at=? WHERE mouse_id=?",
                                (date, notes, now, mid))
                else:
                    con.execute("INSERT INTO ephys_logs (mouse_id, exp_date, memo, created_at, updated_at) VALUES (?,?,?,?,?)",
                                (mid, date, notes, now, now))
                cur = (con.execute("SELECT notes FROM animals WHERE mouse_id=?", (mid,)).fetchone()[0] or "").strip()
                tag = f"ePhys {date}" + (f" — {notes}" if notes else "")
                con.execute("UPDATE animals SET notes=?, synced_at=? WHERE mouse_id=?",
                            ((cur + "\n" + tag).strip(), now, mid))
            say(f"⚡ `{mid}` logged for ePhys ({date}){f'  {notes}' if notes else ''}")
        except Exception as e:
            say(f"❌ Failed: `{e}`")

    # ── 7. Cohort report (CSV file upload) ────────────────────────
    elif action == "cohort_report":
        import io, csv as _csv
        cohort = (intent.get("cohort") or intent.get("_args", "")).strip()
        try:
            qs = f"?cohort={cohort}" if cohort else ""
            data = _api("GET", f"/api/animals/cohort-report{qs}")
        except Exception as e:
            say(f"❌ Report failed: `{e}`"); return
        active   = data.get("active", [])
        archived = data.get("archived", [])
        all_rows = [dict(a, _status="active")   for a in active] + \
                   [dict(a, _status="archived") for a in archived]
        if not all_rows:
            say(f"🐭 No data found for cohort `{cohort}`."); return
        fields = ["_status","mouse_id","strain","sex","project","cohort","dob","dos",
                  "condition","date_cond1","days_cond1","date_cond2","days_cond2",
                  "date_cond3","days_cond3","current_state","days_in_state",
                  "sacrificed","sacrificed_date","notes","barcode"]
        buf = io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(all_rows)
        csv_bytes = ("﻿" + buf.getvalue()).encode("utf-8")
        fname   = f"cohort_{cohort or 'all'}_{today_str()}.csv"
        summary = (f"📊 *Cohort {cohort or 'all'} Report*\n"
                   f"Active: {len(active)}  ·  Archived: {len(archived)}  ·  Total: {len(all_rows)}")
        if client and channel:
            try:
                client.files_upload_v2(channel=channel, content=csv_bytes,
                                       filename=fname, initial_comment=summary)
                return
            except Exception as e:
                print(f"[cohort_report] file upload failed: {e}")
        say(summary + f"\n_Attach failed — {len(all_rows)} rows generated. Restart bot to enable uploads._")

    # ── 8. Archive cohort (move sacrificed → archive table) ───────
    elif action == "archive_cohort":
        cohort = (intent.get("cohort") or intent.get("_args", "")).strip()
        if not cohort:
            say("Please specify a cohort. e.g. `archive cohort dA`"); return
        try:
            with db_conn() as con:
                rows = con.execute(
                    "SELECT mouse_id FROM animals WHERE cohort LIKE ? AND sacrificed='Y'",
                    (f"%{cohort}%",)
                ).fetchall()
            candidates = [r[0] for r in rows]
        except Exception as e:
            say(f"❌ DB error: `{e}`"); return
        if not candidates:
            say(f"📦 No sacrificed animals in cohort `{cohort}` to archive."); return
        ok, failed = [], []
        for mid in candidates:
            try:
                _api("POST", f"/api/animals/{mid}/archive", {"reason": "sacrificed"})
                ok.append(mid)
            except Exception as e:
                failed.append(f"{mid}: {e}")
        msg = f"📦 *Archive complete — cohort {cohort}*\n✅ Archived ({len(ok)}): {', '.join(ok)}"
        if failed:
            msg += f"\n❌ Failed: {'; '.join(failed)}"
        say(msg)

    # ── 9. Reschedule: delete matching events + re-run schedule ──────
    elif action == "reschedule":
        cohort   = (intent.get("cohort") or "").strip()
        task_kw  = (intent.get("task") or "").lower()
        new_days = intent.get("new_days") or []
        cond_ref = int(intent.get("cond_ref") or 0)
        project  = intent.get("project", "")

        if not new_days:
            say("❌ New day offsets required. e.g. `reschedule OKR to 30, 60 days`")
            return

        # Find matching events
        try:
            all_evs = _api("GET", "/api/events").get("events", [])
        except Exception as e:
            say(f"❌ Could not fetch events: `{e}`"); return

        def _ev_matches(ev):
            if cohort and cohort.lower() not in (ev.get("cohort") or "").lower():
                return False
            if task_kw:
                haystack = " ".join([
                    ev.get("title", ""), ev.get("subcategory", ""),
                    ev.get("subcategory2", ""), ev.get("category", "")
                ]).lower()
                if task_kw not in haystack:
                    return False
            return True

        matches = [e for e in all_evs if _ev_matches(e)]
        if not matches:
            tip = f"cohort=`{cohort}` " if cohort else ""
            say(f"🔍 No events found matching {tip}task=`{task_kw or '(all)'}`. Nothing changed."); return

        # Infer cohort from events if not provided
        if not cohort:
            cohorts = list({e.get("cohort", "") for e in matches if e.get("cohort")})
            if len(cohorts) == 1:
                cohort = cohorts[0]

        # Delete matching events
        deleted, failed = [], []
        for ev in matches:
            try:
                _api("DELETE", f"/api/events/{ev['id']}")
                deleted.append(ev["id"])
            except Exception as e:
                failed.append(str(e))

        # Re-run schedule with new days
        sched_result = ""
        if cohort:
            params = {
                "cohort":    cohort,
                "condition": "",
                "cond_ref":  cond_ref,
                "days":      new_days,
                "task":      task_kw or "experiment",
                "control":   "",
                "per_day":   4,
                "project":   project,
                "sub":       "VOR / OKR" if "okr" in task_kw else task_kw,
            }
            sched_result = do_auto_schedule(params)

        msg = (f"🔄 *Reschedule complete*\n"
               f"• Deleted {len(deleted)} old events\n"
               f"• New schedule: `{', '.join(str(d) for d in new_days)}` days")
        if failed:
            msg += f"\n⚠️ {len(failed)} deletion(s) failed"
        if sched_result:
            msg += f"\n\n{sched_result}"
        say(msg)

    elif action == "unknown":
        say(f"🤔 {intent.get('message', '이해하지 못했어요.')} `help`로 명령어 목록을 확인하세요.")

    else:
        say(f"🤔 이해하지 못했어요. `help`로 명령어 목록을 확인하세요.")


# ---------- Help ----------
HELP_TEXT = """🤖 *Lab Assistant*
_Type naturally in Korean or English_

*📅 Schedule*
• `What's today's schedule?` / `오늘 일정 알려줘`
• `How does this week look?` / `이번주 어때?`
• `Add surgery for WT001 on June 3 at 9am` — add event
• `Delete the behavior event on June 5` — delete event
• `Change OKR schedule to 30, 60 days` — reschedule (delete old → recreate)
• `dA cohort OKR 20/40/60일을 30/60일로 바꿔줘`

*🐭 Animals*
• `Show dB_control cohort mice` / `dB_control 코호트 쥐들 보여줘`
• `WT2595 info` / `WT2595 정보`
• `WT2595 moved to DLT on 6/15/26` — condition update
• `Sacrifice WT001` — mark as sacrificed
• `Mark WT001 ePhys done` — log electrophysiology

*📋 Reports*
• `Show dA cohort schedule` — upcoming events for cohort
• `Cohort report for dA` — download CSV (active + archived)
• `Archive dA cohort` — move sacrificed mice to archive

*ℹ️ Other*
• `System status` / `시스템 상태`
• `help`

_Upload a cage card photo (+ project name) to register animals directly_
_Shortcuts: `today` `this week` `animals` `status` `help`_
"""

# ---------- Slack app ----------
def run_bot() -> None:
    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
        print("ERROR: SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set in .env")
        print("See .env.example for setup instructions.")
        return

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        print("ERROR: slack_bolt not installed. Run: pip install slack-bolt")
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        has_scheduler = True
    except ImportError:
        has_scheduler = False
        print("WARNING: apscheduler not installed — scheduled briefings disabled.")
        print("Run: pip install apscheduler")

    app = App(token=SLACK_BOT_TOKEN)

    # Store bot's own user ID to avoid responding to itself
    bot_id: Optional[str] = None
    try:
        result = app.client.auth_test()
        bot_id = result["user_id"]
        bot_channel = result.get("user_id")  # DM channel = user_id for self
        print(f"Bot connected as: {result.get('user')} (id={bot_id})")
    except Exception as e:
        print(f"Auth failed: {e}")
        return

    def get_dm_channel(user_id: str) -> str:
        result = app.client.conversations_open(users=user_id)
        return result["channel"]["id"]

    def _send_brief(text: str) -> None:
        """Send a brief to the bot owner's DM."""
        try:
            channel = get_dm_channel(bot_id)
            app.client.chat_postMessage(channel=channel, text=text)
        except Exception as e:
            print(f"Failed to send brief: {e}")

    # ---------- Pending OCR confirmations ----------
    # {"channel:user_id": {"animals": [...], "step": "confirm"|"ask_project", "projects": [...]}}
    _pending_ocr: dict[str, dict] = {}

    def _get_project_list() -> list[str]:
        """Return available project names from custom_projects + known aliases."""
        try:
            with db_conn() as con:
                rows = con.execute("SELECT name FROM custom_projects ORDER BY name").fetchall()
                return [r[0] for r in rows]
        except Exception:
            return []

    def _extract_project_from_text(text: str) -> str:
        """Extract a project name hint from user's message text."""
        if not text:
            return ""
        tl = text.lower()
        # Explicit: "project=TeMP_dark", "project: TeMP_dark"
        m = re.search(r'project\s*[=:]\s*([\w_\-]+)', tl)
        if m:
            return m.group(1)
        # Known project names appearing anywhere in text
        projects = _get_project_list()
        for p in sorted(projects, key=len, reverse=True):
            if p.lower() in tl:
                return p
        # Common shorthands
        if re.search(r'\btemp[_\s]?dark\b', tl):  return "TeMP_dark"
        if re.search(r'\btemp[_\s]?tun', tl):     return "TeMP_tuning"
        return ""

    # ---------- Image OCR helpers ----------
    def _download_slack_file(url: str, dest: str) -> None:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
            f.write(resp.read())

    def _detect_format(path: str) -> str:
        """Detect actual image format from magic bytes."""
        try:
            with open(path, "rb") as f:
                header = f.read(16)
            if header[:2] == b'\xff\xd8':
                return "jpeg"
            if header[:4] == b'\x89PNG':
                return "png"
            # HEIC/HEIF: ftyp box at offset 4
            if header[4:8] in (b'ftyp', b'mif1', b'msf1'):
                ftyp = header[8:12]
                if ftyp in (b'heic', b'heix', b'hevc', b'hevx', b'mif1'):
                    return "heic"
                return "heic"  # treat any ftyp as heic
            if header[:4] == b'GIF8':
                return "gif"
            if header[:4] in (b'MM\x00*', b'II*\x00'):
                return "tiff"
        except Exception:
            pass
        return "unknown"

    def _resize_for_ocr(src: str) -> str:
        """Downscale image to max 2000px longest side using sips. Returns path to resized file."""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            out_path = tmp.name
        r = subprocess.run(
            ["sips", "-Z", "2000", "-s", "format", "jpeg", src, "--out", out_path],
            capture_output=True,
        )
        if r.returncode == 0 and os.path.getsize(out_path) > 0:
            size_orig = os.path.getsize(src)
            size_new  = os.path.getsize(out_path)
            print(f"[OCR] resized {size_orig//1024}KB → {size_new//1024}KB")
            return out_path
        try: os.unlink(out_path)
        except Exception: pass
        return src  # fallback to original if sips fails

    def _to_png(src: str) -> str | None:
        """Convert image to PNG using sips (with local copy) or qlmanage fallback."""
        import shutil as _shutil

        # Copy to a uniquely-named tmp file so sips can access it (sandbox bypass)
        with tempfile.NamedTemporaryFile(suffix=Path(src).suffix, delete=False) as tmp:
            local_copy = tmp.name
        try:
            _shutil.copy2(src, local_copy)
        except Exception as e:
            print(f"[OCR] copy failed: {e}")
            try: os.unlink(local_copy)
            except Exception: pass
            local_copy = src

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            out_path = tmp.name

        # Try sips
        r = subprocess.run(
            ["sips", "-s", "format", "png", local_copy, "--out", out_path],
            capture_output=True,
        )
        print(f"[OCR] sips rc={r.returncode} stderr={r.stderr[:200]}")
        if r.returncode == 0 and os.path.getsize(out_path) > 0:
            if local_copy != src:
                try: os.unlink(local_copy)
                except Exception: pass
            return out_path

        try: os.unlink(out_path)
        except Exception: pass

        # Try qlmanage (QuickLook — handles any macOS format)
        ql_dir = tempfile.mkdtemp()
        try:
            r2 = subprocess.run(
                ["qlmanage", "-t", "-s", "2048", "-o", ql_dir, local_copy],
                capture_output=True,
            )
            print(f"[OCR] qlmanage rc={r2.returncode}")
            candidates = [f for f in os.listdir(ql_dir) if f.endswith(".png")]
            if candidates:
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as final:
                    final_path = final.name
                _shutil.copy2(os.path.join(ql_dir, candidates[0]), final_path)
                if local_copy != src:
                    try: os.unlink(local_copy)
                    except Exception: pass
                return final_path
        finally:
            _shutil.rmtree(ql_dir, ignore_errors=True)

        if local_copy != src:
            try: os.unlink(local_copy)
            except Exception: pass
        return None

    def _handle_image(file_info: dict, say, ocr_key: str, project_override: str = "") -> None:
        try:
            from ocr_engine import ocr_image
            from cage_card_parser import parse_ocr_lines, fmt_parsed_animal
        except ImportError as e:
            say(f"⚠️ OCR 모듈 로드 실패: {e}")
            return

        url = file_info.get("url_private_download") or file_info.get("url_private", "")
        if not url:
            say("⚠️ 파일 URL을 가져올 수 없어요.")
            return

        name = file_info.get("name", "image")
        ext  = Path(name).suffix.lower() or ".jpg"

        say(f"🔍 *{name}* 분석 중...")

        tmp_path: str = ""
        converted_path: str | None = None
        resized_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp_path = tmp.name
            _download_slack_file(url, tmp_path)

            file_size = os.path.getsize(tmp_path)
            if file_size == 0:
                say("❌ 파일 다운로드 실패 (0 bytes). 다시 시도해주세요.")
                return

            # Detect actual format (Slack may re-encode files)
            actual_fmt = _detect_format(tmp_path)
            print(f"[OCR] {file_size//1024}KB, fmt={actual_fmt}, ext={ext}")

            # Prepare image: convert HEIC → PNG, resize large files
            ocr_path = tmp_path
            if actual_fmt == "heic" or ext in (".heic", ".heif"):
                converted_path = _to_png(tmp_path)
                if converted_path:
                    ocr_path = converted_path
            if file_size > 2_000_000:
                resized = _resize_for_ocr(ocr_path)
                if resized != ocr_path:
                    resized_path = resized
                    ocr_path = resized

            # ── Strategy 1: GPT-4o Vision (sees image directly — best for rotated cards) ──
            animals = parse_with_gpt_vision(ocr_path)
            if animals is not None:
                img_type = "cage_card"
                parser_tag = "👁️ GPT Vision"
                lines: list[str] = []
            else:
                # ── Strategy 2: Apple Vision OCR → GPT text parse → rule-based ──
                lines = ocr_image(ocr_path)
                if not lines:
                    say("⚠️ 텍스트를 인식하지 못했어요. 이미지가 너무 흐리거나 빈 파일일 수 있어요.")
                    return
                gpt_animals = parse_cage_card_with_gpt(lines)
                if gpt_animals is not None:
                    img_type = "cage_card"
                    animals = gpt_animals
                    parser_tag = "🤖 GPT"
                else:
                    img_type, animals = parse_ocr_lines(lines)
                    parser_tag = "📐 Rule"

        except Exception as e:
            say(f"❌ 처리 실패: `{e}`")
            return
        finally:
            for p in (tmp_path, converted_path, resized_path):
                if p:
                    try: os.unlink(p)
                    except Exception: pass

        if not animals:
            if lines:
                raw_text = "\n".join(lines[:15])
                say(f"⚠️ 동물 정보를 파싱하지 못했어요 ({parser_tag}).\n인식된 텍스트:\n```{raw_text}```")
            else:
                say(f"⚠️ 동물 정보를 파싱하지 못했어요 ({parser_tag}).")
            return

        # Apply project override (from text or from cage card parser)
        if project_override:
            for a in animals:
                a["project"] = project_override

        type_label = {"cage_card": "케이지 카드", "db_screenshot": "DB 스크린샷"}.get(img_type, "이미지")
        from cage_card_parser import fmt_parsed_animal

        # Check if any animal is missing a project
        missing_project = any(not a.get("project") for a in animals)

        if missing_project and not project_override:
            # Ask which project before confirming
            projects = _get_project_list()
            _pending_ocr[ocr_key] = {
                "animals": animals,
                "step": "ask_project",
                "projects": projects,
            }
            lines_out = [f"📋 *{type_label}에서 {len(animals)}건 파싱됨* ({parser_tag})\n"]
            for i, a in enumerate(animals, 1):
                lines_out.append(fmt_parsed_animal(a, i))
            lines_out.append("\n❓ *어떤 프로젝트인가요?* 번호로 답하거나 이름을 입력하세요:")
            for i, p in enumerate(projects, 1):
                lines_out.append(f"  `{i}` {p}")
            lines_out.append("\n_또는 다음에 바로 지정: 사진 전송 시 `TeMP_dark 등록해줘` 처럼 입력_")
            say("\n".join(lines_out))
        else:
            diff_lines = _build_ocr_diff(animals)
            _pending_ocr[ocr_key] = {"animals": animals, "step": "confirm"}
            lines_out = [f"📋 *{type_label}에서 {len(animals)}건 파싱됨* ({parser_tag})\n"]
            for i, a in enumerate(animals, 1):
                lines_out.append(fmt_parsed_animal(a, i))
            if diff_lines:
                lines_out.append("\n*DB 변경 예정:*")
                lines_out.extend(diff_lines)
            lines_out.append("\n✅ DB에 업데이트할까요? `yes` / `no`")
            lines_out.append("_수정: `fix mouse_id=WT1234` 또는 `fix project=TeMP_dark`_")
            say("\n".join(lines_out))

    def _apply_ocr_update(animals: list[dict], say) -> None:
        """Delegate to animal_updater.upsert() and report result."""
        try:
            r = _db_upsert(animals)
            say(f"✅ DB 업데이트 완료 — 신규: {r['inserted']}건, 수정: {r['updated']}건"
                + (f", 건너뜀: {r['skipped']}건" if r["skipped"] else ""))
        except Exception as e:
            say(f"❌ DB 업데이트 실패: `{e}`")

    # ---------- Message handler ----------
    @app.event("message")
    def handle_message(event, say):
        print(
            "[slack event] message "
            f"channel={event.get('channel')} "
            f"user={event.get('user')} "
            f"subtype={event.get('subtype')} "
            f"bot_id={event.get('bot_id')} "
            f"text={(event.get('text') or '')[:80]!r}"
        )
        if event.get("bot_id") or event.get("subtype") in ("message_deleted", "message_changed"):
            return

        channel = event.get("channel", "")
        user_id = event.get("user", "")
        ocr_key = f"{channel}:{user_id}"

        text = (event.get("text") or "").strip()

        # ── Image upload ──────────────────────────────────────────
        files = event.get("files", [])
        if files:
            project_hint = _extract_project_from_text(text)
            image_files = [f for f in files
                           if f.get("mimetype","").startswith("image/")
                           or f.get("filetype") in ("jpg","jpeg","png","heic","heif","gif","tiff","bmp")]
            if image_files:
                import threading
                _key = ocr_key  # capture for thread closure
                def _process():
                    for f in image_files:
                        _handle_image(f, say, _key, project_override=project_hint)
                threading.Thread(target=_process, daemon=True).start()
            return

        if not text:
            return

        # ── OCR state machine ─────────────────────────────────────
        if ocr_key in _pending_ocr:
            state_ocr = _pending_ocr[ocr_key]
            step = state_ocr.get("step", "confirm")
            tl = text.strip()

            # ── fix field=value ───────────────────────────────────
            fix_m = re.match(r'^fix\s+(\w+)\s*=\s*(.+)$', tl, re.I)
            if fix_m:
                field, value = fix_m.group(1).lower(), fix_m.group(2).strip()
                if field not in _SAFE_UPDATE_FIELDS and field != "mouse_id":
                    say(f"⚠️ `{field}` 필드는 수정할 수 없어요.")
                    return
                animals = state_ocr["animals"]
                for a in animals:
                    a[field] = value
                diff_lines = _build_ocr_diff(animals)
                from cage_card_parser import fmt_parsed_animal
                lines_out = [f"✏️ `{field}` → `{value}` 수정됨\n"]
                for i, a in enumerate(animals, 1):
                    lines_out.append(fmt_parsed_animal(a, i))
                if diff_lines:
                    lines_out.append("\n*DB 변경 예정:*")
                    lines_out.extend(diff_lines)
                lines_out.append("\n✅ DB에 업데이트할까요? `yes` / `no`")
                state_ocr["step"] = "confirm"
                say("\n".join(lines_out))
                return

            # ── ask_project step ──────────────────────────────────
            if step == "ask_project":
                projects = state_ocr.get("projects", [])
                chosen = ""
                # Number input: "1", "2" ...
                if tl.isdigit():
                    idx = int(tl) - 1
                    if 0 <= idx < len(projects):
                        chosen = projects[idx]
                if not chosen:
                    # Direct name match (case-insensitive partial)
                    for p in projects:
                        if p.lower().startswith(tl.lower()) or tl.lower() in p.lower():
                            chosen = p
                            break
                if not chosen:
                    # Try project extraction from natural text
                    chosen = _extract_project_from_text(tl)
                if not chosen:
                    proj_list = "\n".join(f"  `{i}` {p}" for i, p in enumerate(projects, 1))
                    say(f"❓ 프로젝트를 못 찾았어요. 번호로 입력해주세요:\n{proj_list}")
                    return
                # Apply chosen project to all animals, then show diff
                for a in state_ocr["animals"]:
                    a["project"] = chosen
                state_ocr["step"] = "confirm"
                diff_lines = _build_ocr_diff(state_ocr["animals"])
                from cage_card_parser import fmt_parsed_animal
                lines_out = [f"✅ 프로젝트 `{chosen}` 지정됨\n"]
                for i, a in enumerate(state_ocr["animals"], 1):
                    lines_out.append(fmt_parsed_animal(a, i))
                if diff_lines:
                    lines_out.append("\n*DB 변경 예정:*")
                    lines_out.extend(diff_lines)
                lines_out.append("\n✅ DB에 업데이트할까요? `yes` / `no`")
                lines_out.append("_수정: `fix mouse_id=WT1234`_")
                say("\n".join(lines_out))
                return

            # ── confirm step ──────────────────────────────────────
            tl_lower = tl.lower()
            if tl_lower in ("yes", "y", "네", "응", "ㅇ", "맞아", "ok", "확인"):
                animals = _pending_ocr.pop(ocr_key)["animals"]
                _apply_ocr_update(animals, say)
                return
            elif tl_lower in ("no", "n", "아니", "취소", "cancel"):
                _pending_ocr.pop(ocr_key)
                say("취소했어요.")
                return
            # Fall through to NL handler if not a confirmation keyword

        # ── Standalone cancel (even without pending state) ───────
        if re.match(r'^(no|n|아니|취소|cancel)$', text.strip(), re.I):
            if ocr_key in _pending_ocr:
                _pending_ocr.pop(ocr_key)
                say("취소했어요.")
            else:
                say("취소할 작업이 없어요.")
            return

        # ── Normal NL command ─────────────────────────────────────
        intent = parse_nl(text)
        if intent.get("action") == "unknown" and OPENAI_API_KEY:
            gpt_intent = parse_nl_with_gpt(text)
            if gpt_intent:
                intent = gpt_intent
        dispatch(intent, say, client=app.client, channel=channel)

    # ---------- Scheduled briefings ----------
    # Disabled by default — Majel now handles all scheduled reminders.
    # Set labos_BRIEFINGS=1 to re-enable AZI-3's own briefings.
    briefings_enabled = os.environ.get("labos_BRIEFINGS", "0").lower() in ("1", "true", "yes", "on")
    if has_scheduler and briefings_enabled:
        sched = BackgroundScheduler()

        @sched.scheduled_job(CronTrigger(hour=8, minute=0))
        def daily_morning_brief():
            _send_brief(build_today_brief())

        @sched.scheduled_job(CronTrigger(day_of_week="sun", hour=20, minute=0))
        def weekly_sunday_brief():
            _send_brief(build_week_brief())

        @sched.scheduled_job(CronTrigger(day_of_week="mon", hour=8, minute=30))
        def weekly_monday_brief():
            _send_brief(build_week_brief())

        # 3-day advance notification (runs daily at 8:05am)
        @sched.scheduled_job(CronTrigger(hour=8, minute=5))
        def three_day_advance():
            brief = build_upcoming_brief(3)
            _send_brief(brief)

        sched.start()
        print("Scheduled briefings active: daily 08:00, Mon 08:30, Sun 20:00, 3-day advance 08:05")
    else:
        print("Scheduled briefings disabled (Majel handles reminders). "
              "Set labos_BRIEFINGS=1 to re-enable.")

    # ---------- Polling fallback for channel chat ----------
    # Socket Mode should deliver message.channels events, but Slack app/token
    # migrations can leave that path flaky. This fallback makes AZI-3 behave
    # like PADD: scan recent channel history and dispatch unseen text commands.
    if SLACK_POLL_FALLBACK:
        def _resolve_poll_channels() -> list[str]:
            raw = [x.strip() for x in re.split(r"[,\s]+", SLACK_POLL_CHANNELS) if x.strip()]
            resolved: list[str] = []
            names = [x.lstrip("#").lower() for x in raw if not x.startswith(("C", "G"))]
            ids = [x for x in raw if x.startswith(("C", "G"))]
            resolved.extend(ids)
            if names:
                try:
                    cursor = None
                    for _ in range(10):
                        resp = app.client.conversations_list(types="public_channel", limit=200, cursor=cursor)
                        for ch in resp.get("channels", []):
                            if (ch.get("name") or "").lower() in names:
                                resolved.append(ch["id"])
                        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                        if not cursor:
                            break
                except Exception as e:
                    print(f"[poll] channel resolve failed: {e}")
            return list(dict.fromkeys(resolved))

        def _poll_loop() -> None:
            channels = _resolve_poll_channels()
            print(f"[poll] fallback active channels={channels or '(none)'} interval={SLACK_POLL_INTERVAL}s")
            state = _load_poll_state()
            last_err: dict[str, str] = {}
            err_count: dict[str, int] = {}
            while True:
                try:
                    seen = set(state.get("seen_ts", []))
                    dirty = False
                    for channel_id in channels:
                        try:
                            resp = app.client.conversations_history(channel=channel_id, limit=20)
                        except Exception as e:
                            msg_str = str(e)
                            err_count[channel_id] = err_count.get(channel_id, 0) + 1
                            if last_err.get(channel_id) != msg_str:
                                print(f"[poll] {channel_id} error: {msg_str}")
                                last_err[channel_id] = msg_str
                            elif err_count[channel_id] % 60 == 0:
                                print(f"[poll] {channel_id} still failing ({err_count[channel_id]}x): {msg_str}")
                            continue
                        last_err.pop(channel_id, None)
                        err_count.pop(channel_id, None)
                        messages = sorted(resp.get("messages", []), key=lambda m: float(m.get("ts", "0") or 0))
                        for msg in messages:
                            ts = str(msg.get("ts") or "")
                            if not ts or ts in seen:
                                continue
                            state.setdefault("seen_ts", []).append(ts)
                            seen.add(ts)
                            dirty = True
                            if msg.get("bot_id") or msg.get("subtype"):
                                continue
                            text = (msg.get("text") or "").strip()
                            if not text:
                                continue
                            print(f"[poll event] channel={channel_id} user={msg.get('user')} text={text[:80]!r}")
                            def _say(reply: str, _channel=channel_id) -> None:
                                app.client.chat_postMessage(channel=_channel, text=reply)
                            try:
                                tl = text.lower().strip()
                                if tl.startswith(("invite ", "/invite")):
                                    continue
                                if re.fullmatch(r"(<@[^>]+>\s*)?/?help", text.strip(), re.I):
                                    _say(HELP_TEXT)
                                    continue
                                if re.fullmatch(r"(<@[^>]+>\s*)?/?status", text.strip(), re.I) or "status" in tl or "상태" in tl:
                                    dispatch({"action": "status"}, _say, client=app.client, channel=channel_id)
                                    continue
                                intent = parse_nl(text)
                                # Keep polling responsive: avoid GPT fallback for very short unknown chatter.
                                if intent.get("action") == "unknown" and OPENAI_API_KEY and len(text) >= 8:
                                    gpt_intent = parse_nl_with_gpt(text)
                                    if gpt_intent:
                                        intent = gpt_intent
                                dispatch(intent, _say, client=app.client, channel=channel_id)
                            except Exception as e:
                                print(f"[poll dispatch] error ts={ts}: {e}")
                    if dirty:
                        _save_poll_state(state)
                except Exception as e:
                    print(f"[poll] error: {e}")
                time.sleep(max(2, SLACK_POLL_INTERVAL))

        threading.Thread(target=_poll_loop, daemon=True).start()

    # ---------- Start ----------
    print(f"Starting Slack bot (Socket Mode)...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    run_bot()
