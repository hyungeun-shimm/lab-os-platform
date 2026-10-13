"""ELN router — mounted at /api/eln/* by server.py.

Step A scope: authentication (register / login / logout / forgot ID / forgot password),
users CRUD (admin), projects CRUD + assignment, current-user info. Notes/PDF/monitoring
come in later steps.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from modules.core import auth as auth_helpers
from modules.core import animals as animals_dal
from modules.core import users as users_dal
from modules.core import mailer

router = APIRouter(prefix="/api/eln", tags=["eln"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _con() -> sqlite3.Connection:
    """Lazy import db_conn to avoid circular import with server.py."""
    from server import db_conn  # type: ignore
    return db_conn()


# ---------- helpers ----------
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
PILOT_SLUG_RE = re.compile(r"^pilot_(\d+)-[a-z0-9][a-z0-9-]{1,60}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,80}$")
NAME_KEEP_RE = re.compile(r"[^a-z]")


def _norm_username(name: str) -> str:
    return (name or "").strip().lower()


def _slugify_namepart(part: str) -> str:
    """Lowercase + drop anything but a-z. 'O'Brien' -> 'obrien'; non-ASCII letters drop to ''."""
    return NAME_KEEP_RE.sub("", (part or "").lower())


def _derive_username_root(first_name: str, last_name: str) -> str:
    """Rule: first-name initial + first 6 chars of last name. e.g. Daniel Johnston → djohnst."""
    first = _slugify_namepart(first_name)
    last = _slugify_namepart(last_name)
    if not first or not last:
        raise HTTPException(400, "First and last name must contain at least one English letter so we can build a username.")
    return (first[0] + last[:6])


def _unique_username(con: sqlite3.Connection, root: str) -> str:
    """Append a numeric suffix if the root is taken (djohnst → djohnst2 → djohnst3 …)."""
    candidate = root
    n = 1
    while con.execute("SELECT 1 FROM eln_users WHERE LOWER(username) = ?", (candidate,)).fetchone():
        n += 1
        candidate = f"{root}{n}"
        if n > 999:
            raise HTTPException(500, "Could not allocate a unique username; please contact admin.")
    return candidate


def _next_pilot_slug(con: sqlite3.Connection, user_id: str, title_slug: str) -> str:
    """Compute pilot_{N}-{title_slug} where N is one more than the user's current max."""
    row = con.execute(
        """SELECT slug FROM eln_projects
            WHERE kind = 'pilot' AND owner_id = ?
            ORDER BY created_at DESC""",
        (user_id,),
    ).fetchall()
    used_n: list[int] = []
    for r in row:
        m = PILOT_SLUG_RE.match(r["slug"])
        if m:
            used_n.append(int(m.group(1)))
    n = (max(used_n) + 1) if used_n else 1
    return f"pilot_{n}-{title_slug}"


def _titleslug(title: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", title.strip().lower()).strip("-")
    return s[:60] or "untitled"


def _user_count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM eln_users").fetchone()[0]


def _ensure_first_user_is_admin(con: sqlite3.Connection, role: str) -> str:
    """The very first registered user is forced to admin role (regardless of input)."""
    if _user_count(con) == 0:
        return "admin"
    return role or "member"


def _set_session_cookie(response: Response, token: str) -> None:
    # Secure flag is on when served over HTTPS (set LABOS_SECURE_COOKIES=1 on the
    # central server). Off by default so plain-HTTP localhost dev still works.
    secure = os.environ.get("LABOS_SECURE_COOKIES") == "1"
    response.set_cookie(
        key=auth_helpers.SESSION_COOKIE,
        value=token,
        max_age=auth_helpers.SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(auth_helpers.SESSION_COOKIE)


def _row_to_user(row) -> dict:
    keys = row.keys() if hasattr(row, "keys") else []
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "email": row["email"],
        "role": row["role"],
        "color": row["color"],
        "initials": row["initials"],
        "active": bool(row["active"]),
        "approved": bool(row["approved"]) if "approved" in keys else True,
        "email_digest": bool(row["email_digest"]) if "email_digest" in keys else True,
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
        "can_edit_colony": bool(row["can_edit_colony"]) if "can_edit_colony" in keys else False,
        "organization":   (row["organization"] if "organization" in keys else None) or "",
        "position":       (row["position"]     if "position"     in keys else None) or "",
        "status":         (row["status"]       if "status"       in keys else None) or "current",
    }


def _row_to_project(row) -> dict:
    keys = row.keys() if hasattr(row, "keys") else []
    return {
        "slug": row["slug"],
        "title": row["title"],
        "description": row["description"] or "",
        "kind": row["kind"],
        "owner_id": row["owner_id"],
        "created_at": row["created_at"],
        "archived": bool(row["archived"]),
        "short_name": (row["short_name"] if "short_name" in keys else "") or "",
        "pm_slug":    (row["pm_slug"]    if "pm_slug"    in keys else "") or "",
    }


# ====================================================================
# ──── AUTH ───────────────────────────────────────────────────────────
# ====================================================================
class RegisterIn(BaseModel):
    first_name: str
    last_name: str
    email: str
    password: str
    color: Optional[str] = None


class PreviewUsernameIn(BaseModel):
    first_name: str
    last_name: str


@router.post("/auth/preview-username")
def preview_username(payload: PreviewUsernameIn):
    """Return the username that would be assigned for these names, given current DB state."""
    try:
        root = _derive_username_root(payload.first_name, payload.last_name)
    except HTTPException as e:
        return {"ok": False, "detail": e.detail}
    with _con() as con:
        con.row_factory = sqlite3.Row
        candidate = _unique_username(con, root)
    return {"ok": True, "username": candidate, "root": root, "would_suffix": candidate != root}


@router.post("/auth/register")
def register(payload: RegisterIn, request: Request, response: Response):
    auth_helpers.validate_password_strength(payload.password)
    first = (payload.first_name or "").strip()
    last = (payload.last_name or "").strip()
    if not first or not last:
        raise HTTPException(400, "First name and last name are both required.")
    email = (payload.email or "").strip()
    if not email or "@" not in email or "." not in email:
        raise HTTPException(400, "Valid email is required.")
    color = (payload.color or "").strip() or None

    display = f"{first} {last}".strip()
    initials = (first[0] + last[0]).upper() if first and last else None
    root = _derive_username_root(first, last)

    with _con() as con:
        con.row_factory = sqlite3.Row
        if con.execute("SELECT 1 FROM eln_users WHERE LOWER(email) = ?", (email.lower(),)).fetchone():
            raise HTTPException(409, "An account with that email already exists.")
        username = _unique_username(con, root)
        is_first = _user_count(con) == 0
        role = "admin" if is_first else "member"
        # The first user bootstraps the lab and is auto-approved + signed in.
        # Everyone after that is a pending join request an admin must approve.
        approved = 1 if is_first else 0
        user_id = secrets.token_hex(5)
        pwd_hash, pwd_salt = auth_helpers.hash_password(payload.password)
        con.execute(
            """INSERT INTO eln_users (id, username, display_name, email, role, pin_hash, pin_salt, color, initials, active, approved, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (user_id, username, display, email, role, pwd_hash, pwd_salt, color, initials, approved, _now_iso()),
        )
        if not is_first:
            auth_helpers.audit(con, user_id, "register_request", "user", user_id,
                               json.dumps({"email": email}))
            con.commit()
            return {"ok": True, "pending": True, "username": username,
                    "message": "Request submitted. An admin will review your account; "
                               "you can sign in once it's approved."}
        token, expires = auth_helpers.create_session(con, user_id, request.headers.get("user-agent"))
        auth_helpers.audit(con, user_id, "register", "user", user_id, json.dumps({"role": role}))
        con.execute("UPDATE eln_users SET last_login_at = ? WHERE id = ?", (_now_iso(), user_id))
        con.commit()
        sess = auth_helpers.lookup_session(con, token)
    _set_session_cookie(response, token)
    return {"ok": True, "pending": False, "user": sess["user"], "expires_at": expires, "first_user": True}


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(payload: LoginIn, request: Request, response: Response):
    username = _norm_username(payload.username)
    pwd = payload.password or ""
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM eln_users WHERE LOWER(username) = ?", (username,)).fetchone()
        if not row or not auth_helpers.verify_password(pwd, row["pin_hash"], row["pin_salt"]):
            # Don't reveal which side was wrong.
            auth_helpers.audit(con, row["id"] if row else None, "login_fail", "user", username)
            raise HTTPException(401, "Wrong username or password.")
        # Password is correct from here, so it's safe to give a specific reason.
        if "approved" in row.keys() and not row["approved"]:
            auth_helpers.audit(con, row["id"], "login_pending", "user", row["id"])
            raise HTTPException(403, "Your account is awaiting admin approval.")
        if not row["active"]:
            auth_helpers.audit(con, row["id"], "login_disabled", "user", row["id"])
            raise HTTPException(403, "This account has been deactivated. Contact an admin.")
        token, expires = auth_helpers.create_session(con, row["id"], request.headers.get("user-agent"))
        con.execute("UPDATE eln_users SET last_login_at = ? WHERE id = ?", (_now_iso(), row["id"]))
        auth_helpers.audit(con, row["id"], "login", "user", row["id"])
        con.commit()
        sess = auth_helpers.lookup_session(con, token)
    _set_session_cookie(response, token)
    return {"ok": True, "user": sess["user"], "expires_at": expires}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth_helpers.SESSION_COOKIE)
    if token:
        with _con() as con:
            con.row_factory = sqlite3.Row
            sess = auth_helpers.lookup_session(con, token)
            if sess:
                auth_helpers.audit(con, sess["user"]["id"], "logout", "user", sess["user"]["id"])
            auth_helpers.delete_session(con, token)
    _clear_session_cookie(response)
    return {"ok": True}


@router.get("/auth/me")
def me(user: dict = Depends(auth_helpers.require_user)):
    return {"ok": True, "user": user}


class SetModeIn(BaseModel):
    mode: str  # 'admin' | 'member'


@router.post("/auth/set-mode")
def set_mode(payload: SetModeIn, request: Request):
    """Switch this session into admin or member mode.

    Only users with role='admin' can switch into admin mode. Anyone can switch
    *out* (drop privileges by going to member mode). The choice is per-session,
    so an admin can open a second browser to operate as a member without losing
    admin in the first window.
    """
    mode = (payload.mode or "").strip().lower()
    if mode not in {"admin", "member"}:
        raise HTTPException(400, "mode must be 'admin' or 'member'.")
    token = request.cookies.get(auth_helpers.SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Not signed in.")
    with _con() as con:
        con.row_factory = sqlite3.Row
        sess = auth_helpers.lookup_session(con, token)
        if not sess:
            raise HTTPException(401, "Session expired.")
        if mode == "admin" and sess["user"]["role"] != "admin":
            raise HTTPException(403, "You do not have admin privileges on this account.")
        auth_helpers.set_session_mode(con, token, mode)
        auth_helpers.audit(con, sess["user_id"], "set_session_mode", "session", token[:8] + "…", json.dumps({"mode": mode}))
        # Re-lookup so the response carries the updated effective_role.
        sess = auth_helpers.lookup_session(con, token)
    return {"ok": True, "user": sess["user"], "mode": sess["mode"], "effective_role": sess["effective_role"]}


@router.get("/auth/status")
def status(request: Request):
    """Public endpoint — tells the login page whether *any* user exists."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        n = _user_count(con)
        # Are we currently signed in?
        token = request.cookies.get(auth_helpers.SESSION_COOKIE)
        signed = None
        if token:
            sess = auth_helpers.lookup_session(con, token)
            signed = sess["user"] if sess else None
    return {"user_count": n, "first_run": n == 0, "signed_in": signed}


# -- Forgot ID --------------------------------------------------------
class ForgotIdIn(BaseModel):
    email: str


@router.post("/auth/forgot-username")
def forgot_username(payload: ForgotIdIn):
    """Look up usernames associated with this email. For a tiny lab we just return them
    directly — no email infrastructure needed yet. (Easy to swap for SMTP later.)"""
    email = (payload.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Enter a valid email address.")
    with _con() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT username, display_name FROM eln_users WHERE LOWER(email) = ? AND active = 1",
            (email,),
        ).fetchall()
    return {
        "ok": True,
        "found": [{"username": r["username"], "display_name": r["display_name"]} for r in rows],
    }


# -- Forgot password --------------------------------------------------
class ForgotPwIn(BaseModel):
    username: str
    email: str


@router.post("/auth/forgot-password")
def forgot_password(payload: ForgotPwIn):
    """Issue a one-time reset code. For now we return the code in the response (lab uses
    only — replace with SMTP send when remote users are added)."""
    username = _norm_username(payload.username)
    email = (payload.email or "").strip().lower()
    if not username or not email:
        raise HTTPException(400, "Username and email are both required.")
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT id, email FROM eln_users WHERE LOWER(username) = ? AND active = 1",
            (username,),
        ).fetchone()
        # Don't tell the requester whether either field matched.
        ok_match = bool(row and (row["email"] or "").lower() == email)
        if not ok_match:
            auth_helpers.audit(con, row["id"] if row else None, "forgot_password_fail", "user", username)
            # Return success anyway to avoid enumeration; just don't include a code.
            return {"ok": True, "code_sent": False}
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_hash, code_salt = auth_helpers.hash_password(code)
        reset_token = secrets.token_urlsafe(24)
        from datetime import timedelta
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        con.execute(
            "INSERT INTO eln_password_resets (token, user_id, code_hash, code_salt, created_at, expires_at, used) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (reset_token, row["id"], code_hash, code_salt, _now_iso(), expires),
        )
        auth_helpers.audit(con, row["id"], "forgot_password_issued", "user", row["id"])
        con.commit()
        recipient = (row["email"] or "").strip()

    emailed = False
    if mailer.smtp_configured() and recipient:
        try:
            mailer.send_email(
                recipient,
                "Lab OS — password reset code",
                f"Your Lab OS password reset code is: {code}\n\n"
                "Enter it on the password-reset page within 30 minutes to choose a new "
                "password. If you didn't request this, you can ignore this email.",
            )
            emailed = True
        except Exception as exc:  # an SMTP hiccup must not lock the user out
            print(f"[eln] password-reset email send failed: {exc}", flush=True)

    resp = {"ok": True, "code_sent": True, "reset_token": reset_token}
    if emailed:
        resp["emailed"] = True
        resp["note"] = "Reset code sent to your email."
    else:
        # SMTP off (or send failed): fall back to showing the code so lab-local
        # recovery still works without email.
        resp["code"] = code
        resp["note"] = "Lab-local mode: code shown directly (SMTP off or send failed)."
    return resp


class ResetPwIn(BaseModel):
    reset_token: str
    code: str
    new_password: str


@router.post("/auth/reset-password")
def reset_password(payload: ResetPwIn):
    auth_helpers.validate_password_strength(payload.new_password)
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM eln_password_resets WHERE token = ?", (payload.reset_token,)).fetchone()
        if not row or row["used"] or row["expires_at"] < _now_iso():
            raise HTTPException(400, "Reset code is invalid or expired.")
        if not auth_helpers.verify_password(payload.code, row["code_hash"], row["code_salt"]):
            raise HTTPException(400, "Reset code is invalid or expired.")
        new_hash, new_salt = auth_helpers.hash_password(payload.new_password)
        con.execute("UPDATE eln_users SET pin_hash = ?, pin_salt = ? WHERE id = ?", (new_hash, new_salt, row["user_id"]))
        con.execute("UPDATE eln_password_resets SET used = 1 WHERE token = ?", (payload.reset_token,))
        # Invalidate any existing sessions
        con.execute("DELETE FROM eln_sessions WHERE user_id = ?", (row["user_id"],))
        auth_helpers.audit(con, row["user_id"], "reset_password_done", "user", row["user_id"])
        con.commit()
    return {"ok": True}


class ChangePwIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/auth/change-password")
def change_password(payload: ChangePwIn, user: dict = Depends(auth_helpers.require_user)):
    """Logged-in user changes their own password (knows the current one)."""
    auth_helpers.validate_password_strength(payload.new_password)
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT pin_hash, pin_salt FROM eln_users WHERE id = ?", (user["id"],)).fetchone()
        if not row or not auth_helpers.verify_password(payload.current_password, row["pin_hash"], row["pin_salt"]):
            raise HTTPException(400, "Current password is incorrect.")
        new_hash, new_salt = auth_helpers.hash_password(payload.new_password)
        con.execute("UPDATE eln_users SET pin_hash = ?, pin_salt = ? WHERE id = ?", (new_hash, new_salt, user["id"]))
        auth_helpers.audit(con, user["id"], "change_password", "user", user["id"])
        con.commit()
    return {"ok": True}


# ====================================================================
# ──── USERS (admin) ──────────────────────────────────────────────────
# ====================================================================
@router.get("/users")
def list_users(user: dict = Depends(auth_helpers.require_admin)):
    with _con() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM eln_users ORDER BY created_at").fetchall()
    return {"users": [_row_to_user(r) for r in rows]}


@router.get("/members")
def list_members(user: dict = Depends(auth_helpers.require_admin)):
    """Admin member roster — users + per-project assignments rolled up.

    Each user gets a `projects` array: [{slug, title, short_name, project_role, authority}].
    """
    with _con() as con:
        con.row_factory = sqlite3.Row
        urows = con.execute("SELECT * FROM eln_users ORDER BY status, display_name").fetchall()
        prows = con.execute("""
            SELECT pm.user_id, pm.project_slug, pm.project_role, pm.authority,
                   p.title, p.short_name
              FROM eln_project_members pm
              LEFT JOIN eln_projects p ON p.slug = pm.project_slug
        """).fetchall()
        # All projects (for the per-row picker on the FE)
        proj_rows = con.execute(
            "SELECT slug, title, short_name FROM eln_projects ORDER BY title"
        ).fetchall()

    by_user: dict = {}
    for r in prows:
        by_user.setdefault(r["user_id"], []).append({
            "slug":          r["project_slug"],
            "title":         r["title"] or r["project_slug"],
            "short_name":    r["short_name"] or "",
            "project_role":  r["project_role"] or "",
            "authority":     r["authority"]    or "edit",
        })

    members = []
    for u in urows:
        d = _row_to_user(u)
        d["projects"] = by_user.get(u["id"], [])
        members.append(d)
    return {
        "members": members,
        "projects": [{"slug": p["slug"], "title": p["title"], "short_name": p["short_name"] or ""}
                     for p in proj_rows],
    }


class UpdateMemberProjectIn(BaseModel):
    project_role: Optional[str] = None  # 'first_author' | 'collaborator' | 'ra' | ''
    authority:    Optional[str] = None  # 'edit' | 'read' | 'restricted'


@router.put("/projects/{slug}/members/{user_id}")
def set_member_project(slug: str, user_id: str, payload: UpdateMemberProjectIn,
                       _admin: dict = Depends(auth_helpers.require_admin)):
    """Upsert a user's per-project intellectual role + permission tier."""
    role_v = (payload.project_role or "").strip().lower()
    auth_v = (payload.authority    or "edit").strip().lower()
    if role_v not in {"", "first_author", "collaborator", "ra"}:
        raise HTTPException(400, "project_role must be first_author|collaborator|ra|(empty)")
    if auth_v not in {"edit", "read", "restricted"}:
        raise HTTPException(400, "authority must be edit|read|restricted")
    with _con() as con:
        con.row_factory = sqlite3.Row
        proj = con.execute("SELECT slug FROM eln_projects WHERE slug = ?", (slug,)).fetchone()
        if not proj:
            raise HTTPException(404, "Project not found.")
        usr  = con.execute("SELECT id FROM eln_users WHERE id = ?", (user_id,)).fetchone()
        if not usr:
            raise HTTPException(404, "User not found.")
        exists = con.execute(
            "SELECT 1 FROM eln_project_members WHERE project_slug = ? AND user_id = ?",
            (slug, user_id),
        ).fetchone()
        if exists:
            con.execute(
                "UPDATE eln_project_members SET project_role = ?, authority = ?"
                " WHERE project_slug = ? AND user_id = ?",
                (role_v or None, auth_v, slug, user_id),
            )
        else:
            con.execute(
                "INSERT INTO eln_project_members (project_slug, user_id, role, project_role, authority, assigned_at)"
                " VALUES (?, ?, 'write', ?, ?, ?)",
                (slug, user_id, role_v or None, auth_v, _now_iso()),
            )
        auth_helpers.audit(con, _admin["id"], "set_member_project", "project_member",
                           f"{slug}:{user_id}", json.dumps({"project_role": role_v, "authority": auth_v}))
        con.commit()
    return {"ok": True, "project_slug": slug, "user_id": user_id,
            "project_role": role_v, "authority": auth_v}


class UpdateUserIn(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    color: Optional[str] = None
    initials: Optional[str] = None
    active: Optional[bool] = None
    can_edit_colony: Optional[bool] = None
    email_digest: Optional[bool] = None
    organization: Optional[str] = None
    position: Optional[str] = None
    status: Optional[str] = None    # 'current' | 'alumni'


@router.patch("/users/{user_id}")
def update_user(user_id: str, payload: UpdateUserIn, _admin: dict = Depends(auth_helpers.require_admin)):
    fields: list[tuple[str, object]] = []
    if payload.display_name is not None: fields.append(("display_name", payload.display_name.strip()))
    if payload.email is not None: fields.append(("email", (payload.email or "").strip() or None))
    if payload.role in {"admin", "member"}: fields.append(("role", payload.role))
    if payload.color is not None: fields.append(("color", payload.color.strip() or None))
    if payload.initials is not None: fields.append(("initials", (payload.initials or "").strip().upper() or None))
    if payload.active is not None: fields.append(("active", 1 if payload.active else 0))
    if payload.can_edit_colony is not None: fields.append(("can_edit_colony", 1 if payload.can_edit_colony else 0))
    if payload.email_digest is not None: fields.append(("email_digest", 1 if payload.email_digest else 0))
    if payload.organization is not None: fields.append(("organization", (payload.organization or "").strip() or None))
    if payload.position    is not None: fields.append(("position",     (payload.position    or "").strip() or None))
    if payload.status      is not None:
        s = (payload.status or "current").strip().lower()
        if s not in {"current", "alumni"}:
            raise HTTPException(400, "status must be 'current' or 'alumni'.")
        fields.append(("status", s))
    if not fields:
        raise HTTPException(400, "No fields to update.")
    set_clause = ", ".join(f"{k} = ?" for k, _ in fields)
    values = [v for _, v in fields] + [user_id]
    with _con() as con:
        con.row_factory = sqlite3.Row
        cur = con.execute(f"UPDATE eln_users SET {set_clause} WHERE id = ?", values)
        if cur.rowcount == 0:
            raise HTTPException(404, "User not found.")
        auth_helpers.audit(con, _admin["id"], "update_user", "user", user_id, json.dumps({k: v for k, v in fields}))
        con.commit()
        row = con.execute("SELECT * FROM eln_users WHERE id = ?", (user_id,)).fetchone()
    return {"ok": True, "user": _row_to_user(row)}


@router.post("/users/{user_id}/approve")
def approve_user(user_id: str, _admin: dict = Depends(auth_helpers.require_admin)):
    """Approve a pending join request so the account can sign in."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM eln_users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found.")
        con.execute("UPDATE eln_users SET approved = 1 WHERE id = ?", (user_id,))
        auth_helpers.audit(con, _admin["id"], "approve_user", "user", user_id)
        con.commit()
        row = con.execute("SELECT * FROM eln_users WHERE id = ?", (user_id,)).fetchone()
    return {"ok": True, "user": _row_to_user(row)}


@router.post("/users/{user_id}/reject")
def reject_user(user_id: str, _admin: dict = Depends(auth_helpers.require_admin)):
    """Reject (delete) a still-pending join request. Refuses approved accounts so
    this can never be used to wipe a real, in-use user."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT id, approved FROM eln_users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found.")
        if row["approved"]:
            raise HTTPException(409, "This account is already approved. Deactivate it instead.")
        con.execute("DELETE FROM eln_sessions WHERE user_id = ?", (user_id,))
        con.execute("DELETE FROM eln_users WHERE id = ?", (user_id,))
        auth_helpers.audit(con, _admin["id"], "reject_user", "user", user_id)
        con.commit()
    return {"ok": True}


# ====================================================================
# ──── PROJECTS ───────────────────────────────────────────────────────
# ====================================================================
class CreateProjectIn(BaseModel):
    title: str
    description: Optional[str] = None
    kind: str = "assigned"     # 'assigned' | 'pilot'
    slug: Optional[str] = None # only used for assigned (admin-set); ignored for pilot
    short_name: Optional[str] = None
    pm_slug: Optional[str] = None   # link to custom_projects.slug if picked from PM
    assign_user_ids: list[str] = Field(default_factory=list)


def _enrich_project(con: sqlite3.Connection, row, include_members: bool, viewer_id: str) -> dict:
    """Add member_count, note_count, last_note_at + (optionally) full member list."""
    p = _row_to_project(row)
    slug = row["slug"]
    mc = con.execute(
        "SELECT COUNT(*) AS n FROM eln_project_members WHERE project_slug = ?", (slug,),
    ).fetchone()
    p["member_count"] = mc["n"] if mc else 0
    nc = con.execute(
        "SELECT COUNT(*) AS n, MAX(updated_at) AS last FROM eln_notes WHERE project_slug = ?",
        (slug,),
    ).fetchone()
    p["note_count"] = nc["n"] if nc else 0
    p["last_note_at"] = nc["last"] if nc else None
    # Viewer's own role on the project
    vr = con.execute(
        "SELECT role FROM eln_project_members WHERE project_slug = ? AND user_id = ?",
        (slug, viewer_id),
    ).fetchone()
    p["viewer_role"] = vr["role"] if vr else None
    if include_members:
        members = con.execute(
            """SELECT u.id, u.username, u.display_name, u.color, u.initials, m.role
                 FROM eln_project_members m
                 JOIN eln_users u ON u.id = m.user_id
                WHERE m.project_slug = ?
                ORDER BY m.assigned_at""",
            (slug,),
        ).fetchall()
        p["members"] = [
            {"id": m["id"], "username": m["username"], "display_name": m["display_name"],
             "color": m["color"], "initials": m["initials"], "role": m["role"]} for m in members
        ]
    return p


@router.get("/projects")
def list_my_projects(user: dict = Depends(auth_helpers.require_user)):
    """Project list filtered to what the current user can access. Admin mode sees all.

    Returns enriched cards: member_count, note_count, last_note_at, viewer_role.
    """
    with _con() as con:
        con.row_factory = sqlite3.Row
        if user.get("effective_role") == "admin":
            rows = con.execute(
                "SELECT * FROM eln_projects WHERE archived = 0 ORDER BY kind, created_at DESC"
            ).fetchall()
            projects = [_enrich_project(con, r, include_members=True, viewer_id=user["id"]) for r in rows]
        else:
            rows = con.execute(
                """SELECT p.* FROM eln_projects p
                     JOIN eln_project_members m ON m.project_slug = p.slug
                    WHERE m.user_id = ? AND p.archived = 0
                    ORDER BY p.kind, p.created_at DESC""",
                (user["id"],),
            ).fetchall()
            projects = [_enrich_project(con, r, include_members=False, viewer_id=user["id"]) for r in rows]
    return {"projects": projects}


@router.get("/projects/{slug}")
def get_project(slug: str, user: dict = Depends(auth_helpers.require_user)):
    """Single-project read — checks viewer access."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM eln_projects WHERE slug = ? AND archived = 0", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Project not found.")
        if user.get("effective_role") != "admin":
            membership = con.execute(
                "SELECT role FROM eln_project_members WHERE project_slug = ? AND user_id = ?",
                (slug, user["id"]),
            ).fetchone()
            if not membership:
                raise HTTPException(403, "You don't have access to this project.")
        include = user.get("effective_role") == "admin"
        p = _enrich_project(con, row, include_members=include, viewer_id=user["id"])
    return {"project": p}


@router.post("/projects")
def create_project(payload: CreateProjectIn, user: dict = Depends(auth_helpers.require_user)):
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(400, "Title is required.")
    kind = (payload.kind or "assigned").strip().lower()
    if kind not in {"assigned", "pilot"}:
        raise HTTPException(400, "kind must be 'assigned' or 'pilot'.")
    is_admin = user.get("effective_role") == "admin"
    if kind == "assigned" and not is_admin:
        raise HTTPException(403, "Only the lab head (admin mode) can create assigned projects. Use 'pilot' instead, or switch to admin mode.")

    with _con() as con:
        con.row_factory = sqlite3.Row
        title_slug = _titleslug(title)
        if kind == "pilot":
            slug = _next_pilot_slug(con, user["id"], title_slug)
        else:
            slug = (payload.slug or title_slug).strip().lower()
            if not SLUG_RE.match(slug):
                raise HTTPException(400, "Invalid slug — letters/digits/-/_ only.")
            if con.execute("SELECT 1 FROM eln_projects WHERE slug = ?", (slug,)).fetchone():
                raise HTTPException(409, "A project with that slug already exists.")
        con.execute(
            """INSERT INTO eln_projects
                 (slug, title, description, kind, owner_id, created_at, short_name, pm_slug)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (slug, title, (payload.description or "").strip() or None, kind, user["id"], _now_iso(),
             (payload.short_name or "").strip() or None,
             (payload.pm_slug or "").strip() or None),
        )
        # Always grant creator write access.
        con.execute(
            "INSERT INTO eln_project_members (project_slug, user_id, role, assigned_at) VALUES (?, ?, ?, ?)",
            (slug, user["id"], "admin" if is_admin else "write", _now_iso()),
        )
        # Admin can pre-assign other users on create.
        if is_admin and payload.assign_user_ids:
            for uid in payload.assign_user_ids:
                if uid == user["id"]:
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO eln_project_members (project_slug, user_id, role, assigned_at) VALUES (?, ?, 'write', ?)",
                    (slug, uid, _now_iso()),
                )
        auth_helpers.audit(con, user["id"], "create_project", "project", slug,
                           json.dumps({"kind": kind, "title": title}))
        con.commit()
        row = con.execute("SELECT * FROM eln_projects WHERE slug = ?", (slug,)).fetchone()
    return {"ok": True, "project": _row_to_project(row)}


class AssignIn(BaseModel):
    user_ids: list[str]
    role: str = "write"


@router.post("/projects/{slug}/members")
def assign_members(slug: str, payload: AssignIn, _admin: dict = Depends(auth_helpers.require_admin)):
    if payload.role not in {"read", "write", "admin"}:
        raise HTTPException(400, "role must be read | write | admin.")
    with _con() as con:
        con.row_factory = sqlite3.Row
        if not con.execute("SELECT 1 FROM eln_projects WHERE slug = ?", (slug,)).fetchone():
            raise HTTPException(404, "Project not found.")
        for uid in payload.user_ids:
            con.execute(
                "INSERT OR REPLACE INTO eln_project_members (project_slug, user_id, role, assigned_at) VALUES (?, ?, ?, ?)",
                (slug, uid, payload.role, _now_iso()),
            )
        auth_helpers.audit(con, _admin["id"], "assign_members", "project", slug,
                           json.dumps({"user_ids": payload.user_ids, "role": payload.role}))
        con.commit()
    return {"ok": True}


@router.delete("/projects/{slug}/members/{user_id}")
def remove_member(slug: str, user_id: str, _admin: dict = Depends(auth_helpers.require_admin)):
    with _con() as con:
        con.execute("DELETE FROM eln_project_members WHERE project_slug = ? AND user_id = ?", (slug, user_id))
        auth_helpers.audit(con, _admin["id"], "remove_member", "project", slug, json.dumps({"user_id": user_id}))
        con.commit()
    return {"ok": True}


class UpdateProjectIn(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    new_slug: Optional[str] = None       # admin only — rename DB key (and FK fixups)
    short_name: Optional[str] = None
    pm_slug: Optional[str] = None        # admin only — relink to a PM project


def _project_mutator_for(con: sqlite3.Connection, slug: str, user: dict) -> dict:
    """Return the project row + permission flags for the current user, raising 403 if no access."""
    row = con.execute("SELECT * FROM eln_projects WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise HTTPException(404, "Project not found.")
    is_admin = user.get("effective_role") == "admin"
    is_owner = (row["owner_id"] == user["id"])
    # Owners of pilot projects can edit/delete their own. Assigned projects: admin only.
    can_mutate = is_admin or (is_owner and row["kind"] == "pilot")
    return {"row": row, "is_admin": is_admin, "is_owner": is_owner, "can_mutate": can_mutate}


@router.patch("/projects/{slug}")
def update_project(slug: str, payload: UpdateProjectIn, user: dict = Depends(auth_helpers.require_user)):
    """Rename / re-describe a project.

    - Title / description: owner of a pilot project, or any admin-mode user.
    - new_slug (rename): admin mode only. Cascades to all FK tables.
    """
    with _con() as con:
        con.row_factory = sqlite3.Row
        ctx = _project_mutator_for(con, slug, user)
        if not ctx["can_mutate"]:
            raise HTTPException(403, "Only the project owner (pilot) or an admin can edit this project.")
        row = ctx["row"]

        sets: list = []
        vals: list = []
        if payload.title is not None:
            t = payload.title.strip()
            if not t:
                raise HTTPException(400, "Title cannot be blank.")
            sets.append("title = ?"); vals.append(t)
        if payload.description is not None:
            sets.append("description = ?"); vals.append(payload.description.strip() or None)
        if payload.short_name is not None:
            sets.append("short_name = ?"); vals.append((payload.short_name or "").strip() or None)
        if payload.pm_slug is not None and ctx["is_admin"]:
            sets.append("pm_slug = ?"); vals.append((payload.pm_slug or "").strip() or None)

        # Slug rename — admin mode only, and skip if it equals the current slug.
        new_slug = (payload.new_slug or "").strip().lower()
        renaming = bool(new_slug) and new_slug != slug
        if renaming:
            if not ctx["is_admin"]:
                raise HTTPException(403, "Renaming the slug requires admin mode.")
            if not SLUG_RE.match(new_slug):
                raise HTTPException(400, "Invalid slug — letters/digits/-/_ only.")
            if con.execute("SELECT 1 FROM eln_projects WHERE slug = ?", (new_slug,)).fetchone():
                raise HTTPException(409, "A project with that slug already exists.")
            # Cascade — manually since SQLite FKs don't auto-update on UPDATE
            con.execute("UPDATE eln_projects SET slug = ? WHERE slug = ?", (new_slug, slug))
            con.execute("UPDATE eln_project_members SET project_slug = ? WHERE project_slug = ?", (new_slug, slug))
            con.execute("UPDATE eln_notes SET project_slug = ? WHERE project_slug = ?", (new_slug, slug))
            target_slug = new_slug
        else:
            target_slug = slug

        if sets:
            con.execute(f"UPDATE eln_projects SET {', '.join(sets)} WHERE slug = ?", vals + [target_slug])

        if not sets and not renaming:
            raise HTTPException(400, "Nothing to update.")

        auth_helpers.audit(
            con, user["id"], "update_project", "project", target_slug,
            json.dumps({"old_slug": slug if renaming else None,
                        "title": payload.title, "renamed": renaming}),
        )
        con.commit()
        fresh = con.execute("SELECT * FROM eln_projects WHERE slug = ?", (target_slug,)).fetchone()
        include = user.get("effective_role") == "admin"
        out = _enrich_project(con, fresh, include_members=include, viewer_id=user["id"])
    return {"ok": True, "project": out}


@router.delete("/projects/{slug}")
def delete_project(slug: str, hard: bool = False, user: dict = Depends(auth_helpers.require_user)):
    """Archive (soft delete) by default. Pass ?hard=true (admin mode only) to permanently
    remove the project + all its notes/members via CASCADE.
    """
    with _con() as con:
        con.row_factory = sqlite3.Row
        ctx = _project_mutator_for(con, slug, user)
        if not ctx["can_mutate"]:
            raise HTTPException(403, "Only the project owner (pilot) or an admin can delete this project.")
        if hard:
            if not ctx["is_admin"]:
                raise HTTPException(403, "Hard delete requires admin mode.")
            con.execute("DELETE FROM eln_notes WHERE project_slug = ?", (slug,))
            con.execute("DELETE FROM eln_project_members WHERE project_slug = ?", (slug,))
            con.execute("DELETE FROM eln_projects WHERE slug = ?", (slug,))
            auth_helpers.audit(con, user["id"], "hard_delete_project", "project", slug)
        else:
            con.execute("UPDATE eln_projects SET archived = 1 WHERE slug = ?", (slug,))
            auth_helpers.audit(con, user["id"], "archive_project", "project", slug)
        con.commit()
    return {"ok": True, "mode": "hard_delete" if hard else "archive"}


# ====================================================================
# ──── MICE — animal lookup from scheduler.db ────────────────────────
# ====================================================================
def _compute_age_weeks(dob):
    if not dob:
        return None
    try:
        from datetime import date
        d = date.fromisoformat(dob)
        today = date.today()
        return (today - d).days // 7
    except Exception:
        return None


def _surgery_done(dos):
    """True when a surgery date exists and is on/before today. Permissive on parse
    failure → False (treated as not-yet-operated)."""
    if not dos:
        return False
    try:
        from datetime import date
        return date.fromisoformat(str(dos).strip()) <= date.today()
    except Exception:
        return False


def _cohort_base(v):
    """Strip a control-group suffix (-con / -control, any spacing/dash) so an
    experimental cohort and its control collapse to one base, case-insensitively.
    e.g. 'dC', 'dC-con', 'dC - control' → 'dc'."""
    import re
    s = re.sub(r"\s*[-–]\s*(con|control)\s*$", "", (v or "").strip(), flags=re.I)
    return s.strip().lower()


def _cohort_matches(filt, val):
    """True if cohort `val` is in the same group as filter `filt` — i.e. the
    cohort itself OR its control (filtering 'dC' also shows 'dC-con')."""
    if not (filt or "").strip():
        return True
    return _cohort_base(filt) == _cohort_base(val)


def _compute_days_in_condition(animal_row):
    """Return (days_in_current_condition, marker_date_iso). Walks dates from cond3 back to dos."""
    from datetime import date
    today = date.today()
    for key in ("date_cond3", "date_cond2", "date_cond1", "dos"):
        v = animal_row[key] if key in animal_row.keys() else None
        if not v:
            continue
        try:
            d = date.fromisoformat(v)
        except Exception:
            continue
        if d <= today:
            return (today - d).days, v
    return None, None


class MouseTransferIn(BaseModel):
    to_user_id: str
    note: Optional[str] = None


@router.post("/mice/{mouse_id}/transfer")
def transfer_mouse(mouse_id: str, payload: MouseTransferIn,
                   user: dict = Depends(auth_helpers.require_user)):
    """Reassign a mouse to another user. Only the current owner or an admin may transfer."""
    mid = (mouse_id or "").strip()
    if not mid:
        raise HTTPException(400, "Mouse ID is required.")
    to_uid = (payload.to_user_id or "").strip()
    if not to_uid:
        raise HTTPException(400, "to_user_id is required.")

    with _con() as con:
        con.row_factory = sqlite3.Row

        m_row = animals_dal.get(con, mid)
        if not m_row:
            raise HTTPException(404, "Mouse not in animal DB.")
        from_uid = m_row["assigned_to"]

        # Permission: owner or admin
        if user.get("effective_role") != "admin" and from_uid != user["id"]:
            raise HTTPException(403, "You can only transfer mice assigned to you.")

        # Recipient must exist
        to_row = con.execute("SELECT id FROM eln_users WHERE id = ? AND active = 1", (to_uid,)).fetchone()
        if not to_row:
            raise HTTPException(404, "Recipient user not found.")

        if from_uid == to_uid:
            raise HTTPException(400, "Already assigned to that user.")

        animals_dal.reassign(con, m_row["mouse_id"], to_uid)
        now = _now_iso()
        con.execute(
            """INSERT INTO eln_mouse_transfers
                 (id, mouse_id, from_user_id, to_user_id, transferred_by, note, transferred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (uuid.uuid4().hex, m_row["mouse_id"], from_uid, to_uid, user["id"], payload.note, now),
        )
        auth_helpers.audit(con, user["id"], "transfer_mouse", "mouse", m_row["mouse_id"],
                           json.dumps({"from": from_uid, "to": to_uid, "note": payload.note}))
        con.commit()

    return {"ok": True, "mouse_id": m_row["mouse_id"], "from_user_id": from_uid, "to_user_id": to_uid}


@router.get("/mice/{mouse_id}/transfers")
def list_mouse_transfers(mouse_id: str, _user: dict = Depends(auth_helpers.require_user)):
    """Transfer history for one mouse."""
    mid = (mouse_id or "").strip()
    with _con() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT t.*,
                   uf.display_name AS from_name, uf.username AS from_username,
                   ut.display_name AS to_name,   ut.username AS to_username,
                   ub.display_name AS by_name,   ub.username AS by_username
            FROM eln_mouse_transfers t
            LEFT JOIN eln_users uf ON uf.id = t.from_user_id
            LEFT JOIN eln_users ut ON ut.id = t.to_user_id
            LEFT JOIN eln_users ub ON ub.id = t.transferred_by
            WHERE LOWER(t.mouse_id) = LOWER(?)
            ORDER BY t.transferred_at DESC
        """, (mid,)).fetchall()
    return {"transfers": [dict(r) for r in rows]}


@router.get("/users-light")
def list_users_light(_user: dict = Depends(auth_helpers.require_user)):
    """Lightweight active-user list for pickers (anyone can read)."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        rows = users_dal.list_eln_active(con)
    return {"users": [dict(r) for r in rows]}


@router.get("/mice-search")
def search_mice(q: str = "", limit: int = 12,
                _user: dict = Depends(auth_helpers.require_user)):
    """Prefix/substring search on animals.mouse_id for autocomplete."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        rows = animals_dal.search(con, q, limit=limit)
    return {"results": [dict(r) for r in rows]}


@router.get("/mice/{mouse_id}")
def lookup_mouse(mouse_id: str, _user: dict = Depends(auth_helpers.require_user)):
    """Look up an animal by Mouse ID from the scheduler.db `animals` table.
    Returns a normalized object the ELN entry form can prefill from.
    404 if not in the synced animal DB — the user can then fill in manually.
    """
    mouse_id = (mouse_id or "").strip()
    if not mouse_id:
        raise HTTPException(400, "Mouse ID is required.")
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = animals_dal.get(con, mouse_id)
    if not row:
        raise HTTPException(404, "Mouse not found in animal DB.")
    days_in_cond, since = _compute_days_in_condition(row)
    return {
        "ok": True,
        "from_db": True,
        "mouse": {
            "mouse_id": row["mouse_id"],
            "strain": row["strain"],
            "sex": row["sex"],
            "dob": row["dob"],
            "age_weeks": _compute_age_weeks(row["dob"]),
            "condition": row["condition"],
            "days_in_condition": days_in_cond,
            "condition_started_on": since,
            "cohort": row["cohort"],
            "project": row["project"],
            "experiment": row["experiment"],
            "dos": row["dos"] if "dos" in row.keys() else None,
            "surgery_done": _surgery_done(row["dos"] if "dos" in row.keys() else None),
            "sacrificed": str(row["sacrificed"] or "").strip().upper() in {"Y", "YES", "1", "TRUE"},
        },
    }


@router.get("/mice/{mouse_id}/timeline")
def mouse_timeline(mouse_id: str, user: dict = Depends(auth_helpers.require_user)):
    """All ELN notes that reference this mouse across visible projects.

    Admin → all notes. Non-admin → only own notes (across projects they're members of).
    """
    mid = (mouse_id or "").strip()
    if not mid:
        raise HTTPException(400, "Mouse ID is required.")

    is_admin = user.get("effective_role") == "admin"
    with _con() as con:
        con.row_factory = sqlite3.Row

        m_row = animals_dal.get(con, mid)
        if m_row:
            days_in_cond, since = _compute_days_in_condition(m_row)
            mouse_info = {
                "from_db": True,
                "mouse_id": m_row["mouse_id"],
                "strain": m_row["strain"],
                "sex": m_row["sex"],
                "dob": m_row["dob"],
                "age_weeks": _compute_age_weeks(m_row["dob"]),
                "condition": m_row["condition"],
                "days_in_condition": days_in_cond,
                "condition_started_on": since,
                "cohort": m_row["cohort"],
                "project": m_row["project"],
                "experiment": m_row["experiment"],
                "sacrificed": str(m_row["sacrificed"] or "").strip().upper() in {"Y", "YES", "1", "TRUE"},
            }
        else:
            mouse_info = {"from_db": False, "mouse_id": mid}

        if is_admin:
            proj_rows = con.execute("SELECT slug, title FROM eln_projects").fetchall()
        else:
            proj_rows = con.execute("""
                SELECT p.slug, p.title FROM eln_projects p
                JOIN eln_project_members m ON m.project_slug = p.slug
                WHERE m.user_id = ?
            """, (user["id"],)).fetchall()
        proj_titles = {p["slug"]: p["title"] for p in proj_rows}
        if not proj_rows:
            return {"mouse": mouse_info, "entries": [], "is_admin": is_admin}

        ph = ",".join(["?"] * len(proj_rows))
        proj_slugs = [p["slug"] for p in proj_rows]
        if is_admin:
            note_rows = con.execute(f"""
                SELECT n.*, u.username, u.display_name, u.color, u.initials
                FROM eln_notes n
                LEFT JOIN eln_users u ON u.id = n.user_id
                WHERE n.project_slug IN ({ph})
            """, proj_slugs).fetchall()
        else:
            note_rows = con.execute(f"""
                SELECT n.*, u.username, u.display_name, u.color, u.initials
                FROM eln_notes n
                LEFT JOIN eln_users u ON u.id = n.user_id
                WHERE n.project_slug IN ({ph}) AND n.user_id = ?
            """, proj_slugs + [user["id"]]).fetchall()

        mid_lower = mid.lower()
        entries_out = []
        for r in note_rows:
            try:
                payload = json.loads(r["payload"] or "{}")
            except Exception:
                continue
            for e in (payload.get("entries") or []):
                if (e.get("mouse_id") or "").strip().lower() != mid_lower:
                    continue
                entries_out.append({
                    "note_id":        r["id"],
                    "user_id":        r["user_id"],
                    "approved_by":    r["approved_by"],
                    "project_slug":   r["project_slug"],
                    "project_title":  proj_titles.get(r["project_slug"], r["project_slug"]),
                    "experiment_type": r["experiment_type"],
                    "note_date":      r["note_date"],
                    "status":         r["status"],
                    "updated_at":     r["updated_at"],
                    "author": {
                        "id":           r["user_id"],
                        "display_name": r["display_name"] or r["username"],
                        "username":     r["username"],
                        "color":        r["color"],
                        "initials":     r["initials"],
                    },
                    "entry": e,
                })
        entries_out.sort(key=lambda x: (x["note_date"] or "", x["updated_at"] or ""), reverse=True)

    return {"mouse": mouse_info, "entries": entries_out, "is_admin": is_admin}


# ====================================================================
# ──── NOTES — per project + experiment_type + date ──────────────────
# ====================================================================
def _check_project_access(con: sqlite3.Connection, slug: str, user: dict) -> str:
    """Return viewer_role ('read'/'write'/'admin') or raise 403. Admin mode bypasses membership."""
    if user.get("effective_role") == "admin":
        if not con.execute("SELECT 1 FROM eln_projects WHERE slug = ?", (slug,)).fetchone():
            raise HTTPException(404, "Project not found.")
        return "admin"
    row = con.execute(
        "SELECT role FROM eln_project_members WHERE project_slug = ? AND user_id = ?",
        (slug, user["id"]),
    ).fetchone()
    if not row:
        raise HTTPException(403, "You don't have access to this project.")
    return row["role"]


def _row_to_note(row, include_payload: bool = True) -> dict:
    return {
        "id": row["id"],
        "project_slug": row["project_slug"],
        "user_id": row["user_id"],
        "experiment_type": row["experiment_type"],
        "note_date": row["note_date"],
        "status": row["status"],
        "submitted_at": row["submitted_at"],
        "approved_at": row["approved_at"],
        "approved_by": row["approved_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "payload": json.loads(row["payload"] or "{}") if include_payload else None,
    }


class CreateNoteIn(BaseModel):
    project_slug: str
    experiment_type: str
    note_date: str  # YYYY-MM-DD
    payload: dict = Field(default_factory=dict)


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXP_TYPES = {"slice_recording", "behavior", "histology", "overview"}


@router.post("/notes")
def create_note(payload: CreateNoteIn, user: dict = Depends(auth_helpers.require_user)):
    if payload.experiment_type not in EXP_TYPES:
        raise HTTPException(400, f"experiment_type must be one of {sorted(EXP_TYPES)}.")
    if not DATE_RE.match(payload.note_date):
        raise HTTPException(400, "note_date must be YYYY-MM-DD.")
    with _con() as con:
        con.row_factory = sqlite3.Row
        _check_project_access(con, payload.project_slug, user)
        # A user may run more than one experiment of the same kind on the same
        # day, so multiple notes per (project, exp_type, date, user) are allowed.
        note_id = secrets.token_hex(8)
        now = _now_iso()
        con.execute(
            """INSERT INTO eln_notes
                (id, project_slug, user_id, experiment_type, note_date, payload, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
            (note_id, payload.project_slug, user["id"], payload.experiment_type, payload.note_date,
             json.dumps(payload.payload), now, now),
        )
        auth_helpers.audit(con, user["id"], "create_note", "note", note_id,
                           json.dumps({"project": payload.project_slug, "exp": payload.experiment_type,
                                       "date": payload.note_date}))
        con.commit()
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
    return {"ok": True, "note": _row_to_note(row)}


class UpdateNoteIn(BaseModel):
    payload: dict


@router.patch("/notes/{note_id}")
def update_note(note_id: str, payload: UpdateNoteIn, user: dict = Depends(auth_helpers.require_user)):
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Note not found.")
        _check_project_access(con, row["project_slug"], user)
        is_owner = row["user_id"] == user["id"]
        is_admin = user.get("effective_role") == "admin"
        if not (is_owner or is_admin):
            raise HTTPException(403, "You can only edit your own notes.")
        if row["status"] in {"submitted", "approved"} and not is_admin:
            raise HTTPException(403, "This note is locked (submitted/approved). Ask an admin to return it for edit.")
        saved_payload = json.loads(row["payload"] or "{}")
        saved_payload.update(payload.payload)
        con.execute(
            "UPDATE eln_notes SET payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps(saved_payload), _now_iso(), note_id),
        )
        auth_helpers.audit(con, user["id"], "update_note", "note", note_id)
        con.commit()
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
    return {"ok": True, "note": _row_to_note(row)}


@router.post("/notes/{note_id}/submit")
def submit_note(note_id: str, user: dict = Depends(auth_helpers.require_user)):
    """Lock a note: draft → submitted. Only the owner can submit their own."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Note not found.")
        if row["user_id"] != user["id"]:
            raise HTTPException(403, "Only the note's author can submit it.")
        if row["status"] != "draft":
            raise HTTPException(409, f"Note is {row['status']}, cannot submit.")
        con.execute(
            "UPDATE eln_notes SET status='submitted', submitted_at=?, updated_at=? WHERE id=?",
            (_now_iso(), _now_iso(), note_id),
        )
        auth_helpers.audit(con, user["id"], "submit_note", "note", note_id)
        con.commit()
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
    return {"ok": True, "note": _row_to_note(row)}


@router.post("/notes/{note_id}/approve")
def approve_note(note_id: str, user: dict = Depends(auth_helpers.require_user)):
    """Finalize (lock) a note. The note's own author can finalize it; admins can
    finalize any. (Admin-only `return`/unlock still applies for re-opening.)"""
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Note not found.")
        if not (row["user_id"] == user["id"] or user.get("effective_role") == "admin"):
            raise HTTPException(403, "Only the note's author or an admin can finalize it.")
        if row["status"] not in {"submitted", "draft"}:
            raise HTTPException(409, f"Cannot approve a {row['status']} note.")
        con.execute(
            "UPDATE eln_notes SET status='approved', approved_at=?, approved_by=?, updated_at=? WHERE id=?",
            (_now_iso(), user["id"], _now_iso(), note_id),
        )
        auth_helpers.audit(con, user["id"], "approve_note", "note", note_id)
        con.commit()
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
    return {"ok": True, "note": _row_to_note(row)}


class PushRecordIn(BaseModel):
    folders: list = Field(default_factory=list)
    destinations: list = Field(default_factory=list)


@router.post("/notes/{note_id}/push-record")
def push_record(note_id: str, payload: PushRecordIn,
                user: dict = Depends(auth_helpers.require_user)):
    """Record (append-only, in eln_audit) that a finalized note's data folder(s)
    were copied to local push destinations. Author or admin; works on locked
    notes so provenance can be written after the lock."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT id, user_id FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Note not found.")
        if not (row["user_id"] == user["id"] or user.get("effective_role") == "admin"):
            raise HTTPException(403, "Not allowed.")
        auth_helpers.audit(con, user["id"], "push_data", "note", note_id,
                           json.dumps({"folders": payload.folders, "destinations": payload.destinations}))
        con.commit()
    return {"ok": True}


class CopyFoldersIn(BaseModel):
    destination: str


@router.post("/notes/{note_id}/copy-folders")
def copy_folders(note_id: str, payload: CopyFoldersIn,
                 user: dict = Depends(auth_helpers.require_user)):
    """Copy all data folders in a note's VOR/OKR entries to a destination path.
    Originals are never removed. Works on draft and finalized notes."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Note not found.")
        if not (row["user_id"] == user["id"] or user.get("effective_role") == "admin"):
            raise HTTPException(403, "Not allowed.")

        dest_root = Path(payload.destination.strip())
        try:
            dest_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"Cannot create destination: {e}")

        note_payload = json.loads(row["payload"] or "{}")
        entries = note_payload.get("entries", [])

        seen: set = set()
        results = []
        for entry in entries:
            for log in entry.get("vor_okr_log", []):
                for key in ("full_path", "calib_folder_path"):
                    src_str = log.get(key)
                    if not src_str or src_str in seen:
                        continue
                    seen.add(src_str)
                    src = Path(src_str)
                    if not src.exists():
                        results.append({"folder": src.name, "dest": str(dest_root), "ok": False,
                                        "error": "Source folder not found."})
                        continue
                    dst = dest_root / src.name
                    try:
                        shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
                        results.append({"folder": src.name, "dest": str(dst), "ok": True})
                    except Exception as e:
                        results.append({"folder": src.name, "dest": str(dst), "ok": False,
                                        "error": str(e)[:200]})

        auth_helpers.audit(con, user["id"], "push_data", "note", note_id,
                           json.dumps({"destination": str(dest_root),
                                       "results": results}))
        con.commit()
    return {"ok": True, "results": results}


DISPOSITIONS = {"", "include", "exclude", "retest", "drop"}


class EntryDispositionIn(BaseModel):
    entry_index: int
    disposition: str


@router.post("/notes/{note_id}/entry-disposition")
def set_entry_disposition(note_id: str, payload: EntryDispositionIn,
                          user: dict = Depends(auth_helpers.require_user)):
    """Set include/exclude/retest/drop on a single animal entry. Mirrors
    push-record: author-or-admin, and deliberately has NO status gate so the
    disposition can be recorded after the note is finalized (locked). Stamps the
    date the decision was made."""
    from datetime import date
    dispo = (payload.disposition or "").strip().lower()
    if dispo not in DISPOSITIONS:
        raise HTTPException(400, f"disposition must be one of {sorted(DISPOSITIONS - {''})} or empty.")
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT id, user_id, payload FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Note not found.")
        if not (row["user_id"] == user["id"] or user.get("effective_role") == "admin"):
            raise HTTPException(403, "Not allowed.")
        note_payload = json.loads(row["payload"] or "{}")
        entries = note_payload.get("entries", [])
        if not (0 <= payload.entry_index < len(entries)):
            raise HTTPException(400, "entry_index out of range.")
        entry = entries[payload.entry_index]
        if dispo:
            entry["disposition"] = dispo
            entry["disposition_at"] = date.today().isoformat()
            entry["disposition_by"] = user["id"]
        else:
            entry["disposition"] = ""
            entry["disposition_at"] = ""
            entry["disposition_by"] = ""
        con.execute(
            "UPDATE eln_notes SET payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps(note_payload), _now_iso(), note_id),
        )
        auth_helpers.audit(con, user["id"], "set_disposition", "note", note_id,
                           json.dumps({"entry_index": payload.entry_index,
                                       "mouse_id": entry.get("mouse_id"),
                                       "disposition": dispo}))
        con.commit()
    return {"ok": True, "entry_index": payload.entry_index,
            "disposition": entry["disposition"], "disposition_at": entry["disposition_at"]}


@router.post("/notes/{note_id}/archive-mice")
def archive_note_mice(note_id: str, user: dict = Depends(auth_helpers.require_user)):
    """Move the mice on a slice-recording note into the sacrificed archive.
    Author-or-admin — works for the note's own author even in member mode, since
    slice recording is terminal and the person who ran it retires the animals.
    Idempotent: mice already archived / not in the colony are skipped."""
    from server import require_animal_removal_source
    require_animal_removal_source()
    now = _now_iso()
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT id, user_id, experiment_type, note_date, payload FROM eln_notes WHERE id=?",
            (note_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Note not found.")
        if not (row["user_id"] == user["id"] or user.get("effective_role") == "admin"):
            raise HTTPException(403, "Only the note's author or an admin can archive its mice.")
        if row["experiment_type"] != "slice_recording":
            raise HTTPException(400, "Only slice-recording notes can archive mice.")
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        mids: list = []
        for e in payload.get("entries", []):
            m = (e.get("mouse_id") or "").strip()
            if m and m not in mids:
                mids.append(m)
        sdate = row["note_date"]
        archived: list = []
        skipped: list = []
        for mid in mids:
            a = animals_dal.get(con, mid, case_insensitive=True)
            if not a:
                skipped.append(mid)
                continue
            a = dict(a)
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
                now, "sacrificed — slice recording",
            ))
            animals_dal.delete(con, a["mouse_id"])
            archived.append(a["mouse_id"])
        auth_helpers.audit(con, user["id"], "archive_mice", "note", note_id,
                           json.dumps({"archived": archived, "skipped": skipped}))
        con.commit()
    return {"ok": True, "archived": archived, "skipped": skipped}


@router.post("/notes/{note_id}/return")
def return_note(note_id: str, _admin: dict = Depends(auth_helpers.require_admin)):
    """Unlock a submitted/approved note back to draft for further edits."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Note not found.")
        con.execute(
            "UPDATE eln_notes SET status='draft', submitted_at=NULL, approved_at=NULL, approved_by=NULL, updated_at=? WHERE id=?",
            (_now_iso(), note_id),
        )
        auth_helpers.audit(con, _admin["id"], "return_note", "note", note_id)
        con.commit()
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
    return {"ok": True, "note": _row_to_note(row)}


@router.delete("/notes/{note_id}")
def delete_note(note_id: str, user: dict = Depends(auth_helpers.require_user)):
    """Delete a note. Admins (admin mode) may delete any note; an author may
    delete only their own note while it is still a draft."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Note not found.")
        _check_project_access(con, row["project_slug"], user)
        is_admin = user.get("effective_role") == "admin"
        is_owner = row["user_id"] == user["id"]
        if not is_admin:
            if not is_owner:
                raise HTTPException(403, "You can only delete your own notes.")
            if row["status"] != "draft":
                raise HTTPException(403, "This note is locked. Ask an admin to delete it.")
        con.execute("DELETE FROM eln_notes WHERE id = ?", (note_id,))
        auth_helpers.audit(con, user["id"], "delete_note", "note", note_id,
                           json.dumps({"project": row["project_slug"], "exp": row["experiment_type"],
                                       "date": row["note_date"], "status": row["status"],
                                       "author": row["user_id"]}))
        con.commit()
    return {"ok": True}


@router.get("/notes/{note_id}")
def get_note(note_id: str, user: dict = Depends(auth_helpers.require_user)):
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Note not found.")
        _check_project_access(con, row["project_slug"], user)
        # Non-admin can only view their own notes
        if row["user_id"] != user["id"] and user.get("effective_role") != "admin":
            raise HTTPException(403, "You can only view your own notes.")
        # Author info
        author = con.execute(
            "SELECT id, username, display_name, color, initials FROM eln_users WHERE id = ?",
            (row["user_id"],),
        ).fetchone()
    note = _row_to_note(row)
    note["author"] = dict(author) if author else None
    return {"ok": True, "note": note}


@router.get("/projects/{slug}/notes")
def list_notes(slug: str, experiment_type: str, date: Optional[str] = None,
               date_from: Optional[str] = None, date_to: Optional[str] = None,
               author_id: Optional[str] = None,
               user: dict = Depends(auth_helpers.require_user)):
    """List notes for a project + experiment_type. Filter by single date, date range, or author.

    Non-admin: only sees own notes (author_id ignored, always self).
    Admin mode: sees everyone's notes; can filter by author_id.
    """
    with _con() as con:
        con.row_factory = sqlite3.Row
        _check_project_access(con, slug, user)
        if experiment_type not in EXP_TYPES:
            raise HTTPException(400, "Invalid experiment_type.")

        clauses = ["project_slug = ?", "experiment_type = ?"]
        params: list = [slug, experiment_type]
        if user.get("effective_role") != "admin":
            clauses.append("user_id = ?")
            params.append(user["id"])
        elif author_id:
            clauses.append("user_id = ?")
            params.append(author_id)
        if date:
            if not DATE_RE.match(date):
                raise HTTPException(400, "date must be YYYY-MM-DD.")
            clauses.append("note_date = ?")
            params.append(date)
        if date_from:
            clauses.append("note_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("note_date <= ?")
            params.append(date_to)
        rows = con.execute(
            f"SELECT * FROM eln_notes WHERE {' AND '.join(clauses)} ORDER BY note_date DESC, updated_at DESC",
            params,
        ).fetchall()
        # Attach author info per note
        out = []
        for r in rows:
            n = _row_to_note(r)
            author = con.execute(
                "SELECT id, username, display_name, color, initials FROM eln_users WHERE id = ?",
                (r["user_id"],),
            ).fetchone()
            n["author"] = dict(author) if author else None
            out.append(n)
    return {"notes": out}


# ---------- PDF export helpers ----------

def _normalize_entry_py(e: dict) -> dict:
    if "recordings" in e:
        return e
    if "cells" in e:
        recs = [{"cell": c.get("cell", ""), "protocol": c.get("protocol", ""), "notes": c.get("notes", "")}
                for c in e.get("cells", [])]
        return {**e, "recordings": recs}
    if e.get("cell") or e.get("protocol") or e.get("notes"):
        return {**e, "recordings": [{"cell": e.get("cell", ""), "protocol": e.get("protocol", ""), "notes": e.get("notes", "")}]}
    return {**e, "recordings": []}


def _yymmdd(iso: str) -> str:
    """ISO date (YYYY-MM-DD…) → YYMMDD, or '' if unparseable."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(iso or ""))
    return (m.group(1)[2:] + m.group(2) + m.group(3)) if m else ""


def _compute_filename_py(entry: dict, rec: dict, initials: str = "", note_date: str = "") -> str:
    cohort_cell = (entry.get("cohort") or "") + (rec.get("cell") or "")
    run_str     = str(int(rec.get("run_index") or 0)).zfill(4)
    exc_sfx     = "_exclude" if rec.get("excluded") else ""
    parts = [p for p in [
        _yymmdd(note_date),
        initials or "",
        entry.get("exp_condition") or "",
        entry.get("header_name") or entry.get("exp_type") or "",
        cohort_cell,
        rec.get("protocol") or "",
    ] if p]
    return ("_".join(parts) + f"_{run_str}{exc_sfx}.abf") if parts else ""


def _fmt_date_long(iso: str) -> str:
    from datetime import date as _date
    try:
        return _date.fromisoformat(iso).strftime("%A, %B %d, %Y")
    except Exception:
        return iso


def _notes_to_pdf_bytes(proj_title: str, experiment_type: str, notes: list,
                        generated_by: str, range_label: str) -> bytes:
    """Render enriched note dicts (each carrying ['author']) to protected PDF bytes.

    Shared by the range export and the single-note finalize PDF so both stay in sync,
    including the YYMMDD-prefixed .abf filenames.
    """
    try:
        import io
        from itertools import groupby
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.pdfencrypt import StandardEncryption
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            HRFlowable, KeepTogether,
        )
    except ImportError:
        raise HTTPException(500, "reportlab is not installed on the server.")

    buf = io.BytesIO()
    enc = StandardEncryption(
        userPassword="",
        ownerPassword="lab-os-owner-2024",
        canPrint=1, canModify=0, canCopy=0, canAnnotate=0,
        strength=128,
    )
    exp_label = experiment_type.replace("_", " ").title()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2.5*cm, bottomMargin=2.5*cm,
        encrypt=enc,
        title=f"{proj_title} — {exp_label}",
        author=generated_by,
    )

    base = getSampleStyleSheet()
    S = {
        "proj_title": ParagraphStyle("proj_title", parent=base["Title"],
                                     fontSize=18, spaceAfter=2),
        "sub":        ParagraphStyle("sub", parent=base["Normal"],
                                     fontSize=12, spaceAfter=2),
        "meta":       ParagraphStyle("meta", parent=base["Normal"],
                                     fontSize=9, textColor=colors.grey, spaceAfter=10),
        "date_h":     ParagraphStyle("date_h", parent=base["Heading2"],
                                     fontSize=13, spaceBefore=16, spaceAfter=4,
                                     textColor=colors.HexColor("#1a3a5c")),
        "note_meta":  ParagraphStyle("note_meta", parent=base["Normal"],
                                     fontSize=9, textColor=colors.HexColor("#555555"), spaceAfter=4),
        "animal":     ParagraphStyle("animal", parent=base["Normal"],
                                     fontSize=10, spaceBefore=6, spaceAfter=2,
                                     fontName="Helvetica-Bold"),
        "empty":      ParagraphStyle("empty", parent=base["Normal"],
                                     fontSize=9, textColor=colors.grey, spaceAfter=4),
    }

    STATUS_COLOR = {"approved": "#059669", "submitted": "#d97706", "draft": "#6b7280"}

    story: list = []
    story.append(Paragraph(proj_title, S["proj_title"]))
    story.append(Paragraph(f"{exp_label} — Lab Notes · {range_label}", S["sub"]))
    story.append(Paragraph(f"Generated {generated_at} by {generated_by}", S["meta"]))
    story.append(HRFlowable(width="100%", thickness=0.5,
                            color=colors.HexColor("#cccccc"), spaceAfter=12))

    if not notes:
        story.append(Paragraph("No notes found for this project.", S["empty"]))
    else:
        for date_str, day_iter in groupby(notes, key=lambda n: n["note_date"]):
            day_notes = list(day_iter)
            story.append(Paragraph(_fmt_date_long(date_str), S["date_h"]))

            for note in day_notes:
                a = note.get("author") or {}
                a_name = a.get("display_name") or a.get("username") or "—"
                a_initials = a.get("initials") or ""
                status = note.get("status", "draft")
                sc = STATUS_COLOR.get(status, "#6b7280")
                story.append(Paragraph(
                    f"<b>{a_name}</b> &nbsp;·&nbsp; "
                    f"<font color='{sc}'>{status.upper()}</font>",
                    S["note_meta"],
                ))

                entries = (note.get("payload") or {}).get("entries") or []
                if not entries:
                    story.append(Paragraph("No entries.", S["empty"]))
                    continue

                for entry in entries:
                    entry = _normalize_entry_py(entry)
                    chips = []
                    if entry.get("mouse_id"):      chips.append(f"Mouse: <b>{entry['mouse_id']}</b>")
                    if entry.get("agent"):         chips.append(f"Agent: {entry['agent']}")
                    if entry.get("exp_condition"): chips.append(f"Condition: {entry['exp_condition']}")
                    if entry.get("exp_type"):      chips.append(f"Type: {entry['exp_type']}")
                    if entry.get("cohort"):        chips.append(f"Cohort: {entry['cohort']}")
                    story.append(Paragraph(
                        " &nbsp;·&nbsp; ".join(chips) if chips else "(no animal info)",
                        S["animal"],
                    ))

                    tbl_style = TableStyle([
                        ("BACKGROUND",     (0, 0), (-1, 0),  colors.HexColor("#1a3a5c")),
                        ("TEXTCOLOR",      (0, 0), (-1, 0),  colors.white),
                        ("FONTNAME",       (0, 0), (-1, 0),  "Helvetica-Bold"),
                        ("FONTSIZE",       (0, 0), (-1, -1), 8),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4f8")]),
                        ("GRID",           (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                        ("LEFTPADDING",    (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING",   (0, 0), (-1, -1), 4),
                        ("TOPPADDING",     (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING",  (0, 0), (-1, -1), 3),
                        ("VALIGN",         (0, 0), (-1, -1), "TOP"),
                    ])

                    if experiment_type == "behavior":
                        tasks = entry.get("behavior_tasks") or []
                        if tasks:
                            story.append(Paragraph("Behavior tasks: <b>" + ", ".join(tasks) + "</b>", S["animal"]))
                        rec = entry.get("recording") or {}
                        mods = []
                        if rec.get("photometry", {}).get("enabled"):
                            ph = rec["photometry"]
                            mods.append(f"Photometry (site={ph.get('site','—')}, indicator={ph.get('indicator','—')}, channel={ph.get('channel','—')})")
                        if rec.get("ephys", {}).get("enabled"):
                            ep = rec["ephys"]
                            mods.append(f"ePhys (type={ep.get('type','—')}, site={ep.get('site','—')})")
                        if rec.get("opto", {}).get("enabled"):
                            op = rec["opto"]
                            mods.append(f"Opto (opsin={op.get('opsin','—')}, {op.get('n_pulses','—')} pulses @ {op.get('frequency','—')}, power={op.get('power','—')})")
                        if rec.get("chemogenetic", {}).get("enabled"):
                            ch = rec["chemogenetic"]
                            mods.append(f"Chemogenetic (agent={ch.get('agent','—')}, dose={ch.get('dose','—')}, route={ch.get('route','—')})")
                        if rec.get("drug", {}).get("enabled"):
                            dr = rec["drug"]
                            mods.append(f"Drug (name={dr.get('name','—')}, dose={dr.get('dose','—')}, route={dr.get('route','—')})")
                        if rec.get("target_region"):
                            story.append(Paragraph(f"Target region: <b>{rec['target_region']}</b>", S["animal"]))
                        if mods:
                            for line in mods:
                                story.append(Paragraph("• " + line, S["animal"]))
                        sessions = entry.get("sessions") or []
                        if sessions:
                            tbl_data = [["Task", "Score", "Notes"]]
                            for s in sessions:
                                tbl_data.append([
                                    s.get("task", "") or "",
                                    s.get("score", "") or "",
                                    s.get("notes", "") or "",
                                ])
                            tbl = Table(tbl_data, colWidths=[3.5*cm, 2.5*cm, 9*cm], repeatRows=1)
                            tbl.setStyle(tbl_style)
                            story.append(KeepTogether([tbl, Spacer(1, 4)]))
                        elif not tasks and not mods:
                            story.append(Paragraph("No behavior data.", S["empty"]))
                    else:
                        recordings = entry.get("recordings") or []
                        if recordings:
                            tbl_data = [["Cell", "Protocol", "Filename", "Notes"]]
                            for rec in recordings:
                                tbl_data.append([
                                    rec.get("cell", "") or "",
                                    rec.get("protocol", "") or "",
                                    _compute_filename_py(entry, rec, a_initials, note.get("note_date", "")),
                                    rec.get("notes", "") or "",
                                ])
                            tbl = Table(tbl_data, colWidths=[1.4*cm, 3.2*cm, 6.2*cm, 4.2*cm], repeatRows=1)
                            tbl.setStyle(tbl_style)
                            story.append(KeepTogether([tbl, Spacer(1, 4)]))
                        else:
                            story.append(Paragraph("No recordings.", S["empty"]))

            story.append(Spacer(1, 6))

    doc.build(story)
    buf.seek(0)
    return buf.read()


@router.get("/projects/{slug}/notes/export-pdf")
def export_notes_pdf(slug: str, experiment_type: str,
                     date_from: Optional[str] = None,
                     date_to: Optional[str] = None,
                     user: dict = Depends(auth_helpers.require_user)):
    """Export notes for a project + experiment_type as a protected, locked PDF.

    date_from / date_to (YYYY-MM-DD): optional inclusive range. Omit both for all dates.
    """
    for d in (date_from, date_to):
        if d and not DATE_RE.match(d):
            raise HTTPException(400, "date_from / date_to must be YYYY-MM-DD.")

    with _con() as con:
        con.row_factory = sqlite3.Row
        _check_project_access(con, slug, user)
        if experiment_type not in EXP_TYPES:
            raise HTTPException(400, "Invalid experiment_type.")

        proj_row = con.execute("SELECT title FROM eln_projects WHERE slug = ?", (slug,)).fetchone()
        proj_title = proj_row["title"] if proj_row else slug

        clauses = ["project_slug = ?", "experiment_type = ?"]
        params: list = [slug, experiment_type]
        if user.get("effective_role") != "admin":
            clauses.append("user_id = ?")
            params.append(user["id"])
        if date_from:
            clauses.append("note_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("note_date <= ?")
            params.append(date_to)
        rows = con.execute(
            f"SELECT * FROM eln_notes WHERE {' AND '.join(clauses)} ORDER BY note_date ASC, updated_at ASC",
            params,
        ).fetchall()

        notes = []
        for r in rows:
            n = _row_to_note(r)
            author = con.execute(
                "SELECT id, username, display_name, initials FROM eln_users WHERE id = ?",
                (r["user_id"],),
            ).fetchone()
            n["author"] = dict(author) if author else None
            notes.append(n)

    if date_from and date_to:
        range_label = f"{date_from} – {date_to}"
    elif date_from:
        range_label = f"From {date_from}"
    elif date_to:
        range_label = f"Up to {date_to}"
    else:
        range_label = "All dates"
    generated_by = user.get("display_name") or user.get("username") or ""
    pdf_bytes = _notes_to_pdf_bytes(proj_title, experiment_type, notes, generated_by, range_label)

    date_suffix = f"{date_from or 'start'}_{date_to or 'end'}" if (date_from or date_to) else "all"
    filename = f"{slug}_{experiment_type}_{date_suffix}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/notes/{note_id}/pdf")
def note_pdf(note_id: str, user: dict = Depends(auth_helpers.require_user)):
    """Render a single note to a protected PDF. Used when finalizing a slice note so a
    copy of the day's lab note can be dropped into its data folder."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM eln_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Note not found.")
        _check_project_access(con, row["project_slug"], user)
        if user.get("effective_role") != "admin" and row["user_id"] != user["id"]:
            raise HTTPException(403, "You can only export your own notes.")
        slug            = row["project_slug"]
        experiment_type = row["experiment_type"]
        proj_row = con.execute("SELECT title FROM eln_projects WHERE slug = ?", (slug,)).fetchone()
        proj_title = proj_row["title"] if proj_row else slug
        note = _row_to_note(row)
        author = con.execute(
            "SELECT id, username, display_name, initials FROM eln_users WHERE id = ?",
            (row["user_id"],),
        ).fetchone()
        note["author"] = dict(author) if author else None

    generated_by = user.get("display_name") or user.get("username") or ""
    range_label  = _fmt_date_long(note["note_date"])
    pdf_bytes = _notes_to_pdf_bytes(proj_title, experiment_type, [note], generated_by, range_label)
    filename = f"{note['note_date']}_{slug}_{experiment_type}_labnote.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/projects/{slug}/notes/calendar")
def notes_calendar(slug: str, experiment_type: str,
                   year: int, month: int,
                   author_id: Optional[str] = None,
                   user: dict = Depends(auth_helpers.require_user)):
    """Return a list of {date: 'YYYY-MM-DD', count: N, statuses: [...]} for the calendar
    sidebar in a given year+month. Non-admin sees own; admin can filter."""
    with _con() as con:
        con.row_factory = sqlite3.Row
        _check_project_access(con, slug, user)
        start = f"{year:04d}-{month:02d}-01"
        # Compute next month for upper bound
        nm = month + 1
        ny = year
        if nm > 12:
            nm = 1; ny = year + 1
        end_excl = f"{ny:04d}-{nm:02d}-01"
        params = [slug, experiment_type, start, end_excl]
        clauses = "project_slug = ? AND experiment_type = ? AND note_date >= ? AND note_date < ?"
        if user.get("effective_role") != "admin":
            clauses += " AND user_id = ?"
            params.append(user["id"])
        elif author_id:
            clauses += " AND user_id = ?"
            params.append(author_id)
        rows = con.execute(
            f"""SELECT note_date,
                       COUNT(*) AS count,
                       GROUP_CONCAT(DISTINCT status) AS statuses,
                       GROUP_CONCAT(DISTINCT user_id) AS user_ids
                  FROM eln_notes
                 WHERE {clauses}
              GROUP BY note_date
              ORDER BY note_date""",
            params,
        ).fetchall()
    return {
        "days": [
            {"date": r["note_date"], "count": r["count"],
             "statuses": (r["statuses"] or "").split(","),
             "author_count": len((r["user_ids"] or "").split(",")) if r["user_ids"] else 0}
            for r in rows
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Global config  (key-value store — admin only for writes)
# ═══════════════════════════════════════════════════════════════════════════════

_DEFAULT_DATA_ROOT = str(Path.home() / "Desktop" / "LabData")


def _get_config(key: str) -> Optional[str]:
    with _con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT value FROM eln_config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


# ═══════════════════════════════════════════════════════════════════════════════
# Naming conventions  (dropdown options for exp_condition / exp_type / protocol)
# ═══════════════════════════════════════════════════════════════════════════════

_ALLOWED_NC_CATEGORIES = {"exp_condition", "exp_type", "protocol", "agent", "cell_type", "behavior_task"}


class NamingConventionIn(BaseModel):
    category: str
    value:    str
    label:    Optional[str] = None
    parent:   Optional[str] = None
    sort_order: int = 0


# ═══════════════════════════════════════════════════════════════════════════════
# Dashboard  (cross-project per-user aggregates)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/dashboard")
def dashboard(user: dict = Depends(auth_helpers.require_user)):
    """Per-user / per-project note counts across all visible projects.

    Admin → sees everyone × all projects.
    Non-admin → only own notes across visible projects.
    """
    is_admin = user.get("effective_role") == "admin"
    with _con() as con:
        con.row_factory = sqlite3.Row
        if is_admin:
            projects = con.execute(
                "SELECT slug, title, kind FROM eln_projects ORDER BY title"
            ).fetchall()
        else:
            projects = con.execute("""
                SELECT p.slug, p.title, p.kind FROM eln_projects p
                JOIN eln_project_members m ON m.project_slug = p.slug
                WHERE m.user_id = ?
                ORDER BY p.title
            """, (user["id"],)).fetchall()

        proj_slugs = [p["slug"] for p in projects]
        if not proj_slugs:
            return {"generated_at": _now_iso(), "users": [], "projects": [], "is_admin": is_admin}

        ph = ",".join(["?"] * len(proj_slugs))
        if is_admin:
            note_rows = con.execute(f"""
                SELECT n.project_slug, n.user_id, n.experiment_type, n.note_date,
                       n.status, n.updated_at,
                       u.username, u.display_name, u.color, u.initials
                FROM eln_notes n
                LEFT JOIN eln_users u ON u.id = n.user_id
                WHERE n.project_slug IN ({ph})
            """, proj_slugs).fetchall()
        else:
            note_rows = con.execute(f"""
                SELECT n.project_slug, n.user_id, n.experiment_type, n.note_date,
                       n.status, n.updated_at,
                       u.username, u.display_name, u.color, u.initials
                FROM eln_notes n
                LEFT JOIN eln_users u ON u.id = n.user_id
                WHERE n.project_slug IN ({ph}) AND n.user_id = ?
            """, proj_slugs + [user["id"]]).fetchall()

        users: dict = {}
        proj_stats = {
            p["slug"]: {
                "slug": p["slug"], "title": p["title"], "kind": p["kind"],
                "total_notes": 0, "draft": 0, "submitted": 0, "finalized": 0,
                "slice_recording": 0, "behavior": 0,
                "last_active": None, "_user_ids": set(),
            }
            for p in projects
        }

        for r in note_rows:
            uid = r["user_id"]
            if uid not in users:
                users[uid] = {
                    "id": uid,
                    "display_name": r["display_name"] or r["username"] or uid,
                    "username": r["username"],
                    "color": r["color"],
                    "initials": r["initials"],
                    "slice_recording": 0,
                    "behavior": 0,
                    "draft": 0,
                    "submitted": 0,
                    "finalized": 0,
                    "last_active": None,
                    "by_project": {},
                }
            u = users[uid]
            et = r["experiment_type"]
            if et in ("slice_recording", "behavior"):
                u[et] += 1
            if r["status"] == "draft":
                u["draft"] += 1
            elif r["status"] == "submitted":
                u["submitted"] += 1
            else:
                u["finalized"] += 1
            if not u["last_active"] or (r["updated_at"] and r["updated_at"] > u["last_active"]):
                u["last_active"] = r["updated_at"]
            bp = u["by_project"].setdefault(r["project_slug"], {"slice_recording": 0, "behavior": 0})
            if et in bp:
                bp[et] += 1

            ps = proj_stats[r["project_slug"]]
            ps["total_notes"] += 1
            if et in ("slice_recording", "behavior"):
                ps[et] += 1
            if r["status"] == "draft":
                ps["draft"] += 1
            elif r["status"] == "submitted":
                ps["submitted"] += 1
            else:
                ps["finalized"] += 1
            if not ps["last_active"] or (r["updated_at"] and r["updated_at"] > ps["last_active"]):
                ps["last_active"] = r["updated_at"]
            ps["_user_ids"].add(uid)

        for ps in proj_stats.values():
            ps["unique_users"] = len(ps["_user_ids"])
            del ps["_user_ids"]

    return {
        "generated_at": _now_iso(),
        "is_admin": is_admin,
        "users": sorted(users.values(), key=lambda u: -(u["slice_recording"] + u["behavior"])),
        "projects": sorted(proj_stats.values(), key=lambda p: -p["total_notes"]),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Audit log (admin-only)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/audit")
def list_audit_log(limit: int = 200, offset: int = 0,
                   user_id: Optional[str] = None,
                   action: Optional[str] = None,
                   q: Optional[str] = None,
                   user: dict = Depends(auth_helpers.require_user)):
    """Return audit entries. Admin-only. Filterable by user_id, action, free-text q."""
    if user.get("effective_role") != "admin":
        raise HTTPException(403, "Admin only.")
    limit  = max(1, min(500, limit))
    offset = max(0, offset)

    clauses, params = [], []
    if user_id:
        clauses.append("a.user_id = ?"); params.append(user_id)
    if action:
        clauses.append("a.action = ?"); params.append(action)
    if q:
        like = f"%{q}%"
        clauses.append("(a.action LIKE ? OR a.resource_type LIKE ? OR a.resource_id LIKE ? OR a.details LIKE ?)")
        params.extend([like, like, like, like])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with _con() as con:
        con.row_factory = sqlite3.Row
        total = con.execute(f"SELECT COUNT(*) AS n FROM eln_audit a {where}", params).fetchone()["n"]
        rows = con.execute(
            f"""SELECT a.id, a.ts, a.user_id, a.action, a.resource_type, a.resource_id, a.details,
                       u.username, u.display_name, u.color, u.initials
                FROM eln_audit a
                LEFT JOIN eln_users u ON u.id = a.user_id
                {where}
                ORDER BY a.ts DESC, a.id DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        # Distinct action types for filter dropdown
        action_types = [r["action"] for r in con.execute(
            "SELECT DISTINCT action FROM eln_audit ORDER BY action"
        ).fetchall()]
        users_list = [dict(r) for r in con.execute(
            "SELECT id, display_name, username FROM eln_users ORDER BY display_name"
        ).fetchall()]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "actions": action_types,
        "users": users_list,
        "entries": [
            {
                "id": r["id"],
                "ts": r["ts"],
                "user_id": r["user_id"],
                "action": r["action"],
                "resource_type": r["resource_type"],
                "resource_id": r["resource_id"],
                "details": r["details"],
                "author": {
                    "id": r["user_id"],
                    "display_name": r["display_name"] or r["username"],
                    "username": r["username"],
                    "color": r["color"],
                    "initials": r["initials"],
                } if r["user_id"] else None,
            }
            for r in rows
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Breeder pairs
# ═══════════════════════════════════════════════════════════════════════════════

_BREEDER_STATUSES = {"active", "maintenance", "trio", "retired"}

class BreederAnimalIn(BaseModel):
    id: Optional[str] = None
    role: str   # 'male' | 'female_a' | 'female_b' | etc.
    mouse_id: Optional[str] = None
    dob: Optional[str] = None
    strain: Optional[str] = None
    genotype: Optional[str] = None
    sex: Optional[str] = None
    current_location: Optional[str] = None
    pregnant: bool = False
    notes: Optional[str] = None
    sort_order: int = 0


class PupRoundIn(BaseModel):
    id: Optional[str] = None
    female_id: Optional[str] = None
    birth_date: Optional[str] = None
    pup_count: int = 0
    weaning_date: Optional[str] = None
    male_count_weaned: int = 0
    female_count_weaned: int = 0
    notes: Optional[str] = None


class BreederIn(BaseModel):
    label: str
    strain: Optional[str] = None
    status: str = "active"
    jax_available: bool = False
    jax_url: Optional[str] = None
    setup_date: Optional[str] = None
    expected_retire_date: Optional[str] = None
    notes: Optional[str] = None
    parents: List[BreederAnimalIn] = Field(default_factory=list)
    pup_rounds: List[PupRoundIn] = Field(default_factory=list)


def _row_breeder(row, parents, pups) -> dict:
    return {
        "id":                   row["id"],
        "label":                row["label"],
        "strain":               row["strain"],
        "status":               row["status"],
        "jax_available":        bool(row["jax_available"]),
        "jax_url":              row["jax_url"],
        "setup_date":           row["setup_date"],
        "expected_retire_date": row["expected_retire_date"],
        "notes":                row["notes"],
        "created_at":           row["created_at"],
        "updated_at":           row["updated_at"],
        "parents":              parents,
        "pup_rounds":           pups,
    }


def _breeder_rate(con: sqlite3.Connection, pair_id: str) -> str:
    """Compute breeding rate from pup history. Returns 'bad' | 'moderate' | 'high' | 'unknown'."""
    rows = con.execute(
        "SELECT pup_count FROM eln_pup_rounds WHERE pair_id = ? AND pup_count > 0",
        (pair_id,),
    ).fetchall()
    if not rows:
        return "unknown"
    avg = sum(r["pup_count"] for r in rows) / len(rows)
    if avg < 4:    return "bad"
    if avg < 7:    return "moderate"
    return "high"


def _autocalc_retire(setup_date: Optional[str]) -> Optional[str]:
    """setup_date + 365 days, both ISO YYYY-MM-DD. Returns None if invalid."""
    if not setup_date:
        return None
    try:
        from datetime import date as _date, timedelta
        d = _date.fromisoformat(setup_date)
        return (d + timedelta(days=365)).isoformat()
    except Exception:
        return None


@router.get("/breeders")
def list_breeders(user: dict = Depends(auth_helpers.require_user)):
    with _con() as con:
        con.row_factory = sqlite3.Row
        pair_rows = con.execute("SELECT * FROM eln_breeder_pairs ORDER BY status, label").fetchall()
        out = []
        for pr in pair_rows:
            parents = [dict(r) for r in con.execute(
                "SELECT * FROM eln_breeder_animals WHERE pair_id = ? ORDER BY sort_order, role",
                (pr["id"],),
            ).fetchall()]
            pups = [dict(r) for r in con.execute(
                "SELECT * FROM eln_pup_rounds WHERE pair_id = ? ORDER BY birth_date DESC",
                (pr["id"],),
            ).fetchall()]
            d = _row_breeder(pr, parents, pups)
            d["breeding_rate"] = _breeder_rate(con, pr["id"])
            out.append(d)
    return {"breeders": out}


@router.get("/breeders/{breeder_id}")
def get_breeder(breeder_id: str, user: dict = Depends(auth_helpers.require_user)):
    with _con() as con:
        con.row_factory = sqlite3.Row
        pr = con.execute("SELECT * FROM eln_breeder_pairs WHERE id = ?", (breeder_id,)).fetchone()
        if not pr:
            raise HTTPException(404, "Breeder not found.")
        parents = [dict(r) for r in con.execute(
            "SELECT * FROM eln_breeder_animals WHERE pair_id = ? ORDER BY sort_order, role",
            (breeder_id,),
        ).fetchall()]
        pups = [dict(r) for r in con.execute(
            "SELECT * FROM eln_pup_rounds WHERE pair_id = ? ORDER BY birth_date DESC",
            (breeder_id,),
        ).fetchall()]
        d = _row_breeder(pr, parents, pups)
        d["breeding_rate"] = _breeder_rate(con, breeder_id)
    return {"breeder": d}


def _upsert_breeder(con, payload: BreederIn, breeder_id: Optional[str], actor_id: str) -> str:
    if payload.status not in _BREEDER_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(_BREEDER_STATUSES)}")
    now = _now_iso()
    retire = payload.expected_retire_date or _autocalc_retire(payload.setup_date)
    if breeder_id is None:
        breeder_id = uuid.uuid4().hex
        con.execute("""
            INSERT INTO eln_breeder_pairs
              (id, label, strain, status, jax_available, jax_url, setup_date,
               expected_retire_date, notes, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            breeder_id, payload.label.strip(), payload.strain, payload.status,
            1 if payload.jax_available else 0, payload.jax_url, payload.setup_date,
            retire, payload.notes, now, now,
        ))
        auth_helpers.audit(con, actor_id, "create_breeder", "breeder", breeder_id,
                           json.dumps({"label": payload.label}))
    else:
        existing = con.execute("SELECT id FROM eln_breeder_pairs WHERE id = ?", (breeder_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Breeder not found.")
        con.execute("""
            UPDATE eln_breeder_pairs SET
              label=?, strain=?, status=?, jax_available=?, jax_url=?, setup_date=?,
              expected_retire_date=?, notes=?, updated_at=?
            WHERE id=?
        """, (
            payload.label.strip(), payload.strain, payload.status,
            1 if payload.jax_available else 0, payload.jax_url, payload.setup_date,
            retire, payload.notes, now, breeder_id,
        ))
        auth_helpers.audit(con, actor_id, "update_breeder", "breeder", breeder_id, None)

    # Replace parents (idempotent — keep ids stable if provided)
    existing_parent_ids = {r["id"] for r in con.execute(
        "SELECT id FROM eln_breeder_animals WHERE pair_id = ?", (breeder_id,)
    ).fetchall()}
    incoming_ids = set()
    for p in payload.parents:
        pid = p.id or uuid.uuid4().hex
        incoming_ids.add(pid)
        if p.id and p.id in existing_parent_ids:
            con.execute("""
                UPDATE eln_breeder_animals SET
                  role=?, mouse_id=?, dob=?, strain=?, genotype=?, sex=?,
                  current_location=?, pregnant=?, notes=?, sort_order=?, updated_at=?
                WHERE id=?
            """, (p.role, p.mouse_id, p.dob, p.strain, p.genotype, p.sex,
                  p.current_location, 1 if p.pregnant else 0, p.notes, p.sort_order, now, pid))
        else:
            con.execute("""
                INSERT INTO eln_breeder_animals
                  (id, pair_id, role, mouse_id, dob, strain, genotype, sex,
                   current_location, pregnant, notes, sort_order, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (pid, breeder_id, p.role, p.mouse_id, p.dob, p.strain, p.genotype, p.sex,
                  p.current_location, 1 if p.pregnant else 0, p.notes, p.sort_order, now, now))
    for old_pid in (existing_parent_ids - incoming_ids):
        con.execute("DELETE FROM eln_breeder_animals WHERE id = ?", (old_pid,))

    # Replace pup_rounds
    existing_pup_ids = {r["id"] for r in con.execute(
        "SELECT id FROM eln_pup_rounds WHERE pair_id = ?", (breeder_id,)
    ).fetchall()}
    incoming_pup_ids = set()
    for r in payload.pup_rounds:
        rid = r.id or uuid.uuid4().hex
        incoming_pup_ids.add(rid)
        if r.id and r.id in existing_pup_ids:
            con.execute("""
                UPDATE eln_pup_rounds SET
                  female_id=?, birth_date=?, pup_count=?, weaning_date=?,
                  male_count_weaned=?, female_count_weaned=?, notes=?, updated_at=?
                WHERE id=?
            """, (r.female_id, r.birth_date, r.pup_count, r.weaning_date,
                  r.male_count_weaned, r.female_count_weaned, r.notes, now, rid))
        else:
            con.execute("""
                INSERT INTO eln_pup_rounds
                  (id, pair_id, female_id, birth_date, pup_count, weaning_date,
                   male_count_weaned, female_count_weaned, notes, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (rid, breeder_id, r.female_id, r.birth_date, r.pup_count, r.weaning_date,
                  r.male_count_weaned, r.female_count_weaned, r.notes, now, now))
    for old_rid in (existing_pup_ids - incoming_pup_ids):
        con.execute("DELETE FROM eln_pup_rounds WHERE id = ?", (old_rid,))

    con.commit()
    return breeder_id


@router.post("/breeders")
def create_breeder(payload: BreederIn, user: dict = Depends(auth_helpers.require_user)):
    with _con() as con:
        breeder_id = _upsert_breeder(con, payload, None, user["id"])
    return get_breeder(breeder_id, user)


@router.put("/breeders/{breeder_id}")
def update_breeder(breeder_id: str, payload: BreederIn,
                   user: dict = Depends(auth_helpers.require_user)):
    with _con() as con:
        _upsert_breeder(con, payload, breeder_id, user["id"])
    return get_breeder(breeder_id, user)


@router.delete("/breeders/{breeder_id}")
def delete_breeder(breeder_id: str, user: dict = Depends(auth_helpers.require_user)):
    with _con() as con:
        existing = con.execute("SELECT id FROM eln_breeder_pairs WHERE id = ?", (breeder_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Breeder not found.")
        con.execute("DELETE FROM eln_breeder_pairs WHERE id = ?", (breeder_id,))
        auth_helpers.audit(con, user["id"], "delete_breeder", "breeder", breeder_id, None)
        con.commit()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Mouse colony stats
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/colony-stats")
def colony_stats(scope: str = "mine", user: dict = Depends(auth_helpers.require_user)):
    """Aggregate counts over the animal colony for dashboard display.

    scope:
      'mine' (default) → only animals assigned to current user
      'all'            → all animals (admin only)
    """
    from datetime import date as _date
    if scope not in ("mine", "all"):
        raise HTTPException(400, "scope must be 'mine' or 'all'.")
    if scope == "all" and user.get("effective_role") != "admin":
        raise HTTPException(403, "Admin only for scope=all.")

    owner = user["id"] if scope == "mine" else None
    with _con() as con:
        con.row_factory = sqlite3.Row

        total = animals_dal.count(con, assigned_to=owner)
        alive, sacrificed = animals_dal.aggregate_alive_sacrificed(con, assigned_to=owner)
        by_strain    = animals_dal.aggregate_by(con, "strain",    assigned_to=owner)
        by_project   = animals_dal.aggregate_by(con, "project",   assigned_to=owner)
        by_condition = animals_dal.aggregate_by(con, "condition", assigned_to=owner)
        by_sex       = animals_dal.aggregate_by(con, "sex",       assigned_to=owner)
        by_cohort    = animals_dal.aggregate_by(con, "cohort",    assigned_to=owner)
        pivot   = animals_dal.project_strain_crosstab(con, assigned_to=owner)
        buckets = animals_dal.age_buckets(con, assigned_to=owner)
        cages   = animals_dal.cage_cards(con, assigned_to=owner)
        recent  = animals_dal.list_recent(con, limit=10, assigned_to=owner)

        # Admin-only per-person summary: each person's current (alive/active)
        # animal count + strain holdings. Archived (sacrificed) mice are already
        # out of `animals`, so a plain count is the live holding.
        by_person = []
        if scope == "all":
            prows = con.execute("""
                SELECT a.assigned_to AS uid,
                       COALESCE(u.display_name, u.username, '— unassigned —') AS name,
                       u.initials AS initials, u.color AS color,
                       a.strain AS strain, COUNT(*) AS n
                FROM animals a LEFT JOIN eln_users u ON u.id = a.assigned_to
                WHERE COALESCE(a.removed,0)=0
                  AND (a.sacrificed IS NULL OR TRIM(a.sacrificed)=''
                       OR LOWER(a.sacrificed) IN ('0','no','false','n'))
                  AND a.assigned_to IS NOT NULL AND TRIM(a.assigned_to) != ''
                GROUP BY a.assigned_to, a.strain
            """).fetchall()
            _agg: dict = {}
            for r in prows:
                d = _agg.setdefault(r["uid"], {
                    "uid": r["uid"], "name": r["name"], "initials": r["initials"],
                    "color": r["color"], "count": 0, "by_strain": {}})
                d["count"] += r["n"]
                if r["strain"]:
                    d["by_strain"][r["strain"]] = d["by_strain"].get(r["strain"], 0) + r["n"]
            by_person = sorted(
                [{"uid": d["uid"], "name": d["name"], "initials": d["initials"],
                  "color": d["color"], "count": d["count"],
                  "by_strain": sorted(
                      [{"strain": s, "n": n} for s, n in d["by_strain"].items()],
                      key=lambda x: -x["n"])}
                 for d in _agg.values()],
                key=lambda x: -x["count"])

    return {
        "by_person":     by_person,
        "total":         total,
        "alive":         alive,
        "sacrificed":    sacrificed,
        "by_strain":     by_strain,
        "by_project":    by_project,
        "by_condition":  by_condition,
        "by_sex":        by_sex,
        "by_cohort":     by_cohort,
        "project_x_strain": pivot,
        "age_buckets":   [{"key": k, "count": v} for k, v in buckets.items()],
        "recent":        [dict(r) for r in recent],
        "cages_supported": True,
        "cage_count":      cages["count"],
        "mice_with_cage":  cages["total_carded"],
        "mice_no_cage":    total - cages["total_carded"],
        "cage_size_buckets": cages["size_buckets"],
        "cages":           cages["cards"],
        "scope":           scope,
    }


@router.get("/naming-conventions")
def list_naming_conventions(
    category: str,
    parent: Optional[str] = None,
    _user: dict = Depends(auth_helpers.require_user),
):
    if category not in _ALLOWED_NC_CATEGORIES:
        raise HTTPException(400, f"Unknown category '{category}'.")
    with _con() as con:
        con.row_factory = sqlite3.Row
        if parent is not None:
            rows = con.execute(
                "SELECT id, category, value, label, parent, sort_order FROM eln_naming_conventions"
                " WHERE category = ? AND parent = ? ORDER BY sort_order, value COLLATE NOCASE",
                (category, parent),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id, category, value, label, parent, sort_order FROM eln_naming_conventions"
                " WHERE category = ? ORDER BY sort_order, value COLLATE NOCASE",
                (category,),
            ).fetchall()
    return {"category": category, "items": [dict(r) for r in rows]}


@router.post("/naming-conventions")
def add_naming_convention(payload: NamingConventionIn,
                          _admin: dict = Depends(auth_helpers.require_admin)):
    if payload.category not in _ALLOWED_NC_CATEGORIES:
        raise HTTPException(400, f"Unknown category '{payload.category}'.")
    val = payload.value.strip()
    if not val:
        raise HTTPException(400, "value cannot be empty.")
    with _con() as con:
        try:
            con.execute(
                "INSERT INTO eln_naming_conventions (category, value, label, parent, sort_order, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (payload.category, val, payload.label, payload.parent, payload.sort_order, _now_iso()),
            )
            con.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"'{val}' already exists in {payload.category}.")
    return {"ok": True}


@router.delete("/naming-conventions/{item_id}")
def delete_naming_convention(item_id: int,
                             _admin: dict = Depends(auth_helpers.require_admin)):
    with _con() as con:
        con.execute("DELETE FROM eln_naming_conventions WHERE id = ?", (item_id,))
        con.commit()
    return {"ok": True}


class ConfigIn(BaseModel):
    key: str
    value: str


@router.get("/config")
def get_config(key: str, _user: dict = Depends(auth_helpers.require_user)):
    """Return a single config value by key. Any authenticated user can read."""
    val = _get_config(key)
    if val is None and key == "data_root":
        val = _DEFAULT_DATA_ROOT
    return {"key": key, "value": val}


@router.patch("/config")
def set_config(payload: ConfigIn, _admin: dict = Depends(auth_helpers.require_admin)):
    """Set a config value. Admin only."""
    with _con() as con:
        con.execute(
            "INSERT INTO eln_config (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (payload.key, payload.value, _now_iso()),
        )
        con.commit()
    return {"key": payload.key, "value": payload.value}


# ═══════════════════════════════════════════════════════════════════════════════
# Data folder  (create + browse)
# ═══════════════════════════════════════════════════════════════════════════════

def _entry_folder_name(entry: dict, note_date: str = "") -> str:
    """Compute the canonical folder name for an entry.

    Format: {exp_condition}_{exp_type}_{cohort}_{YYMMDD}_{mouse_id}_{days_in_cond}d
    The experiment date (YYMMDD) takes the slot the old "sub-" tag used to occupy.
    Kept in sync with computeFolderName() in slice.html; the client normally passes
    an explicit folder_name, so this is a fallback.
    """
    parts: List[str] = []
    if entry.get("exp_condition"): parts.append(entry["exp_condition"])
    if entry.get("exp_type"):      parts.append(entry["exp_type"])
    if entry.get("cohort"):        parts.append(entry["cohort"])
    dp = _yymmdd(note_date)
    if dp:                         parts.append(dp)
    if entry.get("mouse_id"):      parts.append(entry["mouse_id"])
    days = (entry.get("mouse") or {}).get("days_in_condition")
    if days is not None:           parts.append(f"{days}d")
    return "_".join(parts) if parts else "unnamed"


def _folder_files(folder_path: Path) -> list:
    """Return a list of {name, size, mtime} dicts for files in *folder_path*."""
    if not folder_path.exists():
        return []
    files = []
    for p in sorted(folder_path.iterdir()):
        if p.is_file():
            stat = p.stat()
            files.append({
                "name": p.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
    return files


class FolderIn(BaseModel):
    entry: dict
    data_root: Optional[str] = None  # client-supplied path overrides server config
    folder_name: Optional[str] = None  # client-supplied name overrides server-computed one
    metadata_text: Optional[str] = None  # if set, written to {metadata_filename} inside the folder
    metadata_filename: Optional[str] = "metadata.txt"


class RenameFileIn(BaseModel):
    folder_name: str
    old_name: str
    new_name: str
    data_root: Optional[str] = None


def _resolve_data_root(override: Optional[str]) -> Path:
    """Return the data root Path: client override → DB config → default."""
    raw = override or _get_config("data_root") or _DEFAULT_DATA_ROOT
    return Path(raw).expanduser()


@router.post("/data-folder")
def create_data_folder(payload: FolderIn, user: dict = Depends(auth_helpers.require_user)):
    """Create the data folder for an entry (idempotent).

    Client may pass data_root to override the server-configured path.
    Returns folder_name, full_path, data_root, created, files.
    """
    data_root  = _resolve_data_root(payload.data_root)
    folder_name = (payload.folder_name or "").strip() or _entry_folder_name(payload.entry)
    # Path-safety: reject separators
    if "/" in folder_name or "\\" in folder_name or folder_name in (".", ".."):
        raise HTTPException(400, "Invalid folder name.")
    folder_path = (data_root / folder_name).resolve()
    if not str(folder_path).startswith(str(data_root.resolve())):
        raise HTTPException(400, "Invalid folder path.")

    exists_before = folder_path.exists()
    try:
        folder_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(500, f"Could not create folder: {e}")

    metadata_written = None
    if payload.metadata_text:
        fname = (payload.metadata_filename or "metadata.txt").strip()
        if "/" in fname or "\\" in fname or fname in (".", ".."):
            raise HTTPException(400, "Invalid metadata filename.")
        meta_path = (folder_path / fname).resolve()
        if not str(meta_path).startswith(str(folder_path.resolve())):
            raise HTTPException(400, "Invalid metadata path.")
        try:
            meta_path.write_text(payload.metadata_text, encoding="utf-8")
            metadata_written = fname
        except OSError as e:
            raise HTTPException(500, f"Could not write metadata: {e}")

    return {
        "folder_name": folder_name,
        "full_path": str(folder_path),
        "data_root": str(data_root),
        "created": not exists_before,
        "metadata_written": metadata_written,
        "files": _folder_files(folder_path),
    }


@router.get("/data-folder")
def list_data_folder(folder_name: str, data_root: Optional[str] = None,
                     user: dict = Depends(auth_helpers.require_user)):
    """List files inside a data folder (read-only, no creation)."""
    root_path    = _resolve_data_root(data_root)
    folder_path  = (root_path / folder_name).resolve()
    root_resolved = root_path.resolve()

    # Safety: prevent path traversal outside data_root
    if not str(folder_path).startswith(str(root_resolved)):
        raise HTTPException(400, "Invalid folder path.")

    return {
        "folder_name": folder_name,
        "full_path": str(folder_path),
        "data_root": str(root_resolved),
        "exists": folder_path.exists(),
        "files": _folder_files(folder_path),
    }


@router.post("/rename-file")
def rename_file(payload: RenameFileIn, _user: dict = Depends(auth_helpers.require_user)):
    """Rename a file inside a data folder. If old_name is '*.abf', finds and renames the most recent .abf file."""
    from pathlib import Path
    root   = _resolve_data_root(payload.data_root)
    folder = (root / payload.folder_name).resolve()
    new_p  = (folder / payload.new_name).resolve()

    # path traversal guard
    if not str(new_p).startswith(str(folder)):
        raise HTTPException(400, "Invalid path.")

    # If old_name is "*.abf", find the most recent .abf file
    old_name = payload.old_name
    if old_name == "*.abf":
        if not folder.exists():
            raise HTTPException(404, f"Folder not found: {payload.folder_name}")
        # Find most recent .abf file
        abf_files = sorted(
            folder.glob("*.abf"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        if not abf_files:
            raise HTTPException(404, "No .abf file found in folder")
        old_p = abf_files[0]
        old_name = old_p.name
    else:
        old_p = (folder / old_name).resolve()
        if not str(old_p).startswith(str(folder)):
            raise HTTPException(400, "Invalid path.")
        if not old_p.exists():
            raise HTTPException(404, f"File not found: {payload.old_name}")

    if new_p.exists():
        raise HTTPException(409, f"File already exists: {payload.new_name}")

    old_p.rename(new_p)
    return {"ok": True, "old_name": old_name, "new_name": payload.new_name}


@router.post("/match-and-rename-files")
def match_and_rename_files(
    payload: dict,
    _user: dict = Depends(auth_helpers.require_user)
):
    """Auto-match existing .abf files to note entries by file mtime order.

    Scans folder for all .abf files (sorted by modification time), then matches
    them 1:1 with entries from the specified project's notes on the given date.
    Renames files based on computeFilename logic from the matched entries.

    Request body:
      {
        "project_slug": str,
        "note_date": "YYYY-MM-DD",
        "folder_name": str,
        "data_root": str (optional)
      }

    Returns:
      {
        "matched": int,
        "renamed": [{"old_name": str, "new_name": str}, ...],
        "unmatched_files": [str, ...],
        "unmatched_entries": int
      }
    """
    import json
    from datetime import datetime

    project_slug = payload.get("project_slug", "").strip()
    note_date = payload.get("note_date", "").strip()
    folder_name = payload.get("folder_name", "").strip()
    data_root = payload.get("data_root")

    if not project_slug or not note_date or not folder_name:
        raise HTTPException(400, "Missing required fields: project_slug, note_date, folder_name")

    # Get the note for that date
    con = _con()
    con.row_factory = sqlite3.Row

    note = con.execute(
        "SELECT id, payload FROM eln_notes WHERE project_slug = ? AND note_date = ? AND user_id = ?",
        (project_slug, note_date, _user["id"])
    ).fetchone()

    if not note:
        raise HTTPException(404, "No note found for that date/project")

    try:
        entries = json.loads(note["payload"]).get("entries", [])
    except (json.JSONDecodeError, TypeError):
        entries = []

    # Get the folder
    root = _resolve_data_root(data_root)
    folder = (root / folder_name).resolve()

    if not folder.exists():
        raise HTTPException(404, f"Folder not found: {folder_name}")

    # Get all .abf files sorted by mtime (oldest first)
    abf_files = sorted(
        folder.glob("*.abf"),
        key=lambda p: p.stat().st_mtime
    )

    if not abf_files:
        raise HTTPException(404, "No .abf files found in folder")

    # Match files to entries
    renamed = []
    unmatched_files = []

    for idx, file_path in enumerate(abf_files):
        if idx >= len(entries):
            # More files than entries
            unmatched_files.append(file_path.name)
            continue

        entry = entries[idx]
        # Compute the target filename from the entry
        # This mirrors the logic in slice.html's computeFilename()
        exp_cond = entry.get("exp_condition", "").strip()
        exp_type = entry.get("exp_type", "").strip()
        cohort = entry.get("cohort", "").strip()
        cell = entry.get("cell", "").strip()
        protocol = entry.get("protocol", "").strip()

        parts = []
        if exp_cond:
            parts.append(exp_cond)
        if exp_type:
            parts.append(exp_type)
        if cohort:
            parts.append(cohort)
        if cell:
            parts.append(cell)
        if protocol:
            parts.append(protocol)

        if not parts:
            # Can't compute a name, skip this file
            unmatched_files.append(file_path.name)
            continue

        new_name = "_".join(parts) + ".abf"
        new_path = folder / new_name

        # Skip if target already exists
        if new_path.exists():
            unmatched_files.append(f"{file_path.name} (target exists: {new_name})")
            continue

        # Rename
        try:
            file_path.rename(new_path)
            renamed.append({"old_name": file_path.name, "new_name": new_name})
        except Exception as e:
            unmatched_files.append(f"{file_path.name} (rename failed: {str(e)})")

    return {
        "matched": len(renamed),
        "renamed": renamed,
        "unmatched_files": unmatched_files,
        "unmatched_entries": max(0, len(entries) - len(abf_files))
    }


@router.get("/browse-dir")
def browse_dir(path: Optional[str] = None, _user: dict = Depends(auth_helpers.require_user)):
    """Return immediate subdirectories at *path* for the folder-picker UI.

    Defaults to the user's home directory. Hidden dirs (starting with '.') are excluded.
    """
    target = Path(path).expanduser().resolve() if path else Path.home().resolve()
    if not target.is_absolute():
        raise HTTPException(400, "Path must be absolute.")
    if not target.exists():
        raise HTTPException(404, "Path not found.")
    try:
        entries = sorted(
            [{"name": p.name, "path": str(p)} for p in target.iterdir()
             if p.is_dir() and not p.name.startswith(".")],
            key=lambda x: x["name"].lower(),
        )
    except PermissionError:
        raise HTTPException(403, "Permission denied.")
    parent = str(target.parent) if target != target.parent else None
    return {"path": str(target), "parent": parent, "entries": entries}


# ── Vision: extract per-cell patch params from a test-pulse screenshot ──
# Provider-agnostic. Configure on the server with:
#   LABOS_VISION_PROVIDER = openai | anthropic
#   LABOS_VISION_API_KEY  = <key>
#   LABOS_VISION_MODEL    = (optional override; defaults: gpt-4o-mini / claude-haiku-4-5)
# Until those env vars are set the endpoint returns 503 — the UI is ready, the
# AI call just isn't wired yet, so a key can be connected at any time.

_VISION_PROMPT = (
    "This image is a patch-clamp membrane test / test-pulse readout. Extract these "
    "values if visible: Ra (access/series resistance, MΩ), Rin (input/membrane "
    "resistance, MΩ), Cm (membrane capacitance, pF), Ihold (holding current, pA), "
    "RMP (resting membrane potential, mV). Reply with ONLY a JSON object with keys "
    "ra, rin, cm, ihold, rmp — the number only (no units), null if not visible."
)


def _vision_http(req):
    import urllib.request, urllib.error
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise HTTPException(502, f"Vision API error {e.code}: {detail}")
    except Exception as e:
        raise HTTPException(502, f"Vision API call failed: {e}")


def _vision_extract_cell_params(image_b64: str, mime: str) -> dict:
    import urllib.request
    provider = (os.environ.get("LABOS_VISION_PROVIDER") or "").strip().lower()
    api_key = (os.environ.get("LABOS_VISION_API_KEY") or "").strip()
    if not provider or not api_key:
        raise HTTPException(503, "AI extraction not configured. Set LABOS_VISION_PROVIDER "
                                 "(openai|anthropic) and LABOS_VISION_API_KEY on the server.")
    if provider == "anthropic":
        model = os.environ.get("LABOS_VISION_MODEL") or "claude-haiku-4-5"
        body = {"model": model, "max_tokens": 300, "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": image_b64}},
            {"type": "text", "text": _VISION_PROMPT}]}]}
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        raw = _vision_http(req)
        text = "".join(b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text")
    elif provider == "openai":
        model = os.environ.get("LABOS_VISION_MODEL") or "gpt-4o-mini"
        body = {"model": model, "max_tokens": 300, "messages": [{"role": "user", "content": [
            {"type": "text", "text": _VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}}]}]}
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        raw = _vision_http(req)
        text = raw["choices"][0]["message"]["content"]
    else:
        raise HTTPException(400, f"Unknown LABOS_VISION_PROVIDER: {provider}")
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise HTTPException(502, f"Could not parse extraction result: {(text or '')[:200]}")
    try:
        data = json.loads(m.group(0))
    except Exception:
        raise HTTPException(502, f"Bad JSON from model: {text[:200]}")
    return {k: data.get(k) for k in ("ra", "rin", "cm", "ihold", "rmp")}


@router.get("/vor-okr/status")
def vor_okr_status(
    project_slug: Optional[str] = None,
    user: dict = Depends(auth_helpers.require_user),
):
    """Return all VOR/OKR entries across notes with their calibration status.

    Returns a list of records: {date, project_slug, mouse_id, task, calibration_done,
    good_signal, calib_channel, experiment_notes, note_id, author}.
    Admins see all notes; members see only their own.
    """
    with _con() as con:
        con.row_factory = sqlite3.Row
        is_admin = user.get("effective_role") == "admin"
        if is_admin:
            if project_slug:
                rows = con.execute(
                    "SELECT n.id, n.note_date, n.project_slug, n.user_id, n.payload, "
                    "u.display_name, u.username "
                    "FROM eln_notes n LEFT JOIN eln_users u ON u.id=n.user_id "
                    "WHERE n.experiment_type='behavior' AND n.project_slug=? "
                    "ORDER BY n.note_date DESC",
                    (project_slug,),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT n.id, n.note_date, n.project_slug, n.user_id, n.payload, "
                    "u.display_name, u.username "
                    "FROM eln_notes n LEFT JOIN eln_users u ON u.id=n.user_id "
                    "WHERE n.experiment_type='behavior' "
                    "ORDER BY n.note_date DESC",
                ).fetchall()
        else:
            q = ("SELECT n.id, n.note_date, n.project_slug, n.user_id, n.payload, "
                 "u.display_name, u.username "
                 "FROM eln_notes n LEFT JOIN eln_users u ON u.id=n.user_id "
                 "WHERE n.experiment_type='behavior' AND n.user_id=? ")
            params = [user["id"]]
            if project_slug:
                q += "AND n.project_slug=? "
                params.append(project_slug)
            q += "ORDER BY n.note_date DESC"
            rows = con.execute(q, params).fetchall()

    vor_abbrs = {"vor", "okr"}

    def _task_abbr(t):
        import re as _re
        s = str(t or "").strip()
        m = _re.search(r'\(([^)]+)\)\s*$', s)
        raw = m.group(1) if m else _re.sub(r'\s*\([^)]*\)\s*$', '', s)
        return _re.sub(r'[^A-Za-z0-9]', '', raw).lower()

    results = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            continue
        entries = payload.get("entries") or []
        for entry in entries:
            tasks = entry.get("behavior_tasks") or []
            folders = entry.get("folders") or {}
            for task in tasks:
                if _task_abbr(task) not in vor_abbrs:
                    continue
                fd = folders.get(task) or {}
                results.append({
                    "note_id":          row["id"],
                    "date":             row["note_date"],
                    "project_slug":     row["project_slug"],
                    "mouse_id":         entry.get("mouse_id", ""),
                    "task":             task,
                    "calibration_done": bool(fd.get("calibration_done")),
                    "good_signal":      fd.get("good_signal", ""),
                    "calib_channel":    fd.get("calib_channel", ""),
                    "experiment_notes": fd.get("experiment_notes", ""),
                    "author":           row["display_name"] or row["username"] or row["user_id"],
                })

    return {"records": results}


# ── Experiment Log (aggregated view across all notes) ──────────────────────

@router.get("/log")
def get_experiment_log(
    project_slug: Optional[str] = None,
    experiment_type: Optional[str] = None,   # "slice" | "behavior"
    sub_type: Optional[str] = None,           # "vor_okr"
    author_id: Optional[str] = None,
    mouse_id: Optional[str] = None,
    cohort: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    user: dict = Depends(auth_helpers.require_user),
):
    con = _con()
    con.row_factory = sqlite3.Row
    effective_role = user.get("effective_role", "member")

    q = """
        SELECT n.id, n.project_slug, n.experiment_type, n.note_date, n.payload,
               n.user_id, u.display_name, u.username, u.color, u.initials
        FROM eln_notes n
        JOIN eln_users u ON u.id = n.user_id
        WHERE 1=1
    """
    params: list = []

    if effective_role != "admin":
        q += " AND n.project_slug IN (SELECT project_slug FROM eln_project_members WHERE user_id=?)"
        params.append(user["id"])

    # NOTE: project is filtered post-hoc by the mouse's REAL project
    # (animals.project), not the note's ELN project_slug — see the mouse_project
    # pass below. This keeps e.g. cohort-I mice under their tuning project even
    # if a note was filed under a different ELN project.
    if experiment_type:
        # notes store slice as "slice_recording"; the UI/filter uses "slice"
        q += " AND n.experiment_type=?"
        params.append("slice_recording" if experiment_type == "slice" else experiment_type)
    if author_id:
        q += " AND n.user_id=?"
        params.append(author_id)
    if date_from:
        q += " AND n.note_date>=?"
        params.append(date_from)
    if date_to:
        q += " AND n.note_date<=?"
        params.append(date_to)

    q += " ORDER BY n.note_date DESC"
    rows = con.execute(q, params).fetchall()

    import re as _re

    def _tab(t):
        s = str(t or "").strip()
        m = _re.search(r'\(([^)]+)\)\s*$', s)
        raw = m.group(1) if m else _re.sub(r'\s*\([^)]*\)\s*$', '', s)
        return _re.sub(r'[^A-Za-z0-9]', '', raw).lower()

    vor_abbrs = {"vor", "okr"}
    records = []

    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            continue

        author = {
            "id": row["user_id"],
            "name": row["display_name"] or row["username"],
            "color": row["color"] or "#8E8E93",
            "initials": row["initials"] or "?",
        }
        base = {
            "note_id": row["id"],
            "date": row["note_date"],
            "project_slug": row["project_slug"],
            "experiment_type": row["experiment_type"],
            "author": author,
        }

        etype = row["experiment_type"]
        entries = payload.get("entries") or []

        if etype in ("slice", "slice_recording"):
            for entry in entries:
                mid = entry.get("mouse_id", "")
                if mouse_id and mid != mouse_id:
                    continue
                if cohort and not _cohort_matches(cohort, entry.get("cohort", "")):
                    continue
                mouse = entry.get("mouse") or {}
                records.append({
                    **base,
                    "experiment_type": "slice",
                    "entry_type": "recording",
                    "mouse_id": mid,
                    "strain": mouse.get("strain", ""),
                    "sex": mouse.get("sex", ""),
                    "age_weeks": mouse.get("age_weeks"),
                    "condition": mouse.get("condition", ""),
                    "days_in_condition": mouse.get("days_in_condition"),
                    "cohort": entry.get("cohort", "") or mouse.get("cohort", ""),
                    "cell": entry.get("cell", ""),
                    "exp_condition": entry.get("exp_condition", ""),
                    "exp_type": entry.get("exp_type", ""),
                    "protocol": entry.get("protocol", ""),
                    "agent": entry.get("agent", ""),
                    "notes": entry.get("notes", ""),
                })

        elif etype == "behavior":
            # Recording entries (VOR/OKR tasks)
            for entry in entries:
                tasks = entry.get("behavior_tasks") or []
                folders = entry.get("folders") or {}
                mid = entry.get("mouse_id", "")
                if mouse_id and mid.upper() != mouse_id.upper():
                    continue
                if cohort and not _cohort_matches(cohort, entry.get("cohort", "")):
                    continue
                mouse_meta = entry.get("mouse") or {}
                for task in tasks:
                    abbr = _tab(task)
                    if abbr not in vor_abbrs:
                        continue
                    if sub_type and sub_type != "vor_okr":
                        continue
                    fd = folders.get(task) or {}
                    is_acclim = entry.get("acclimatization", False)
                    records.append({
                        **base,
                        "sub_type": "vor_okr",
                        "entry_type": "acclimatization" if is_acclim else "recording",
                        "mouse_id": mid,
                        "cohort": entry.get("cohort", "") or mouse_meta.get("cohort", ""),
                        "strain": mouse_meta.get("strain", ""),
                        "sex": mouse_meta.get("sex", ""),
                        "condition": mouse_meta.get("condition", ""),
                        "age_weeks": mouse_meta.get("age_weeks"),
                        "days_in_condition": mouse_meta.get("days_in_condition"),
                        "task": task,
                        "good_signal": fd.get("good_signal", ""),
                        "calib_channel": fd.get("calib_channel", ""),
                        "calibration_done": bool(fd.get("calibration_done")),
                        "experiment_notes": fd.get("experiment_notes", ""),
                        "smr_filename": fd.get("smr_filename", ""),
                        "folder_name": fd.get("folder_name", ""),
                        "disposition": entry.get("disposition", ""),
                        "disposition_at": entry.get("disposition_at", ""),
                    })

            # VOR/OKR log (acclimatization events auto-logged on Done)
            if not sub_type or sub_type == "vor_okr":
                for log_entry in (payload.get("vor_okr_log") or []):
                    mid = log_entry.get("mouse_id", "")
                    if mouse_id and mid.upper() != mouse_id.upper():
                        continue
                    records.append({
                        **base,
                        "sub_type": "vor_okr",
                        "entry_type": log_entry.get("type", "log"),
                        "mouse_id": mid,
                        "cohort": "", "strain": "", "sex": "",
                        "condition": "", "age_weeks": None, "days_in_condition": None,
                        "task": log_entry.get("type", ""),
                        "good_signal": "", "calib_channel": "",
                        "calibration_done": False,
                        "experiment_notes": "",
                        "smr_filename": "", "folder_name": "",
                        "notes": log_entry.get("notes", ""),
                    })

    # ── Legacy vor_okr_logs (flat trial/acclim records) ────────────────────
    if not experiment_type or experiment_type == "behavior":
        if not sub_type or sub_type == "vor_okr":
            from datetime import date as _ddate
            # Read active + archived animals so archived (sacrificed) mice keep
            # their cohort/strain/project in the log. animals wins on conflict.
            _acols = ("mouse_id, project, strain, condition, cohort, dob, "
                      "date_cond1, date_cond2, date_cond3")
            legacy_animals = {}
            for r in con.execute(f"SELECT {_acols} FROM animal_archive").fetchall():
                if r["mouse_id"]:
                    legacy_animals[r["mouse_id"].upper()] = dict(r)
            for r in con.execute(f"SELECT {_acols} FROM animals").fetchall():
                if r["mouse_id"]:
                    legacy_animals[r["mouse_id"].upper()] = dict(r)
            legacy_rows = con.execute("SELECT * FROM vor_okr_logs").fetchall()
            for vrow in legacy_rows:
                mid = vrow["mouse_id"]
                if mouse_id and mid.upper() != mouse_id.upper():
                    continue
                ani = legacy_animals.get(mid.upper(), {})
                if cohort and not _cohort_matches(cohort, ani.get("cohort", "")):
                    continue
                try:
                    trials = json.loads(vrow["trials"] or "[]")
                except Exception:
                    trials = []
                try:
                    acclim_dates = json.loads(vrow["acclimatization"] or "[]")
                except Exception:
                    acclim_dates = []

                def _legacy_rec(date_str: str, task_type: str, notes_txt: str, is_acclim: bool):
                    if not date_str:
                        return None
                    if date_from and date_str < date_from:
                        return None
                    if date_to and date_str > date_to:
                        return None
                    age_wk = None
                    dic = None
                    try:
                        td = _ddate.fromisoformat(date_str)
                        if ani.get("dob"):
                            age_wk = (td - _ddate.fromisoformat(ani["dob"])).days // 7
                        for dcol in ["date_cond3", "date_cond2", "date_cond1"]:
                            if ani.get(dcol) and ani[dcol] <= date_str:
                                dic = (td - _ddate.fromisoformat(ani[dcol])).days
                                break
                    except Exception:
                        pass
                    return {
                        "note_id": f"legacy-{mid}-{date_str}-{task_type}",
                        "date": date_str,
                        "project_slug": ani.get("project", ""),
                        "experiment_type": "behavior",
                        "sub_type": "vor_okr",
                        "source": "legacy",
                        "author": None,
                        "entry_type": "acclimatization" if is_acclim else "recording",
                        "mouse_id": mid,
                        "cohort": ani.get("cohort", ""),
                        "strain": ani.get("strain", ""),
                        "condition": ani.get("condition", ""),
                        "age_weeks": age_wk,
                        "days_in_condition": dic,
                        "task": task_type,
                        "good_signal": "",
                        "calib_channel": "",
                        "calibration_done": False,
                        "experiment_notes": notes_txt,
                        "smr_filename": "",
                        "folder_name": "",
                        "notes": notes_txt,
                        "dropout": bool(vrow["dropout"]) if "dropout" in vrow.keys() else False,
                        "disposition": "",
                        "disposition_at": "",
                    }

                for i, ad in enumerate(acclim_dates):
                    rec = _legacy_rec(ad, "Acclimatization", "", True)
                    if rec:
                        records.append(rec)
                for trial in trials:
                    td_str = trial.get("date") or trial.get("test_date") or ""
                    rec = _legacy_rec(td_str, trial.get("type", "VOR/OKR"), trial.get("memo", ""), False)
                    if rec:
                        records.append(rec)

    # Surgery-done behavior mice with no sessions yet: still list them (blank
    # OKR/VOR) so a mouse that's been operated on but not yet run shows up.
    # "Behavior" = experiment mentions VOR/OKR. Skipped when filtering by author
    # (these aren't authored records).
    if ((not experiment_type) or experiment_type == "behavior") and \
       ((not sub_type) or sub_type == "vor_okr") and not author_id:
        from datetime import date as _pdate
        _today = _pdate.today()
        _behav_have = {(r.get("mouse_id") or "").upper()
                       for r in records if r.get("experiment_type") == "behavior"}
        _dropped = {(r["mouse_id"] or "").upper()
                    for r in con.execute("SELECT mouse_id FROM vor_okr_logs WHERE dropout=1").fetchall()
                    if r["mouse_id"]}
        for a in con.execute(
            "SELECT mouse_id, project, strain, sex, condition, cohort, dob, dos, "
            "experiment, date_cond1, date_cond2, date_cond3 FROM animals "
            "WHERE COALESCE(removed,0)=0 AND dos IS NOT NULL AND TRIM(dos)!=''"
        ).fetchall():
            mid = (a["mouse_id"] or "").strip()
            if not mid or mid.upper() in _behav_have:
                continue
            if mouse_id and mid.upper() != mouse_id.upper():
                continue
            if cohort and not _cohort_matches(cohort, a["cohort"] or ""):
                continue
            _exp = (a["experiment"] or "").lower()
            if "vor" not in _exp and "okr" not in _exp:
                continue
            try:
                if _pdate.fromisoformat(a["dos"]) > _today:
                    continue   # surgery not yet performed
            except Exception:
                continue
            _dic, _ = _compute_days_in_condition(a)
            records.append({
                "note_id": f"pending-{mid}",
                "date": a["dos"] or "",
                "project_slug": "",
                "experiment_type": "behavior",
                "sub_type": "vor_okr",
                "source": "pending",
                "pending": True,
                "author": None,
                "entry_type": "pending",
                "mouse_id": mid,
                "cohort": a["cohort"] or "",
                "strain": a["strain"] or "",
                "sex": a["sex"] or "",
                "condition": a["condition"] or "",
                "age_weeks": _compute_age_weeks(a["dob"]),
                "days_in_condition": _dic,
                "task": "",
                "good_signal": "", "calib_channel": "", "calibration_done": False,
                "experiment_notes": "", "smr_filename": "", "folder_name": "",
                "notes": "",
                "dropout": (mid.upper() in _dropped), "disposition": "", "disposition_at": "",
            })

    # Tag every record with the mouse's REAL project (animals.project) and
    # filter by it. This is what the user means by "project" — not where the
    # note happens to be filed.
    proj_map = {}
    for r in con.execute("SELECT mouse_id, project FROM animal_archive").fetchall():
        if r["mouse_id"]:
            proj_map[r["mouse_id"].upper()] = (r["project"] or "").strip()
    for r in con.execute("SELECT mouse_id, project FROM animals").fetchall():
        if r["mouse_id"]:
            proj_map[r["mouse_id"].upper()] = (r["project"] or "").strip()
    for r in records:
        r["mouse_project"] = proj_map.get((r.get("mouse_id") or "").upper(), "")
    if project_slug:
        _pl = project_slug.strip().lower()
        records = [r for r in records if (r.get("mouse_project") or "").lower() == _pl]

    # Derive "sacrificed": a mouse is sacrificed once it appears in a FINALIZED
    # (approved) slice-recording note. App-owned/derived — never written to the
    # animals table or Google Sheet (so a Sheet sync can't clobber it). Date =
    # the slice note's date (slice recording is terminal).
    sacrificed: dict = {}
    for sr in con.execute(
        "SELECT note_date, payload FROM eln_notes "
        "WHERE experiment_type='slice_recording' AND status='approved'"
    ).fetchall():
        try:
            sp = json.loads(sr["payload"] or "{}")
        except Exception:
            continue
        for e in sp.get("entries", []):
            smid = (e.get("mouse_id") or "").strip().upper()
            if not smid:
                continue
            d = sr["note_date"] or ""
            if smid not in sacrificed or (d and d < sacrificed[smid]):
                sacrificed[smid] = d
    for r in records:
        smid = (r.get("mouse_id") or "").strip().upper()
        r["sacrificed"] = smid in sacrificed
        r["sacrificed_at"] = sacrificed.get(smid, "")

    # Project dropdown = distinct real projects (animals.project), case-folded
    proj_seen: dict = {}
    for r in con.execute(
        "SELECT project FROM animals WHERE project IS NOT NULL AND TRIM(project)!=''"
    ).fetchall():
        k = r["project"].strip()
        proj_seen.setdefault(k.lower(), k)
    projects = [{"slug": v, "title": v} for v in sorted(proj_seen.values(), key=str.lower)]

    # Authors — all experimenters (project is now mouse-based, not membership)
    auth_rows = con.execute("""
        SELECT DISTINCT u.id, u.display_name, u.username, u.color, u.initials
        FROM eln_users u JOIN eln_project_members m ON m.user_id=u.id
        ORDER BY u.display_name
    """).fetchall()
    authors = [
        {"id": r["id"], "name": r["display_name"] or r["username"],
         "color": r["color"] or "#8E8E93", "initials": r["initials"] or "?"}
        for r in auth_rows
    ]

    return {"records": records, "projects": projects, "authors": authors}


# ── Ephys Analysis Pipeline ────────────────────────────────────────────────

_UPLOAD_DIR  = Path("/opt/labos/analysis_uploads")
_RESULTS_DIR = Path("/opt/labos/analysis_results")


class AnalysisJobIn(BaseModel):
    note_id:        Optional[str] = None
    project_slug:   str
    analysis_type:  str = "ltd"          # "ltd" | "excitability"
    exp_condition:  str = ""             # e.g. "MTDT"
    note_date:      str = ""             # "YYYY-MM-DD"
    prefix:         str = "LTD"
    protocol_mode:  str = ""             # "" = auto-detect
    epsc_min:       float = 20.0
    epsc_max:       float = 1000.0
    email:          str = ""
    extra_out_dir:  str = ""             # optional secondary output path


@router.post("/analysis/upload/{job_id}")
async def upload_abf(
    job_id: str,
    request: Request,
    user: dict = Depends(auth_helpers.require_user),
):
    """Receive a single ABF file chunk. Call repeatedly for each file."""
    content_type = request.headers.get("content-type", "")
    filename = request.headers.get("x-filename", "file.abf")
    if not filename.lower().endswith((".abf",)):
        raise HTTPException(400, "Only .abf files accepted.")

    dest_dir = _UPLOAD_DIR / job_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(filename).name
    body = await request.body()
    dest.write_bytes(body)
    return {"ok": True, "filename": dest.name, "size": len(body)}




@router.post("/analysis/start")
def start_analysis_job(
    payload: AnalysisJobIn,
    upload_job_id: str,           # the job_id used when uploading files
    user: dict = Depends(auth_helpers.require_user),
):
    """Validate uploaded files exist, then enqueue the analysis job."""
    con = _con()
    upload_dir = _UPLOAD_DIR / upload_job_id
    if not upload_dir.exists() or not list(upload_dir.glob("*.abf")):
        raise HTTPException(400, "No ABF files found for this upload_job_id.")

    n_files = len(list(upload_dir.glob("*.abf")))
    params = payload.dict()

    con.execute(
        """INSERT INTO eln_analysis_jobs
           (id, note_id, project_slug, user_id, analysis_type, status, params, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [upload_job_id, payload.note_id, payload.project_slug, user["id"],
         payload.analysis_type, "pending", json.dumps(params),
         _now_iso(), _now_iso()],
    )
    con.commit()
    return {"job_id": upload_job_id, "n_files": n_files, "status": "pending"}


@router.get("/analysis/jobs/{job_id}")
def get_analysis_job(job_id: str, user: dict = Depends(auth_helpers.require_user)):
    con = _con()
    con.row_factory = __import__("sqlite3").Row
    row = con.execute("SELECT * FROM eln_analysis_jobs WHERE id=?", [job_id]).fetchone()
    if not row:
        raise HTTPException(404, "Job not found.")
    result = json.loads(row["result"] or "null")
    return {
        "job_id":  job_id,
        "status":  row["status"],
        "params":  json.loads(row["params"] or "{}"),
        "result":  result,
        "error":   row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/analysis/jobs")
def list_analysis_jobs(
    project_slug: Optional[str] = None,
    user: dict = Depends(auth_helpers.require_user),
):
    con = _con()
    con.row_factory = __import__("sqlite3").Row
    q = "SELECT * FROM eln_analysis_jobs WHERE user_id=? OR ? = 'admin'"
    rows = con.execute(q, [user["id"], user.get("effective_role", "member")]).fetchall()
    if project_slug:
        rows = [r for r in rows if r["project_slug"] == project_slug]
    return {"jobs": [
        {"job_id": r["id"], "note_id": r["note_id"], "status": r["status"],
         "analysis_type": r["analysis_type"], "project_slug": r["project_slug"],
         "created_at": r["created_at"],
         "params": json.loads(r["params"] or "{}"),
         "result": json.loads(r["result"] or "null")}
        for r in rows
    ]}


@router.get("/analysis/results/{job_id}/{filename}")
def download_result(
    job_id: str, filename: str,
    user: dict = Depends(auth_helpers.require_user),
):
    from fastapi.responses import FileResponse as FR
    safe = Path(filename).name
    path = _RESULTS_DIR / job_id / safe
    if not path.exists():
        raise HTTPException(404, "File not found.")
    return FR(str(path), filename=safe)


class _VisionIn(BaseModel):
    image_b64: str
    mime: Optional[str] = "image/png"


@router.post("/vision/extract-params")
def vision_extract_params(payload: _VisionIn, user: dict = Depends(auth_helpers.require_user)):
    if not payload.image_b64:
        raise HTTPException(400, "image_b64 required")
    params = _vision_extract_cell_params(payload.image_b64, payload.mime or "image/png")
    return {"ok": True, "params": params}
