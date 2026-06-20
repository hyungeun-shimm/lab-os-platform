#!/usr/bin/env python3
"""Break-glass: reset an ELN user's password directly in scheduler.db.

For total lockout — a user forgot BOTH their password and their registered email,
or SMTP is down. Run this ON THE VM as the `labos` user so the SQLite WAL/SHM files
stay labos-owned:

    sudo -u labos /opt/labos/.venv/bin/python /opt/labos/tools/reset_eln_password.py \
        --username labadmin --password 'NewPass123!'

Or let it mint a random temporary password (printed at the end):

    sudo -u labos /opt/labos/.venv/bin/python /opt/labos/tools/reset_eln_password.py \
        --username labadmin --random

It uses the exact same PBKDF2-HMAC-SHA256 (200k iter, per-user salt) hashing as the
app, so the new password works immediately, and it invalidates the user's existing
sessions. Stdlib only — no venv strictly required (plain `python3` works too).
"""

import argparse
import hashlib
import secrets
import sqlite3
import sys

ITER = 200_000
DEFAULT_DB = "/opt/labos/scheduler.db"


def hash_pw(pw, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), ITER)
    return dk.hex(), salt


def main():
    ap = argparse.ArgumentParser(description="Reset an ELN user's password (break-glass).")
    ap.add_argument("--username", required=True, help="eln_users.username (case-insensitive)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--password", help="explicit new password (>= 4 chars)")
    grp.add_argument("--random", action="store_true", help="generate a random temporary password")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"path to scheduler.db (default {DEFAULT_DB})")
    args = ap.parse_args()

    new_pw = args.password if args.password else secrets.token_urlsafe(9)
    if len(new_pw) < 4:
        ap.error("password must be at least 4 characters")

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id, username, email, role, active FROM eln_users WHERE LOWER(username) = ?",
        (args.username.lower(),),
    ).fetchone()
    if not row:
        print(f"No ELN user with username '{args.username}'.", file=sys.stderr)
        con.close()
        sys.exit(1)

    h, s = hash_pw(new_pw)
    con.execute("UPDATE eln_users SET pin_hash = ?, pin_salt = ? WHERE id = ?", (h, s, row["id"]))
    ndel = con.execute("DELETE FROM eln_sessions WHERE user_id = ?", (row["id"],)).rowcount
    con.commit()
    con.close()

    print(f"Reset password for '{row['username']}' (role={row['role']}, email={row['email']}, active={row['active']}).")
    print(f"Sessions invalidated: {ndel}")
    print(f"New password: {new_pw}")


if __name__ == "__main__":
    main()
