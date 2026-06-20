"""Validating · non-destructive · audited animal sync (ROADMAP §B v1).

Replaces the old destructive `animals_dal.replace_all` (a blind
``DELETE FROM animals`` → bulk re-INSERT) with a safe pipeline:

    normalize → validate → snapshot → diff → apply → audit → report

Guarantees:
  * **Bad rows are rejected**, never written — one mis-read cage card can no
    longer replace the canonical table. Each rejection is audited.
  * **A mouse_id that vanishes from the source is soft-removed** (a ``removed``
    flag + audit row), *never hard-deleted*. An accidental Sheet-row deletion
    can't vaporise an animal, and app-only columns (assigned_to, …) survive.
  * **A full pre-sync snapshot** of `animals` is stored for point-in-time
    rollback ("what condition was mouse X in on date Y").
  * **Every change is written to `animal_audit`** (field-level for edits).
  * A structured **sync report** is returned (also shown in the Animals-DB
    sidebar).

SQL lives here; callers pass an open ``sqlite3.Connection`` so transaction
control stays with them. Pure functions (`normalize_row`, `validate_rows`,
`diff_rows`) take no connection and are unit-tested directly.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import date as _date, datetime, timezone
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------------------
# Column sets
# ---------------------------------------------------------------------------
# Sheet-derived columns we upsert. mouse_id is the key (handled separately).
# days_cond1..3 are still written for backward compat but the live
# `animal_with_state` view recomputes them, so they are excluded from the diff
# to avoid daily "modified" noise.
SHEET_FIELDS = (
    "project", "condition", "experiment", "cohort", "strain",
    "dob", "sex", "dos",
    "date_cond1", "date_cond2", "date_cond3",
    "days_cond1", "days_cond2", "days_cond3",
    "sacrificed", "sacrificed_date", "notes", "barcode",
)

# Fields compared for the diff / field-level audit (volatile day-counts excluded).
COMPARE_FIELDS = (
    "project", "condition", "experiment", "cohort", "strain",
    "dob", "sex", "dos",
    "date_cond1", "date_cond2", "date_cond3",
    "sacrificed", "sacrificed_date", "notes", "barcode",
)

_DATE_FIELDS = ("dob", "dos", "date_cond1", "date_cond2", "date_cond3", "sacrificed_date")

# Keep snapshot growth bounded — one snapshot per sync, last N retained.
_SNAPSHOT_KEEP = 60

_MOUSE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./\-]*$")

# Strain alias canonicalisation (CLAUDE.md "Naming Conventions"). Tolerate
# x / × / X and arbitrary whitespace between the two halves; case-insensitive.
# Append (pattern, canonical) rows as new aliases are agreed.
_STRAIN_ALIASES = (
    (re.compile(r"^gcamp6f\s*[x×]\s*l7-?cre$", re.IGNORECASE), "L7-Ai95D"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_sync_id() -> str:
    return secrets.token_hex(5)


# ---------------------------------------------------------------------------
# Normalisation (pure)
# ---------------------------------------------------------------------------

def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _norm_sex(v: Any) -> str:
    s = _s(v).lower()
    if s in ("m", "male"):
        return "M"
    if s in ("f", "female"):
        return "F"
    return _s(v)  # leave as-is; validation will reject if non-empty & not M/F


def _canon_strain(v: Any) -> str:
    s = _s(v)
    if not s:
        return ""
    collapsed = re.sub(r"\s+", " ", s).strip()
    for pat, canon in _STRAIN_ALIASES:
        if pat.match(collapsed):
            return canon
    return s


def _norm_sacrificed(v: Any) -> str:
    s = _s(v).lower()
    if s in ("y", "yes", "1", "true", "t"):
        return "Y"
    if s in ("n", "no", "0", "false", "f"):
        return "N"
    return _s(v)  # unknown token kept verbatim; validation warns


def parse_date(v: Any) -> tuple[Optional[str], bool]:
    """Return (YYYY-MM-DD or None, parsed_ok). Empty → (None, True).
    Non-empty but unparseable → (original, False)."""
    s = _s(v)
    if not s:
        return None, True
    s = s.split(" ")[0].strip()
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$", s)
        if not m:
            return s, False
        a, b, y = (int(x) for x in m.groups())
        # US MM/DD default; fall back to DD/MM only when a>12 forces it.
        mo, d = (b, a) if (a > 12 and b <= 12) else (a, b)
    try:
        _date(y, mo, d)
    except ValueError:
        return s, False
    return f"{y:04d}-{mo:02d}-{d:02d}", True


def normalize_row(raw: dict) -> dict:
    """Trim, canonicalise strain, normalise sex/sacrificed, parse dates.
    Returns a dict over SHEET_FIELDS + mouse_id. Unparseable dates are kept
    verbatim so validation can reject them."""
    out: dict[str, Any] = {"mouse_id": _s(raw.get("mouse_id"))}
    for f in SHEET_FIELDS:
        if f in _DATE_FIELDS:
            norm, _ = parse_date(raw.get(f))
            out[f] = norm
        elif f == "sex":
            out[f] = _norm_sex(raw.get(f))
        elif f == "strain":
            out[f] = _canon_strain(raw.get(f))
        elif f == "sacrificed":
            out[f] = _norm_sacrificed(raw.get(f))
        else:
            out[f] = _s(raw.get(f)) or None
    return out


# ---------------------------------------------------------------------------
# Validation (pure)
# ---------------------------------------------------------------------------

def validate_row(norm: dict) -> tuple[list[str], list[str]]:
    """Validate a *normalised* row. Returns (errors, warnings).
    Non-empty errors ⇒ the row is rejected (blocked from the DB)."""
    errors: list[str] = []
    warnings: list[str] = []

    mid = norm.get("mouse_id") or ""
    if not mid:
        errors.append("mouse_id is empty")
    elif not _MOUSE_ID_RE.match(mid):
        errors.append(f"mouse_id '{mid}' has invalid characters")

    sex = norm.get("sex") or ""
    if not sex:
        warnings.append("sex is empty")
    elif sex not in ("M", "F"):
        errors.append(f"sex '{sex}' is not M/F")

    # Dates: reject non-empty unparseable; warn on implausible dob.
    for f in _DATE_FIELDS:
        raw = norm.get(f)
        if raw is None:
            continue
        _, ok = parse_date(raw)
        if not ok:
            errors.append(f"{f} '{raw}' is not a valid date")

    dob = norm.get("dob")
    if dob:
        d, ok = parse_date(dob)
        if ok and d:
            try:
                dd = _date.fromisoformat(d)
                if dd > _date.today():
                    warnings.append("dob is in the future")
                elif dd.year < 2000:
                    warnings.append("dob is implausibly old")
            except ValueError:
                pass
    else:
        warnings.append("dob is empty")

    if not (norm.get("strain") or ""):
        warnings.append("strain is empty")

    sac = norm.get("sacrificed") or ""
    if sac and sac not in ("Y", "N"):
        warnings.append(f"sacrificed '{sac}' is not Y/N")
    if sac == "Y" and not (norm.get("sacrificed_date") or ""):
        warnings.append("sacrificed=Y but no sacrificed_date")

    # Date-ordering sanity (only among present, parseable dates).
    order = []
    for f in ("dob", "dos", "date_cond1", "date_cond2", "date_cond3", "sacrificed_date"):
        v = norm.get(f)
        if v:
            d, ok = parse_date(v)
            if ok and d:
                order.append((f, d))
    for (fa, a), (fb, b) in zip(order, order[1:]):
        if a > b:
            warnings.append(f"{fa} ({a}) is after {fb} ({b})")

    return errors, warnings


def _compare_sig(n: dict) -> tuple:
    """Signature over COMPARE_FIELDS used to tell identical rows apart."""
    return tuple((n.get(f) or "") for f in COMPARE_FIELDS)


def validate_rows(raw_rows: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Normalise + validate every row. Returns (valid, rejected).

    Each ``valid`` item is the normalised row dict. Each ``rejected`` item is
    ``{mouse_id, errors, warnings, row_index}``.

    Duplicate mouse_ids within the source (case-insensitive) are handled by
    *content*, not just by name:
      * **identical** duplicates (same COMPARE_FIELDS) → harmless double-entry:
        collapse to a single valid row and warn (no manual cleanup needed).
      * **conflicting** duplicates (same id, differing data — which is correct?)
        → ambiguous: reject *all* members of the set and flag.
    """
    normed = [normalize_row(r) for r in raw_rows]

    # Group row indices by mouse_id, then classify each duplicate group.
    groups: dict[str, list[int]] = {}
    for i, n in enumerate(normed):
        k = (n.get("mouse_id") or "").lower()
        if k:
            groups.setdefault(k, []).append(i)

    conflicting: set[str] = set()      # mouse_id keys → reject all members
    merged_away: set[int] = set()      # redundant identical-dup indices → drop
    merge_warn: dict[int, str] = {}    # kept index → "merged N identical rows"
    for k, idxs in groups.items():
        if len(idxs) <= 1:
            continue
        sigs = {_compare_sig(normed[i]) for i in idxs}
        if len(sigs) == 1:
            keep = idxs[0]
            merge_warn[keep] = f"merged {len(idxs)} identical duplicate rows from source"
            merged_away.update(idxs[1:])
        else:
            conflicting.add(k)

    valid: list[dict] = []
    rejected: list[dict] = []
    for i, n in enumerate(normed):
        if i in merged_away:
            continue  # identical duplicate — silently collapsed into the kept row
        errors, warnings = validate_row(n)
        warnings = list(warnings)
        if i in merge_warn:
            warnings.append(merge_warn[i])
        k = (n.get("mouse_id") or "").lower()
        if k and k in conflicting:
            errors = list(errors) + ["conflicting duplicate mouse_id within source"]
        rec = {"mouse_id": n.get("mouse_id") or "", "warnings": warnings, "row_index": i}
        if errors:
            rejected.append({**rec, "errors": errors})
        else:
            valid.append({**n, "_warnings": warnings, "_row_index": i})
    return valid, rejected


# ---------------------------------------------------------------------------
# Diff (reads current `animals`, no writes)
# ---------------------------------------------------------------------------

def diff_rows(con, valid: list[dict]) -> dict:
    """Compare valid (normalised) rows against current `animals` by mouse_id
    (case-insensitive). Returns:
        {added:[row], modified:[{mouse_id, row, changes:{f:[old,new]}, was_removed}],
         unchanged:[mouse_id], current_by_id:{lower_mid: row}}
    """
    cur_rows = con.execute(
        "SELECT id, mouse_id, " + ", ".join(COMPARE_FIELDS) + ", removed FROM animals"
    ).fetchall()
    # One representative per mouse_id, preferring an alive row over a removed one
    # (so a soft-removed / dedup'd twin never shadows the live record).
    current: dict[str, Any] = {}
    for r in cur_rows:
        k = (r["mouse_id"] or "").lower()
        prev = current.get(k)
        if prev is None or (bool(prev["removed"]) and not bool(r["removed"])):
            current[k] = r

    added: list[dict] = []
    modified: list[dict] = []
    unchanged: list[str] = []

    for row in valid:
        k = (row["mouse_id"] or "").lower()
        existing = current.get(k)
        if existing is None:
            added.append(row)
            continue
        changes: dict[str, list] = {}
        for f in COMPARE_FIELDS:
            old = existing[f]
            new = row.get(f)
            if (old or "") != (new or ""):
                changes[f] = [old, new]
        was_removed = bool(existing["removed"]) if "removed" in existing.keys() else False
        if changes or was_removed:
            modified.append({"mouse_id": existing["mouse_id"], "id": existing["id"],
                             "row": row, "changes": changes, "was_removed": was_removed})
        else:
            unchanged.append(existing["mouse_id"])

    return {"added": added, "modified": modified, "unchanged": unchanged,
            "current_by_id": current}


# ---------------------------------------------------------------------------
# Audit / snapshot helpers
# ---------------------------------------------------------------------------

def _audit(con, *, sync_id, source, actor, mouse_id, change_type,
           field=None, old_value=None, new_value=None, detail=None) -> None:
    con.execute(
        """INSERT INTO animal_audit
           (ts, sync_id, source, actor, mouse_id, change_type, field, old_value, new_value, detail)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (_now_iso(), sync_id, source, actor, mouse_id, change_type, field,
         None if old_value is None else str(old_value),
         None if new_value is None else str(new_value),
         None if detail is None else json.dumps(detail, default=str)),
    )


def snapshot_animals(con, sync_id: str, ts: Optional[str] = None) -> int:
    """Store a full pre-sync JSON snapshot of `animals`. Returns row_count."""
    ts = ts or _now_iso()
    rows = [dict(r) for r in con.execute("SELECT * FROM animals").fetchall()]
    con.execute(
        "INSERT INTO animal_snapshots (ts, sync_id, row_count, payload) VALUES (?,?,?,?)",
        (ts, sync_id, len(rows), json.dumps(rows, default=str)),
    )
    # Bound growth: keep only the most recent _SNAPSHOT_KEEP snapshots.
    con.execute(
        """DELETE FROM animal_snapshots WHERE id NOT IN
             (SELECT id FROM animal_snapshots ORDER BY id DESC LIMIT ?)""",
        (_SNAPSHOT_KEEP,),
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Apply (writes) + orchestration
# ---------------------------------------------------------------------------

def _insert_row(con, row: dict, ts: str) -> None:
    cols = ["mouse_id"] + list(SHEET_FIELDS) + ["synced_at", "removed"]
    vals = [row["mouse_id"]] + [row.get(f) for f in SHEET_FIELDS] + [ts, 0]
    con.execute(
        f"INSERT INTO animals ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})",
        vals,
    )


def _update_row(con, row_id: int, row: dict, ts: str) -> None:
    set_cols = list(SHEET_FIELDS) + ["synced_at", "removed", "removed_at"]
    set_clause = ", ".join(f"{c}=?" for c in set_cols)
    vals = [row.get(f) for f in SHEET_FIELDS] + [ts, 0, None, row_id]
    con.execute(f"UPDATE animals SET {set_clause} WHERE id=?", vals)


def dedup_existing(con, *, source: str, actor: Optional[str], sync_id: str,
                   ts: str) -> tuple[list[str], list[str]]:
    """Collapse rows already in `animals` that share a mouse_id.

    Identical duplicates (same COMPARE_FIELDS) → keep one (preferring a row that
    already carries an owner), soft-remove the redundant copies with a 'merged'
    audit. Conflicting duplicates are left untouched and reported as warnings.
    Returns (merged_mouse_ids, conflicting_mouse_ids).
    """
    rows = con.execute("SELECT * FROM animals WHERE COALESCE(removed,0)=0").fetchall()
    by_id: dict[str, list] = {}
    for r in rows:
        by_id.setdefault((r["mouse_id"] or "").lower(), []).append(r)

    merged: list[str] = []
    conflicts: list[str] = []
    for k, grp in by_id.items():
        if not k or len(grp) <= 1:
            continue
        sigs = {tuple((r[f] or "") for f in COMPARE_FIELDS) for r in grp}
        if len(sigs) != 1:
            conflicts.append(grp[0]["mouse_id"])
            continue
        # keep the row with an owner if any, else the lowest id
        keep = sorted(grp, key=lambda r: ((r["assigned_to"] or "") == "", r["id"]))[0]
        for extra in grp:
            if extra["id"] == keep["id"]:
                continue
            con.execute("UPDATE animals SET removed=1, removed_at=? WHERE id=?",
                        (ts, extra["id"]))
            merged.append(extra["mouse_id"])
            _audit(con, sync_id=sync_id, source=source, actor=actor,
                   mouse_id=extra["mouse_id"], change_type="merged",
                   detail={"reason": "identical duplicate row collapsed",
                           "kept_id": keep["id"], "removed_id": extra["id"]})
    return merged, conflicts


def run_sync(con, raw_rows: Iterable[dict], *, source: str,
             actor: Optional[str] = None, sync_id: Optional[str] = None) -> dict:
    """Full safe-sync pipeline. Commits on success. Returns the sync report."""
    raw_rows = list(raw_rows)
    sync_id = sync_id or new_sync_id()
    ts = _now_iso()

    valid, rejected = validate_rows(raw_rows)

    # Soft-remove only applies to mice genuinely absent from the source. A row
    # that is *present* but rejected must NOT be removed — keep its last good
    # DB state. So the "present" set is valid ∪ rejected mouse_ids.
    present = {(v["mouse_id"] or "").lower() for v in valid if v["mouse_id"]}
    present |= {(r["mouse_id"] or "").lower() for r in rejected if r["mouse_id"]}

    snap_rows = snapshot_animals(con, sync_id, ts)

    # Collapse pre-existing identical duplicate rows down to one before diffing.
    merged_ids, conflict_ids = dedup_existing(
        con, source=source, actor=actor, sync_id=sync_id, ts=ts)

    diff = diff_rows(con, valid)

    # --- apply added / modified (target row id so merged twins aren't revived) ---
    for row in diff["added"]:
        _insert_row(con, row, ts)
        _audit(con, sync_id=sync_id, source=source, actor=actor,
               mouse_id=row["mouse_id"], change_type="added",
               detail={f: row.get(f) for f in COMPARE_FIELDS})
    for m in diff["modified"]:
        _update_row(con, m["id"], m["row"], ts)
        if m["was_removed"]:
            _audit(con, sync_id=sync_id, source=source, actor=actor,
                   mouse_id=m["mouse_id"], change_type="unremoved")
        for field, (old, new) in m["changes"].items():
            _audit(con, sync_id=sync_id, source=source, actor=actor,
                   mouse_id=m["mouse_id"], change_type="modified",
                   field=field, old_value=old, new_value=new)

    # --- soft-remove absent (never hard-delete) ---
    removed_ids: list[str] = []
    for k, r in diff["current_by_id"].items():
        already = bool(r["removed"]) if "removed" in r.keys() else False
        if k not in present and not already:
            con.execute(
                "UPDATE animals SET removed=1, removed_at=? WHERE id=?",
                (ts, r["id"]),
            )
            removed_ids.append(r["mouse_id"])
            _audit(con, sync_id=sync_id, source=source, actor=actor,
                   mouse_id=r["mouse_id"], change_type="removed",
                   detail={"reason": "absent from source"})

    # --- audit rejected ---
    for rej in rejected:
        _audit(con, sync_id=sync_id, source=source, actor=actor,
               mouse_id=rej["mouse_id"] or None, change_type="rejected",
               detail={"errors": rej["errors"], "warnings": rej["warnings"],
                       "row_index": rej["row_index"]})

    con.execute(
        "INSERT OR REPLACE INTO animal_config (key,value) VALUES ('last_synced',?)", (ts,)
    )
    con.commit()

    warnings = [{"mouse_id": v["mouse_id"], "warnings": v["_warnings"]}
                for v in valid if v.get("_warnings")]
    report = {
        "sync_id": sync_id,
        "ts": ts,
        "source": source,
        "snapshot_rows": snap_rows,
        "counts": {
            "added": len(diff["added"]),
            "modified": len(diff["modified"]),
            "removed": len(removed_ids),
            "merged": len(merged_ids),
            "rejected": len(rejected),
            "unchanged": len(diff["unchanged"]),
            "total_source_rows": len(raw_rows),
        },
        "rejected": [{"mouse_id": r["mouse_id"], "errors": r["errors"],
                      "row_index": r["row_index"]} for r in rejected],
        "removed": removed_ids,
        "merged": merged_ids,
        "conflicts": conflict_ids,
        "warnings": warnings,
    }
    return report
