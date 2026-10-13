"""ELN-specific DB tables. Created/migrated alongside the scheduler's own tables.

Tables added here all begin with `eln_` to avoid collisions with the existing
scheduler tables (events, team_members, animals, etc.) that live in scheduler.db.

This file is imported and run from server.py's migrate_db() / init_db() path.
"""

from __future__ import annotations

import sqlite3


def init_eln_tables(con: sqlite3.Connection) -> None:
    """Create ELN tables if they don't exist. Safe to call repeatedly."""
    cur = con.cursor()

    # ---- Users / auth ----
    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_users (
            id            TEXT PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            display_name  TEXT NOT NULL,
            email         TEXT,
            role          TEXT NOT NULL DEFAULT 'member',
            pin_hash      TEXT NOT NULL,
            pin_salt      TEXT NOT NULL,
            color         TEXT,
            initials      TEXT,
            active        INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL,
            last_login_at TEXT
        )"""
    )
    cur.execute("CREATE INDEX IF NOT EXISTS eln_users_username_idx ON eln_users(LOWER(username))")
    cur.execute("CREATE INDEX IF NOT EXISTS eln_users_email_idx ON eln_users(LOWER(email))")

    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_sessions (
            token       TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            user_agent  TEXT,
            mode        TEXT,  -- 'admin' or 'member' (NULL = not chosen yet)
            FOREIGN KEY (user_id) REFERENCES eln_users(id) ON DELETE CASCADE
        )"""
    )
    cur.execute("CREATE INDEX IF NOT EXISTS eln_sessions_user_idx ON eln_sessions(user_id)")
    # idempotent migration for the `mode` column on older DBs
    cur.execute("PRAGMA table_info(eln_sessions)")
    cols = {r[1] for r in cur.fetchall()}
    if "mode" not in cols:
        cur.execute("ALTER TABLE eln_sessions ADD COLUMN mode TEXT")

    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_password_resets (
            token       TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            code_hash   TEXT NOT NULL,
            code_salt   TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            used        INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES eln_users(id) ON DELETE CASCADE
        )"""
    )

    # ---- Projects + assignments ----
    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_projects (
            slug        TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            description TEXT,
            kind        TEXT NOT NULL DEFAULT 'assigned',
            owner_id    TEXT,
            created_at  TEXT NOT NULL,
            archived    INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (owner_id) REFERENCES eln_users(id) ON DELETE SET NULL
        )"""
    )

    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_project_members (
            project_slug TEXT NOT NULL,
            user_id      TEXT NOT NULL,
            role         TEXT NOT NULL DEFAULT 'write',
            assigned_at  TEXT NOT NULL,
            PRIMARY KEY (project_slug, user_id),
            FOREIGN KEY (project_slug) REFERENCES eln_projects(slug) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES eln_users(id) ON DELETE CASCADE
        )"""
    )

    # ---- Audit log (admin monitoring) ----
    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_audit (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT NOT NULL,
            user_id       TEXT,
            action        TEXT NOT NULL,
            resource_type TEXT,
            resource_id   TEXT,
            details       TEXT
        )"""
    )
    cur.execute("CREATE INDEX IF NOT EXISTS eln_audit_ts_idx ON eln_audit(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS eln_audit_user_idx ON eln_audit(user_id, ts)")

    # ---- Lab notes (per-date, per-experiment-type) ----
    # Created here so Step D can extend without another migration round.
    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_notes (
            id              TEXT PRIMARY KEY,
            project_slug    TEXT NOT NULL,
            user_id         TEXT NOT NULL,
            experiment_type TEXT NOT NULL,
            note_date       TEXT NOT NULL,
            payload         TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'draft',
            submitted_at    TEXT,
            approved_at     TEXT,
            approved_by     TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            FOREIGN KEY (project_slug) REFERENCES eln_projects(slug) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES eln_users(id) ON DELETE CASCADE
        )"""
    )
    cur.execute("CREATE INDEX IF NOT EXISTS eln_notes_lookup_idx ON eln_notes(project_slug, experiment_type, note_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS eln_notes_user_idx ON eln_notes(user_id, updated_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS eln_notes_status_idx ON eln_notes(status, updated_at)")

    cur.execute("""CREATE TABLE IF NOT EXISTS eln_event_notes (
        event_id TEXT NOT NULL,
        note_id TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (event_id, note_id),
        FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
        FOREIGN KEY (note_id) REFERENCES eln_notes(id) ON DELETE CASCADE
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS eln_event_notes_note_idx ON eln_event_notes(note_id)")

    # ---- Naming conventions (admin-configured prefix lists) ----
    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_naming_conventions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL,
            value       TEXT NOT NULL,
            label       TEXT,
            parent      TEXT,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            UNIQUE (category, value, parent)
        )"""
    )
    # Migration: add parent column if it doesn't exist yet
    try:
        con.execute("ALTER TABLE eln_naming_conventions ADD COLUMN parent TEXT")
        con.commit()
    except Exception:
        pass  # column already exists
    con.execute(
        """CREATE TABLE IF NOT EXISTS eln_config (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )

    # ---- Breeder pairs (colony breeding management) ----
    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_breeder_pairs (
            id                   TEXT PRIMARY KEY,
            label                TEXT NOT NULL,
            strain               TEXT,
            status               TEXT NOT NULL DEFAULT 'active',
            jax_available        INTEGER NOT NULL DEFAULT 0,
            jax_url              TEXT,
            setup_date           TEXT,
            expected_retire_date TEXT,
            notes                TEXT,
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL
        )"""
    )

    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_breeder_animals (
            id               TEXT PRIMARY KEY,
            pair_id          TEXT NOT NULL,
            role             TEXT NOT NULL,
            mouse_id         TEXT,
            dob              TEXT,
            strain           TEXT,
            genotype         TEXT,
            sex              TEXT,
            current_location TEXT,
            pregnant         INTEGER NOT NULL DEFAULT 0,
            notes            TEXT,
            sort_order       INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            FOREIGN KEY (pair_id) REFERENCES eln_breeder_pairs(id) ON DELETE CASCADE
        )"""
    )
    cur.execute("CREATE INDEX IF NOT EXISTS eln_breeder_animals_pair_idx ON eln_breeder_animals(pair_id)")

    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_pup_rounds (
            id                   TEXT PRIMARY KEY,
            pair_id              TEXT NOT NULL,
            female_id            TEXT,
            birth_date           TEXT,
            pup_count            INTEGER NOT NULL DEFAULT 0,
            weaning_date         TEXT,
            male_count_weaned    INTEGER NOT NULL DEFAULT 0,
            female_count_weaned  INTEGER NOT NULL DEFAULT 0,
            notes                TEXT,
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL,
            FOREIGN KEY (pair_id) REFERENCES eln_breeder_pairs(id) ON DELETE CASCADE,
            FOREIGN KEY (female_id) REFERENCES eln_breeder_animals(id) ON DELETE SET NULL
        )"""
    )
    cur.execute("CREATE INDEX IF NOT EXISTS eln_pup_rounds_pair_idx ON eln_pup_rounds(pair_id)")

    # ---- Per-user mouse assignment (animals.assigned_to) ----
    try:
        cur.execute("ALTER TABLE animals ADD COLUMN assigned_to TEXT")
    except Exception:
        pass  # column exists

    # One-shot backfill: if exactly one ELN user exists, give them all unassigned mice
    try:
        cur.execute("""
            UPDATE animals
               SET assigned_to = (SELECT id FROM eln_users LIMIT 1)
             WHERE (assigned_to IS NULL OR assigned_to = '')
               AND (SELECT COUNT(*) FROM eln_users) = 1
        """)
    except Exception:
        pass

    # ---- Project unification — short_name on PM + ELN, bridge from ELN ----
    for tbl, col, ddl in (
        ("custom_projects", "short_name", "ALTER TABLE custom_projects ADD COLUMN short_name TEXT"),
        ("eln_projects",    "short_name", "ALTER TABLE eln_projects   ADD COLUMN short_name TEXT"),
        ("eln_projects",    "pm_slug",    "ALTER TABLE eln_projects   ADD COLUMN pm_slug TEXT"),
    ):
        try:
            cur.execute(ddl)
        except Exception:
            pass  # column exists

    # ---- Member profile fields (admin member-list page) ----
    for ddl in (
        "ALTER TABLE eln_users ADD COLUMN organization TEXT",
        "ALTER TABLE eln_users ADD COLUMN position TEXT",
        "ALTER TABLE eln_users ADD COLUMN status TEXT NOT NULL DEFAULT 'current'",
        # Signup gate: self-registrations land as approved=0 (pending) and cannot
        # log in until an admin approves. Existing rows default to 1 (grandfathered).
        "ALTER TABLE eln_users ADD COLUMN approved INTEGER NOT NULL DEFAULT 1",
        # Daily schedule-digest email opt-out (1 = receive; user/admin can turn off).
        "ALTER TABLE eln_users ADD COLUMN email_digest INTEGER NOT NULL DEFAULT 1",
        # Per-project intellectual role + permission tier
        "ALTER TABLE eln_project_members ADD COLUMN project_role TEXT",
        "ALTER TABLE eln_project_members ADD COLUMN authority TEXT NOT NULL DEFAULT 'edit'",
    ):
        try:
            cur.execute(ddl)
        except Exception:
            pass

    # ---- Per-user colony edit permission ----
    try:
        cur.execute("ALTER TABLE eln_users ADD COLUMN can_edit_colony INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    # Admins always get colony-edit by default
    try:
        cur.execute("UPDATE eln_users SET can_edit_colony = 1 WHERE role = 'admin'")
    except Exception:
        pass

    # ---- Bridge team_members ↔ eln_users (unified identity) ----
    try:
        cur.execute("ALTER TABLE team_members ADD COLUMN eln_user_id TEXT")
    except Exception:
        pass

    # Auto-link existing rows by normalized name match (strip spaces, lowercase).
    # Idempotent — only fills NULLs.
    try:
        cur.execute("""
            UPDATE team_members
               SET eln_user_id = (
                 SELECT u.id FROM eln_users u
                  WHERE LOWER(REPLACE(REPLACE(u.display_name, ' ', ''), '-', ''))
                      = LOWER(REPLACE(REPLACE(team_members.name, ' ', ''), '-', ''))
                  LIMIT 1
               )
             WHERE eln_user_id IS NULL OR eln_user_id = ''
        """)
    except Exception:
        pass

    # ---- Mouse transfer log ----
    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_mouse_transfers (
            id              TEXT PRIMARY KEY,
            mouse_id        TEXT NOT NULL,
            from_user_id    TEXT,
            to_user_id      TEXT NOT NULL,
            transferred_by  TEXT NOT NULL,
            note            TEXT,
            transferred_at  TEXT NOT NULL
        )"""
    )
    cur.execute("CREATE INDEX IF NOT EXISTS eln_mouse_transfers_mid_idx ON eln_mouse_transfers(mouse_id)")

    # ---- Analysis jobs (ephys auto-pipeline) ----
    cur.execute(
        """CREATE TABLE IF NOT EXISTS eln_analysis_jobs (
            id              TEXT PRIMARY KEY,
            note_id         TEXT,
            project_slug    TEXT NOT NULL,
            user_id         TEXT NOT NULL,
            analysis_type   TEXT NOT NULL DEFAULT 'ltd',
            status          TEXT NOT NULL DEFAULT 'pending',
            params          TEXT NOT NULL DEFAULT '{}',
            result          TEXT,
            error           TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )"""
    )
    cur.execute("CREATE INDEX IF NOT EXISTS eln_analysis_jobs_status_idx ON eln_analysis_jobs(status, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS eln_analysis_jobs_note_idx  ON eln_analysis_jobs(note_id)")

    con.commit()
