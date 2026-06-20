"""Per-user daily schedule-digest emails (Phase 1: email only).

A scheduled job in the labos app runs each morning and emails each ELN user the
events assigned to them that day. Mapping:

    events.assignee  ->  team_members.id  ->  team_members.eln_user_id  ->  eln_users

Opt-out: eln_users.email_digest = 0. Empty days are skipped (no email). Reuses
modules.core.mailer (SMTP). build_digests() is pure/testable; send_digests() does
the IO and supports dry_run + only_email for safe testing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

LAB_TZ = "America/Los_Angeles"


def lab_today() -> str:
    """Today's date (YYYY-MM-DD) in the lab's timezone. Events are stored as naive
    local (PT) ISO strings, so we compare against the PT calendar date."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(LAB_TZ)).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def build_digests(con, day: str) -> list:
    """[{user_id, email, display_name, events:[...]}] for users with email_digest
    on who have >=1 assigned event on `day` (YYYY-MM-DD)."""
    rows = con.execute(
        """
        SELECT u.id AS uid, u.email AS email, u.display_name AS name,
               e.title AS title, e.start_at AS start_at, e.end_at AS end_at,
               e.category AS category, e.cohort AS cohort
          FROM events e
          JOIN team_members tm ON tm.id = e.assignee
          JOIN eln_users u     ON u.id = tm.eln_user_id
         WHERE u.email IS NOT NULL AND TRIM(u.email) != ''
           AND COALESCE(u.email_digest, 1) = 1
           AND COALESCE(u.active, 1) = 1
           AND substr(e.start_at, 1, 10) = ?
         ORDER BY u.id, e.start_at
        """,
        (day,),
    ).fetchall()
    by_user: dict = {}
    for r in rows:
        d = by_user.setdefault(
            r["uid"],
            {"user_id": r["uid"], "email": r["email"], "display_name": r["name"], "events": []},
        )
        d["events"].append({
            "title": r["title"], "start": r["start_at"], "end": r["end_at"],
            "category": r["category"], "cohort": r["cohort"],
        })
    return list(by_user.values())


def _render(d: dict, day: str) -> tuple:
    n = len(d["events"])
    subject = f"Lab OS — your schedule for {day} ({n} item{'s' if n != 1 else ''})"
    lines = [f"Hi {d['display_name']},", "", f"Your Lab OS schedule for {day}:", ""]
    for ev in d["events"]:
        t = ev["start"][11:16] if ev.get("start") and len(ev["start"]) >= 16 else "—"
        label = ev.get("title") or ev.get("category") or "event"
        row = f"  - {t}  {label}"
        if ev.get("cohort"):
            row += f"  [{ev['cohort']}]"
        lines.append(row)
    lines += ["", "- Lab OS", "(To stop these daily emails, ask your lab admin.)"]
    return subject, "\n".join(lines)


def send_digests(con, *, day: Optional[str] = None, dry_run: bool = False,
                 only_email: Optional[str] = None) -> dict:
    """Build and (unless dry_run) send each user's digest for `day`.

    only_email restricts sending to a single address (safe live testing)."""
    from modules.core import mailer
    day = day or lab_today()
    digests = build_digests(con, day)
    if only_email:
        digests = [d for d in digests if (d["email"] or "").lower() == only_email.lower()]
    sent, errors, preview = 0, [], []
    for d in digests:
        subject, body = _render(d, day)
        preview.append({"email": d["email"], "events": len(d["events"]), "subject": subject})
        if dry_run:
            continue
        try:
            mailer.send_email(d["email"], subject, body)
            sent += 1
        except Exception as e:
            errors.append({"email": d["email"], "error": str(e)[:200]})
    return {"day": day, "recipients": len(digests), "sent": sent,
            "dry_run": dry_run, "errors": errors, "preview": preview}
