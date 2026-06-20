"""Users DAL — unified identity across Scheduler and ELN.

There are two physical tables:
  * `eln_users`     — full identity (auth, role, password, color, initials).
                       Source of truth for sign-in.
  * `team_members`  — lightweight roster used by the Scheduler. Each row may
                       reference an `eln_users.id` via `eln_user_id`.

Use `resolve(con, ident)` to get a unified view from either an eln_user_id, a
team_member_id, a username, or a display_name. The bridge means a Scheduler
event assignee can be either an eln user or a team-only row, and we still get
a consistent dict back.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_name(s: Optional[str]) -> str:
    return (s or "").strip().lower().replace(" ", "").replace("-", "")


# ===========================================================================
# READ — eln_users
# ===========================================================================

def get_eln(con: sqlite3.Connection, user_id: str) -> Optional[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM eln_users WHERE id = ?", (user_id,)
    ).fetchone()


def get_eln_by_username(con: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM eln_users WHERE LOWER(username) = LOWER(?)", (username or "",)
    ).fetchone()


def list_eln_active(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(con.execute(
        "SELECT id, username, display_name, email, role, color, initials"
        " FROM eln_users WHERE active = 1 ORDER BY display_name"
    ).fetchall())


# ===========================================================================
# READ — team_members
# ===========================================================================

def get_team(con: sqlite3.Connection, team_id: str) -> Optional[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM team_members WHERE id = ?", (team_id,)
    ).fetchone()


def list_team(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(con.execute(
        "SELECT * FROM team_members ORDER BY name"
    ).fetchall())


# ===========================================================================
# UNIFIED VIEW
# ===========================================================================

def _as_dict(eln_row: Optional[sqlite3.Row],
             team_row: Optional[sqlite3.Row]) -> Optional[dict]:
    """Combine an eln_users row and a team_members row into a unified dict.
    Falls back to whichever is available.
    """
    if not eln_row and not team_row:
        return None
    e = dict(eln_row) if eln_row else {}
    t = dict(team_row) if team_row else {}
    return {
        "eln_user_id":    e.get("id"),
        "team_member_id": t.get("id"),
        "username":       e.get("username"),
        "display_name":   e.get("display_name") or t.get("name") or "—",
        "email":          e.get("email"),
        "role":           e.get("role") or t.get("role"),
        "color":          e.get("color") or t.get("color"),
        "initials":       e.get("initials"),
        "active":         bool(e.get("active")) if "active" in e else True,
        "has_eln":        bool(eln_row),
        "has_team":       bool(team_row),
    }


def resolve(con: sqlite3.Connection, ident: str) -> Optional[dict]:
    """Look up a user by eln_user_id, team_member_id, username, or display_name.
    Returns a unified dict, or None if nothing matches.
    """
    ident = (ident or "").strip()
    if not ident:
        return None

    # Try eln_users first (id, then username)
    eln_row = get_eln(con, ident) or get_eln_by_username(con, ident)
    if eln_row:
        bridge = con.execute(
            "SELECT * FROM team_members WHERE eln_user_id = ? LIMIT 1", (eln_row["id"],)
        ).fetchone()
        return _as_dict(eln_row, bridge)

    # Try team_members (id, then name-normalized)
    team_row = get_team(con, ident)
    if not team_row:
        team_row = con.execute(
            "SELECT * FROM team_members"
            " WHERE LOWER(REPLACE(REPLACE(name, ' ', ''), '-', ''))"
            "     = LOWER(REPLACE(REPLACE(?, ' ', ''), '-', ''))"
            " LIMIT 1",
            (ident,),
        ).fetchone()
    if team_row:
        eln_row = (
            get_eln(con, team_row["eln_user_id"])
            if team_row["eln_user_id"]
            else None
        )
        return _as_dict(eln_row, team_row)

    return None


def list_unified(con: sqlite3.Connection) -> list[dict]:
    """All known users — union of eln_users (active) and team_members.
    A linked pair appears once with both ids populated.
    """
    eln_rows  = {r["id"]: r for r in list_eln_active(con)}
    team_rows = list_team(con)

    out: list[dict] = []
    seen_eln: set = set()

    for t in team_rows:
        e = eln_rows.get(t["eln_user_id"]) if t["eln_user_id"] else None
        if e:
            seen_eln.add(e["id"])
        out.append(_as_dict(e, t))

    for eid, e in eln_rows.items():
        if eid not in seen_eln:
            out.append(_as_dict(e, None))

    out.sort(key=lambda u: (u["display_name"] or "").lower())
    return out


# ===========================================================================
# WRITE — team_members
# ===========================================================================

def insert_team(con: sqlite3.Connection, *, team_id: str, name: str,
                role: Optional[str] = None, color: Optional[str] = None,
                eln_user_id: Optional[str] = None) -> None:
    con.execute(
        "INSERT INTO team_members (id, name, role, color, eln_user_id, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (team_id, name, role, color, eln_user_id, _now_iso()),
    )


def update_team(con: sqlite3.Connection, team_id: str, **fields) -> bool:
    allowed = {"name", "role", "color", "eln_user_id"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"update_team: unknown fields {bad}")
    if not fields:
        return False
    cols = list(fields.keys())
    vals = list(fields.values()) + [team_id]
    cur = con.execute(
        f"UPDATE team_members SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
        vals,
    )
    return cur.rowcount > 0


def delete_team(con: sqlite3.Connection, team_id: str) -> bool:
    cur = con.execute("DELETE FROM team_members WHERE id=?", (team_id,))
    return cur.rowcount > 0


def link_team_to_eln(con: sqlite3.Connection, team_id: str,
                     eln_user_id: Optional[str]) -> bool:
    """Set or clear the team_members → eln_users bridge."""
    cur = con.execute(
        "UPDATE team_members SET eln_user_id = ? WHERE id = ?",
        (eln_user_id, team_id),
    )
    return cur.rowcount > 0
