"""Animals DAL — single source of truth for the `animals` table.

Both the scheduler (server.py) and the ELN (modules/eln/api.py) import from
this module. SQL lives here; callers pass in their own sqlite3 connection
(`con`) so transaction control stays with them.

Schema (scheduler.db `animals`):
    id              INTEGER PRIMARY KEY AUTOINCREMENT
    mouse_id        TEXT NOT NULL
    project         TEXT
    condition       TEXT
    experiment      TEXT
    cohort          TEXT
    strain          TEXT
    dob             TEXT
    sex             TEXT
    dos             TEXT
    date_cond1..3   TEXT
    days_cond1..3   TEXT
    sacrificed      TEXT
    sacrificed_date TEXT
    notes           TEXT
    synced_at       TEXT NOT NULL
    barcode         TEXT
    assigned_to     TEXT     -- eln_users.id (added by ELN)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, date as _date
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------------------
# Field set used for full INSERT / replace operations (Excel sync etc.)
# ---------------------------------------------------------------------------
INSERT_FIELDS = (
    "mouse_id", "project", "condition", "experiment", "cohort", "strain",
    "dob", "sex", "dos",
    "date_cond1", "date_cond2", "date_cond3",
    "days_cond1", "days_cond2", "days_cond3",
    "sacrificed", "sacrificed_date",
    "notes", "synced_at", "barcode",
)

# Fields a regular caller may update via update(). assigned_to is intentionally
# excluded — use reassign() so transfers are logged consistently.
UPDATABLE_FIELDS = (
    "project", "condition", "experiment", "cohort", "strain",
    "dob", "sex", "dos",
    "date_cond1", "date_cond2", "date_cond3",
    "days_cond1", "days_cond2", "days_cond3",
    "sacrificed", "sacrificed_date",
    "notes", "barcode",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Soft-removed animals (vanished from the source Sheet) are kept for audit/rollback
# but excluded from normal reads. See modules/core/animal_sync.py.
_ALIVE = "COALESCE(removed,0)=0"


# ===========================================================================
# READ
# ===========================================================================

def get(con: sqlite3.Connection, mouse_id: str, *,
        case_insensitive: bool = True) -> Optional[sqlite3.Row]:
    """Fetch one animal by mouse_id. Returns None if not found."""
    mid = (mouse_id or "").strip()
    if not mid:
        return None
    if case_insensitive:
        return con.execute(
            "SELECT * FROM animals WHERE LOWER(mouse_id) = LOWER(?)", (mid,)
        ).fetchone()
    return con.execute(
        "SELECT * FROM animals WHERE mouse_id = ?", (mid,)
    ).fetchone()


def exists(con: sqlite3.Connection, mouse_id: str, *,
           case_insensitive: bool = True) -> bool:
    return get(con, mouse_id, case_insensitive=case_insensitive) is not None


def list_all(con: sqlite3.Connection, *,
             assigned_to: Optional[str] = None,
             only_with_mouse_id: bool = True,
             include_removed: bool = False) -> list[sqlite3.Row]:
    """List all animals, optionally filtered by owner. Soft-removed animals are
    excluded unless include_removed=True."""
    clauses, params = [], []
    if not include_removed:
        clauses.append(_ALIVE)
    if only_with_mouse_id:
        clauses.append("mouse_id IS NOT NULL AND TRIM(mouse_id) != ''")
    if assigned_to is not None:
        clauses.append("assigned_to = ?")
        params.append(assigned_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return list(con.execute(
        f"SELECT * FROM animals {where} ORDER BY mouse_id", params
    ).fetchall())


def count(con: sqlite3.Connection, *,
          assigned_to: Optional[str] = None,
          include_removed: bool = False) -> int:
    clauses, params = [], []
    if not include_removed:
        clauses.append(_ALIVE)
    if assigned_to is not None:
        clauses.append("assigned_to = ?")
        params.append(assigned_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return con.execute(f"SELECT COUNT(*) AS n FROM animals {where}", params).fetchone()["n"]


def search(con: sqlite3.Connection, q: str, *, limit: int = 12) -> list[sqlite3.Row]:
    """Prefix-preferred substring search on mouse_id (for autocomplete)."""
    q = (q or "").strip()
    if not q:
        return []
    limit = max(1, min(50, limit))
    return list(con.execute(
        f"""SELECT mouse_id, strain, sex, condition, cohort, dob
             FROM animals
            WHERE {_ALIVE} AND LOWER(mouse_id) LIKE LOWER(?)
            ORDER BY
              CASE WHEN LOWER(mouse_id) LIKE LOWER(?) THEN 0 ELSE 1 END,
              mouse_id
            LIMIT ?""",
        (f"%{q}%", f"{q}%", limit),
    ).fetchall())


def list_recent(con: sqlite3.Connection, *,
                limit: int = 10,
                assigned_to: Optional[str] = None,
                fields: str = "mouse_id, strain, sex, project, condition, dob, synced_at, barcode") -> list[sqlite3.Row]:
    clauses, params = [_ALIVE], []
    if assigned_to is not None:
        clauses.append("assigned_to = ?")
        params.append(assigned_to)
    where = "WHERE " + " AND ".join(clauses)
    return list(con.execute(
        f"SELECT {fields} FROM animals {where} ORDER BY synced_at DESC LIMIT ?",
        params + [limit],
    ).fetchall())


# ---- Aggregates for the colony dashboard ---------------------------------

def _base_where(assigned_to: Optional[str]) -> tuple[str, list]:
    if assigned_to is None:
        return f"WHERE {_ALIVE}", []
    return f"WHERE {_ALIVE} AND assigned_to = ?", [assigned_to]


def aggregate_alive_sacrificed(con: sqlite3.Connection, *,
                               assigned_to: Optional[str] = None) -> tuple[int, int]:
    """Return (alive, sacrificed) counts. Treats non-empty/non-falsy text as sacrificed."""
    where, params = _base_where(assigned_to)
    row = con.execute(f"""
        SELECT
          SUM(CASE WHEN sacrificed IS NOT NULL AND sacrificed != ''
                   AND LOWER(sacrificed) NOT IN ('0','no','false')
                   THEN 1 ELSE 0 END) AS sacrificed,
          SUM(CASE WHEN sacrificed IS NULL OR sacrificed = ''
                   OR LOWER(sacrificed) IN ('0','no','false')
                   THEN 1 ELSE 0 END) AS alive
        FROM animals
        {where}
    """, params).fetchone()
    return (row["alive"] or 0, row["sacrificed"] or 0)


def aggregate_by(con: sqlite3.Connection, column: str, *,
                 assigned_to: Optional[str] = None) -> list[dict]:
    """GROUP BY {column}. Returns [{key, count}] sorted DESC by count.
    Only columns in a fixed allowlist are accepted, to prevent SQL injection.
    """
    allowed = {"strain", "project", "condition", "sex", "cohort", "experiment"}
    if column not in allowed:
        raise ValueError(f"aggregate_by: column '{column}' not allowed.")
    where, params = _base_where(assigned_to)
    rows = con.execute(
        f"SELECT {column} AS k, COUNT(*) AS n FROM animals {where} GROUP BY {column} ORDER BY n DESC",
        params,
    ).fetchall()
    return [{"key": (r["k"] or "(none)"), "count": r["n"]} for r in rows]


def project_strain_crosstab(con: sqlite3.Connection, *,
                            assigned_to: Optional[str] = None) -> dict[str, dict[str, int]]:
    """Pivot: { project: { strain: count } }, with None coerced to '(none)'."""
    where, params = _base_where(assigned_to)
    rows = con.execute(
        f"""SELECT project, strain, COUNT(*) AS n
              FROM animals {where}
          GROUP BY project, strain
          ORDER BY n DESC""",
        params,
    ).fetchall()
    pivot: dict = {}
    for r in rows:
        p = r["project"] or "(none)"
        s = r["strain"]  or "(none)"
        pivot.setdefault(p, {})[s] = r["n"]
    return pivot


def age_buckets(con: sqlite3.Connection, *,
                assigned_to: Optional[str] = None,
                alive_only: bool = True) -> dict[str, int]:
    """Bucket alive (or all) animals by weeks-since-DOB."""
    where = f"WHERE {_ALIVE} AND dob IS NOT NULL AND dob != ''"
    params: list = []
    if assigned_to is not None:
        where += " AND assigned_to = ?"
        params.append(assigned_to)
    rows = con.execute(f"SELECT dob, sacrificed FROM animals {where}", params).fetchall()

    buckets = {"<4w": 0, "4-8w": 0, "8-16w": 0, "16-26w": 0,
               "26-52w": 0, ">52w": 0, "unknown": 0}
    today = _date.today()
    for r in rows:
        if alive_only:
            sac = r["sacrificed"]
            if sac is not None and str(sac).strip() and str(sac).lower() not in ("0", "no", "false"):
                continue
        try:
            d = _date.fromisoformat(r["dob"])
            wks = (today - d).days / 7
        except Exception:
            buckets["unknown"] += 1
            continue
        if   wks < 4:  buckets["<4w"]    += 1
        elif wks < 8:  buckets["4-8w"]   += 1
        elif wks < 16: buckets["8-16w"]  += 1
        elif wks < 26: buckets["16-26w"] += 1
        elif wks < 52: buckets["26-52w"] += 1
        else:          buckets[">52w"]   += 1
    return buckets


def cage_cards(con: sqlite3.Connection, *,
               assigned_to: Optional[str] = None) -> dict:
    """Group animals by shared `barcode` (one barcode = one cage card).
    Returns: {count, total_carded, total_uncarded, size_buckets, cards: [...]}.
    """
    where = f"WHERE {_ALIVE} AND barcode IS NOT NULL AND barcode != ''"
    params: list = []
    if assigned_to is not None:
        where += " AND assigned_to = ?"
        params.append(assigned_to)

    distinct = con.execute(
        f"SELECT COUNT(DISTINCT barcode) AS n FROM animals {where}", params
    ).fetchone()["n"]
    carded = con.execute(
        f"SELECT COUNT(*) AS n FROM animals {where}", params
    ).fetchone()["n"]

    rows = con.execute(f"""
        SELECT barcode,
               COUNT(*) AS mouse_count,
               GROUP_CONCAT(mouse_id, '|')      AS mouse_ids,
               GROUP_CONCAT(DISTINCT strain)    AS strains,
               GROUP_CONCAT(DISTINCT sex)       AS sexes,
               GROUP_CONCAT(DISTINCT project)   AS projects,
               GROUP_CONCAT(DISTINCT condition) AS conditions
          FROM animals
          {where}
      GROUP BY barcode
      ORDER BY mouse_count DESC, barcode
    """, params).fetchall()

    cards = [
        {
            "barcode": r["barcode"],
            "mouse_count": r["mouse_count"],
            "mouse_ids": (r["mouse_ids"] or "").split("|"),
            "strains":    [s for s in (r["strains"]    or "").split(",") if s],
            "sexes":      [s for s in (r["sexes"]      or "").split(",") if s],
            "projects":   [s for s in (r["projects"]   or "").split(",") if s],
            "conditions": [s for s in (r["conditions"] or "").split(",") if s],
        }
        for r in rows
    ]

    size_buckets = {"1 mouse": 0, "2 mice": 0, "3 mice": 0, "4 mice": 0, "5+ mice": 0}
    for r in rows:
        n = r["mouse_count"]
        if   n == 1: size_buckets["1 mouse"]  += 1
        elif n == 2: size_buckets["2 mice"]   += 1
        elif n == 3: size_buckets["3 mice"]   += 1
        elif n == 4: size_buckets["4 mice"]   += 1
        else:        size_buckets["5+ mice"]  += 1

    return {
        "count": distinct,
        "total_carded": carded,
        "size_buckets": [{"key": k, "count": v} for k, v in size_buckets.items()],
        "cards": cards,
    }


# ===========================================================================
# WRITE
# ===========================================================================

def insert(con: sqlite3.Connection, mouse_id: str, **fields) -> None:
    """Insert a new animal. Raises sqlite3.IntegrityError on dup mouse_id+project conflict.
    `synced_at` is set to now if not provided.
    """
    if not (mouse_id or "").strip():
        raise ValueError("insert: mouse_id is required.")
    fields = {**fields, "mouse_id": mouse_id, "synced_at": fields.get("synced_at") or _now_iso()}
    cols = [c for c in INSERT_FIELDS if c in fields]
    placeholders = ",".join(["?"] * len(cols))
    values = [fields[c] for c in cols]
    con.execute(
        f"INSERT INTO animals ({','.join(cols)}) VALUES ({placeholders})",
        values,
    )


def update(con: sqlite3.Connection, mouse_id: str, **fields) -> bool:
    """Update fields by mouse_id (exact match). Returns True if a row was updated.
    `synced_at` is refreshed automatically.
    """
    bad = set(fields) - set(UPDATABLE_FIELDS)
    if bad:
        raise ValueError(f"update: unknown fields {bad}")
    if not fields:
        return False
    set_clause = ", ".join(f"{k}=?" for k in fields) + ", synced_at=?"
    values = list(fields.values()) + [_now_iso(), mouse_id]
    cur = con.execute(
        f"UPDATE animals SET {set_clause} WHERE mouse_id=?",
        values,
    )
    return cur.rowcount > 0


def delete(con: sqlite3.Connection, mouse_id: str) -> bool:
    cur = con.execute("DELETE FROM animals WHERE mouse_id=?", (mouse_id,))
    return cur.rowcount > 0


def replace_all(con: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Wipe and reload the animals table from an iterable of row dicts.
    Used by the Excel sync path. Preserves the `assigned_to` mapping by
    rows.mouse_id so transfers survive a re-sync.
    Returns the number of rows inserted.
    """
    rows = list(rows)
    # snapshot owners by mouse_id (case-insensitive key)
    owners = {
        (r["mouse_id"] or "").lower(): r["assigned_to"]
        for r in con.execute(
            "SELECT mouse_id, assigned_to FROM animals WHERE assigned_to IS NOT NULL"
        ).fetchall()
    }
    con.execute("DELETE FROM animals")
    now = _now_iso()
    inserted = 0
    for r in rows:
        mid = (r.get("mouse_id") or "").strip()
        if not mid:
            continue
        cols = list(INSERT_FIELDS)
        vals = [r.get(c) for c in INSERT_FIELDS]
        if "synced_at" in cols:
            vals[cols.index("synced_at")] = r.get("synced_at") or now
        # tack on assigned_to (preserved from previous state if any)
        cols.append("assigned_to")
        vals.append(owners.get(mid.lower()))
        placeholders = ",".join(["?"] * len(cols))
        con.execute(
            f"INSERT INTO animals ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        inserted += 1
    return inserted


def rename_project(con: sqlite3.Connection, old_name: str, new_name: str) -> int:
    """Rename a project label across all animals. Returns rowcount."""
    cur = con.execute(
        "UPDATE animals SET project=? WHERE project=?", (new_name, old_name)
    )
    return cur.rowcount


# ---- Ownership / transfers ----------------------------------------------

def reassign(con: sqlite3.Connection, mouse_id: str, to_user_id: str,
             *, case_insensitive: bool = True) -> Optional[str]:
    """Set assigned_to. Returns the previous owner id (None if mouse missing
    or wasn't owned). Caller is responsible for logging to eln_mouse_transfers.
    """
    row = get(con, mouse_id, case_insensitive=case_insensitive)
    if not row:
        return None
    prev = row["assigned_to"]
    if case_insensitive:
        con.execute(
            "UPDATE animals SET assigned_to = ? WHERE LOWER(mouse_id) = LOWER(?)",
            (to_user_id, row["mouse_id"]),
        )
    else:
        con.execute(
            "UPDATE animals SET assigned_to = ? WHERE mouse_id = ?",
            (to_user_id, row["mouse_id"]),
        )
    return prev
