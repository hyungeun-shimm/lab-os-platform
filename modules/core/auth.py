"""Auth helpers for the ELN: password (PIN) hashing, sessions, FastAPI dependency.

Password model — for the lab context we keep it simple: 4–8 digit PIN OR a free-form
passphrase (anything 4+ chars). Stored as PBKDF2-HMAC-SHA256 with a per-user salt and
200,000 iterations — same parameters as the PMS admin-PIN code, so the security story
is consistent across the lab's local apps.

Sessions live in `eln_sessions` and are addressed by an opaque random token returned
to the browser as a cookie (`labos_session`). 7-day TTL with sliding refresh on each
request.

`require_user()` and `require_admin()` are FastAPI dependencies the routes use to
gate access. They raise HTTPException on failure.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Request, status

SESSION_COOKIE = "labos_session"
SESSION_TTL_DAYS = 7
PBKDF2_ITER = 200_000


# ---------- password hashing ----------
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Return (hash_hex, salt_hex). Generates salt if not given."""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITER)
    return dk.hex(), salt


def verify_password(password: str, expected_hash: str, salt: str) -> bool:
    """Constant-time check of password against stored hash."""
    if not password or not expected_hash or not salt:
        return False
    candidate, _ = hash_password(password, salt=salt)
    return hmac.compare_digest(candidate, expected_hash)


def validate_password_strength(password: str) -> None:
    """Raise HTTPException if password is too weak. Permissive on purpose."""
    if not password or len(password) < 4:
        raise HTTPException(status_code=400, detail="Password / PIN must be at least 4 characters.")
    if len(password) > 128:
        raise HTTPException(status_code=400, detail="Password / PIN is too long (max 128).")


# ---------- sessions ----------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session(con, user_id: str, user_agent: str | None = None) -> tuple[str, str]:
    """Insert a new session row. Returns (token, expires_at_iso)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SESSION_TTL_DAYS)
    con.execute(
        "INSERT INTO eln_sessions (token, user_id, created_at, expires_at, last_seen_at, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (token, user_id, now.isoformat(), expires.isoformat(), now.isoformat(), (user_agent or "")[:200]),
    )
    con.commit()
    return token, expires.isoformat()


def lookup_session(con, token: str) -> Optional[dict]:
    """Return user-row + session info (including mode) if token is valid and unexpired."""
    if not token:
        return None
    row = con.execute(
        """SELECT s.token, s.user_id, s.expires_at, s.mode,
                  u.id, u.username, u.display_name, u.email, u.role, u.color, u.initials, u.active,
                  COALESCE(u.can_edit_colony, 0) AS can_edit_colony
             FROM eln_sessions s
             JOIN eln_users u ON u.id = s.user_id
            WHERE s.token = ?""",
        (token,),
    ).fetchone()
    if not row:
        return None
    expires_at = row["expires_at"]
    if expires_at < _now_iso():
        con.execute("DELETE FROM eln_sessions WHERE token = ?", (token,))
        con.commit()
        return None
    if not row["active"]:
        return None
    # Sliding refresh: bump last_seen and extend expiry on each use
    new_exp = (datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)).isoformat()
    con.execute(
        "UPDATE eln_sessions SET last_seen_at = ?, expires_at = ? WHERE token = ?",
        (_now_iso(), new_exp, token),
    )
    con.commit()
    user_role = row["role"]
    sess_mode = row["mode"]  # NULL if not chosen yet
    # An admin who hasn't picked a mode yet acts as a member by default. They must
    # explicitly POST /auth/set-mode {"mode": "admin"} to gain admin powers this session.
    effective_role = "admin" if (user_role == "admin" and sess_mode == "admin") else "member"
    return {
        "token": row["token"],
        "user_id": row["user_id"],
        "expires_at": new_exp,
        "mode": sess_mode,
        "effective_role": effective_role,
        "user": {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "email": row["email"],
            "role": row["role"],
            "color": row["color"],
            "initials": row["initials"],
            "effective_role": effective_role,
            "session_mode": sess_mode,
            "can_edit_colony": bool(row["can_edit_colony"]) or (user_role == "admin"),
        },
    }


def set_session_mode(con, token: str, mode: str) -> None:
    if mode not in {"admin", "member"}:
        raise ValueError("mode must be 'admin' or 'member'")
    con.execute("UPDATE eln_sessions SET mode = ? WHERE token = ?", (mode, token))
    con.commit()


def delete_session(con, token: str) -> None:
    if not token:
        return
    con.execute("DELETE FROM eln_sessions WHERE token = ?", (token,))
    con.commit()


def purge_expired_sessions(con) -> int:
    cur = con.execute("DELETE FROM eln_sessions WHERE expires_at < ?", (_now_iso(),))
    con.commit()
    return cur.rowcount


# ---------- audit ----------
def audit(con, user_id: str | None, action: str, resource_type: str | None = None,
          resource_id: str | None = None, details: str | None = None) -> None:
    con.execute(
        "INSERT INTO eln_audit (ts, user_id, action, resource_type, resource_id, details) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_now_iso(), user_id, action, resource_type, resource_id, details),
    )
    con.commit()


# ---------- FastAPI dependencies ----------
# Each route can do:    user = Depends(require_user)
# Lazy import db_conn to avoid circular import.
def _con():
    from server import db_conn  # type: ignore
    return db_conn()


def current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with _con() as con:
        con.row_factory = __import__("sqlite3").Row
        sess = lookup_session(con, token)
        return sess["user"] if sess else None


def service_token_ok(request: Request) -> bool:
    """True if the request carries the shared service token (machine callers:
    Slack bot, rigs). Set LABOS_API_TOKEN on the server; clients send it as the
    `X-Labos-Token` header. Constant-time compare. Disabled if the env is unset."""
    expected = (os.environ.get("LABOS_API_TOKEN") or "").strip()
    if not expected:
        return False
    got = (request.headers.get("x-labos-token") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in.")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    # Effective role gates access — an admin who is in 'member' mode for this session
    # is treated as a regular user.
    if user.get("effective_role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin mode required for this action. Switch to admin mode.")
    return user


def require_colony_editor(request: Request) -> dict:
    """Permit if user is an admin (in admin mode) or has `can_edit_colony` set.
    Used to gate colony write endpoints (add/edit/delete/sync animals)."""
    user = require_user(request)
    if user.get("effective_role") == "admin":
        return user
    # Look up the persistent flag on eln_users — `user` from session may be cached.
    with _con() as con:
        con.row_factory = __import__("sqlite3").Row
        row = con.execute(
            "SELECT can_edit_colony FROM eln_users WHERE id = ?", (user["id"],)
        ).fetchone()
    if row and row["can_edit_colony"]:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You don't have permission to edit the colony. Ask an admin to grant access.",
    )
