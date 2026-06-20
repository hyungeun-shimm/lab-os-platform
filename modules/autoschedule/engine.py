"""Deterministic experiment-scheduling engine for Lab OS.

A caller (the Gemini BYOK parser, an MCP tool, or a test) turns natural language
into a structured ``params`` dict; this module does the actual, verifiable work:
resolve a cohort to real mouse IDs from the ``animals`` table, distribute
N mice/day across a date range, optionally mixing in a control group, and emit
event payloads ready for ``server.upsert_event``.

No LLM, no network — pure logic plus thin read-only DB queries. The distribution
math (``build_schedule`` / ``plan_to_events`` / ``check_conflicts``) is fully
unit-testable in isolation; only ``resolve_cohort_mice`` / ``check_conflicts``
touch a DB connection, and they accept any ``sqlite3.Connection``.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

_SACRIFICED = {"Y", "YES", "1", "TRUE", "T"}


# ── DB reads (thin) ──────────────────────────────────────────────────────────
def resolve_cohort_mice(con, cohort: str, include_sacrificed: bool = False) -> List[str]:
    """Mouse IDs whose cohort matches ``cohort`` (case-insensitive, trimmed)."""
    if not cohort:
        return []
    rows = con.execute(
        "SELECT mouse_id, sacrificed FROM animals "
        "WHERE LOWER(TRIM(cohort)) = LOWER(TRIM(?)) ORDER BY mouse_id",
        (cohort,),
    ).fetchall()
    out: List[str] = []
    for r in rows:
        sac = (r["sacrificed"] or "").strip().upper()
        if not include_sacrificed and sac in _SACRIFICED:
            continue
        out.append(r["mouse_id"])
    return out


# ── pure distribution logic ──────────────────────────────────────────────────
def date_range(date_from: str, date_to: str, weekdays_only: bool = False) -> List[date]:
    d0, d1 = date.fromisoformat(date_from), date.fromisoformat(date_to)
    days, d = [], d0
    while d <= d1:
        if not weekdays_only or d.weekday() < 5:  # Mon–Fri
            days.append(d)
        d += timedelta(days=1)
    return days


def build_schedule(
    *,
    exp_mice: List[str],
    date_from: str,
    date_to: str,
    per_day: int,
    ctrl_mice: Optional[List[str]] = None,
    weekdays_only: bool = False,
    mix_control: bool = False,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Assign mice to days, ``per_day`` each, sequentially.

    When ``mix_control`` and a control group is given, the experimental and
    control mice are pooled and shuffled (seedable for a stable preview) so the
    two groups are randomly interleaved across the schedule.
    """
    if per_day < 1:
        raise ValueError("per_day must be >= 1")
    ctrl_mice = ctrl_mice or []
    pool = list(exp_mice)
    if mix_control and ctrl_mice:
        pool = list(exp_mice) + list(ctrl_mice)
        random.Random(seed).shuffle(pool)

    dates = date_range(date_from, date_to, weekdays_only)
    warnings: List[str] = []
    if not dates:
        return {"assignments": [], "unscheduled": pool, "warnings": ["No dates in range."],
                "n_days": 0, "n_mice": len(pool)}

    assignments: List[Tuple[date, str]] = []
    i = 0
    for d in dates:
        for _ in range(per_day):
            if i >= len(pool):
                break
            assignments.append((d, pool[i]))
            i += 1
    unscheduled = pool[i:]
    if unscheduled:
        capacity = len(dates) * per_day
        warnings.append(
            f"{len(unscheduled)} mice didn't fit "
            f"({len(dates)} days × {per_day}/day = {capacity} slots)."
        )
    if not pool:
        warnings.append("No mice resolved for this cohort.")
    return {"assignments": assignments, "unscheduled": unscheduled,
            "warnings": warnings, "n_days": len(dates), "n_mice": len(pool)}


def plan_to_events(
    assignments: List[Tuple[date, str]],
    *,
    category: str,
    subcategory: str = "",
    subcategory2: str = "",
    cohort: str = "",
    assignee: str = "",
    start_time: str = "09:00",
    duration_min: int = 60,
    project_slug: str = "",
    status: str = "planned",
    location: str = "",
    rig: str = "",
) -> List[Dict[str, Any]]:
    """Turn (date, mouse) pairs into event payloads for ``upsert_event``.

    One event per mouse; same-day mice get back-to-back time slots starting at
    ``start_time``, each ``duration_min`` long.
    """
    h0, m0 = (int(x) for x in start_time.split(":"))
    by_date: Dict[date, List[str]] = {}
    for d, mouse in assignments:
        by_date.setdefault(d, []).append(mouse)

    events: List[Dict[str, Any]] = []
    for d in sorted(by_date):
        slot = datetime(d.year, d.month, d.day, h0, m0)
        for mouse in by_date[d]:
            end = slot + timedelta(minutes=duration_min)
            label = subcategory or category
            events.append({
                "title": f"{label} · {mouse}" if label else mouse,
                "category": category,
                "subcategory": subcategory,
                "subcategory2": subcategory2,
                "cohort": cohort,
                "project_slug": project_slug,
                "status": status,
                "start": slot.isoformat(timespec="minutes"),
                "end": end.isoformat(timespec="minutes"),
                "all_day": False,
                "mice": [mouse],
                "assignee": assignee,
                "location": location,
                "rig": rig,
            })
            slot = end
    return events


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start < b_end and b_start < a_end


def check_conflicts(con, proposed: List[Dict[str, Any]], assignee: str) -> List[Dict[str, Any]]:
    """Flag proposed events that overlap an existing event for the same assignee.

    Read-only — never writes. Compares ISO datetime strings, which sort
    chronologically, so no parsing is needed.
    """
    if not assignee:
        return []
    rows = con.execute(
        "SELECT title, start_at, end_at FROM events WHERE assignee = ?", (assignee,)
    ).fetchall()
    existing = [(r["title"], r["start_at"], r["end_at"]) for r in rows]
    conflicts: List[Dict[str, Any]] = []
    for ev in proposed:
        for (title, s, e) in existing:
            if _overlaps(ev["start"], ev["end"], s, e):
                conflicts.append({
                    "proposed": ev["title"], "at": ev["start"],
                    "conflicts_with": title,
                })
    return conflicts
