"""
animal_updater.py — shared pipeline: OCR lines / parsed dicts → DB diff / upsert.

Called by:
  slack_bot.py     — Slack image handler
  Claude Code      — direct CLI usage (register from local image path)
  (future)         — web UI endpoint, notebook integration

Typical flow
  image
  → ocr_engine.ocr_image()          # Apple Vision OCR
  → parse_with_gpt() or
    cage_card_parser.parse_ocr_lines()
  → build_diff()                     # preview changes
  → upsert()                         # write to DB
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

# Load .env so this module works standalone (e.g. called from Claude Code)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

ROOT    = Path(__file__).resolve().parent
DB_PATH = ROOT / "scheduler.db"

# Fields that may be written via OCR update.
# Blocks: id, synced_at, mouse_id (PK), and any future sensitive columns.
SAFE_UPDATE_FIELDS = frozenset({
    "project", "condition", "cohort", "strain", "dob", "sex",
    "date_cond1", "date_cond2", "date_cond3",
    "days_cond1", "days_cond2", "days_cond3",
    "dos", "sacrificed", "sacrificed_date", "experiment", "notes",
    "barcode",
})


# ---------- DB ----------

@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- OpenAI ----------

def openai_chat(messages: list[dict], model: str = "gpt-4o-mini",
                json_mode: bool = False) -> str:
    """Call OpenAI Chat Completions. Returns content string. Raises on failure."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set in .env")
    body: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read())["choices"][0]["message"]["content"]


_CAGE_CARD_SYS = """You extract data from a printed laboratory mouse cage card.
The card has a fixed printed template with PRINTED labels and HANDWRITTEN values.

Identify each field by its PRINTED label (do not rely on position):

  "Strain:"    → strain   e.g. "WT", "PV-Cre"
  "Mice ID:"   → mouse_id. Remove hyphens: "WT-2595" → "WT2595"
  "DOB"        → dob.  Date of birth.
  "Surgery"    → dos.  Surgery date.
  "Transition type and date 1" → Two values written together:
                 (a) a condition abbreviation: DT / NR / DLT / 23dmr / 24dmr
                 (b) a date
                 → condition = the abbreviation, date_cond1 = the date
  "Transition type and date 2" → Same format; may be blank.
                 If filled: condition = this new abbreviation, date_cond2 = the date
  "Project:"   → project.  The card has printed checkboxes; find the ONE that is checked (has a mark).
                 "Metaplasticity_dark"   → "TeMP_dark"
                 "Metaplasticity_tuning" → "TeMP_tuning"
                 "Pilot"                 → "Pilot"
                 "Something else"        → omit project
  "Cohort #"   → cohort.  Handwritten value in or near this cell (often at the right side).
  Sex symbols  → sex.  Both female (circle with cross) and male (circle with arrow) are printed;
                 the user draws a circle or mark around ONE of them.
                 Marked female → "F", marked male → "M"
  "BARCODE"    → barcode.  A handwritten number or printed sticker in this cell. Omit if blank.

DATE FORMAT RULE: Every date on this card is written as  M / D / YYYY  (month first, then day, then 4-digit year).
Example: "1 / 30 / 2026" → "2026-01-30".  "4 / 2 / 2026" → "2026-04-02".
NEVER swap month and day. The first number is ALWAYS the month.
Convert every date to ISO format YYYY-MM-DD before returning.

Return JSON: {"animals": [{mouse_id, strain, dob, dos, condition, date_cond1,
  date_cond2, project, cohort, sex, barcode}]}

Rules:
- Omit any field that is blank, empty, or illegible. Do NOT guess values.
- One card = one mouse (one object in the array).
- Return {"animals":[]} if mouse_id cannot be found."""


def parse_with_gpt_vision(image_path: str) -> list[dict] | None:
    """GPT-4o Vision: image → animal dicts directly (no OCR step needed).

    Handles rotated cards, handwriting, and complex layouts better than OCR+text.
    Returns list (possibly empty) on success, None if unavailable or errored.
    """
    if not OPENAI_API_KEY:
        return None
    try:
        import base64 as _base64
        ext = Path(image_path).suffix.lower()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        with open(image_path, "rb") as f:
            b64 = _base64.b64encode(f.read()).decode()

        raw = openai_chat(
            [{"role": "system", "content": _CAGE_CARD_SYS},
             {"role": "user", "content": [
                 {"type": "image_url",
                  "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"}},
                 {"type": "text",
                  "text": "Parse this cage card image and return all animal data as JSON."},
             ]}],
            json_mode=True,
            model="gpt-4o",
        )
        data = json.loads(raw)
        animals: list[dict] = data.get("animals", data) if isinstance(data, dict) else data
        if not isinstance(animals, list):
            return None
        animals = _normalise_mouse_ids(animals)
        print(f"[GPT vision] parsed {len(animals)} animals")
        return animals
    except Exception as e:
        print(f"[GPT vision] error: {e}")
        return None


def _normalise_mouse_ids(animals: list[dict]) -> list[dict]:
    for a in animals:
        mid = str(a.get("mouse_id") or "").strip()
        if mid:
            mid = re.sub(r"^([A-Za-z]{1,4})\s*-\s*(\d+)$",
                         lambda m: m.group(1).upper() + m.group(2), mid)
            a["mouse_id"] = mid
    return [a for a in animals if a.get("mouse_id")]


def parse_with_gpt(ocr_lines: list[str]) -> list[dict] | None:
    """GPT-4o-mini: OCR lines → structured animal dicts.

    Returns list (possibly empty) on success, None if GPT unavailable or errored.
    Caller should fall back to cage_card_parser.parse_ocr_lines() on None.
    """
    if not OPENAI_API_KEY:
        return None
    try:
        raw = openai_chat(
            [{"role": "system", "content": _CAGE_CARD_SYS},
             {"role": "user",   "content": "\n".join(ocr_lines)}],
            json_mode=True,
        )
        data = json.loads(raw)
        animals: list[dict] = data.get("animals", data) if isinstance(data, dict) else data
        if not isinstance(animals, list):
            return None
        result = _normalise_mouse_ids(animals)
        print(f"[GPT text] parsed {len(result)} animals")
        return result
    except Exception as e:
        print(f"[GPT text] error: {e}")
        return None


# ---------- Validate ----------

def validate(animals: list[dict]) -> list[str]:
    """Return list of warning strings. Empty list = all OK.

    Does NOT raise — caller decides whether to abort or proceed.
    """
    warnings: list[str] = []
    seen: set[str] = set()
    for a in animals:
        mid = a.get("mouse_id", "")
        if not mid:
            warnings.append("⚠️ mouse_id 없는 항목 있음 — 건너뜁니다")
            continue
        if mid in seen:
            warnings.append(f"⚠️ 중복 mouse_id: `{mid}`")
        seen.add(mid)
        blocked = [k for k in a if k in ("id", "synced_at") and a[k]]
        if blocked:
            warnings.append(f"⚠️ `{mid}`: 금지 필드 무시됨 → {blocked}")
    return warnings


# ---------- Diff ----------

def build_diff(animals: list[dict]) -> list[str]:
    """Compare parsed animals against current DB state.

    Returns human-readable lines suitable for Slack markdown display.
    Safe to call before upsert() to preview changes.
    """
    lines: list[str] = []
    with _db() as con:
        for a in animals:
            mid = a.get("mouse_id")
            if not mid:
                continue
            row = con.execute(
                "SELECT * FROM animals WHERE mouse_id=?", (mid,)
            ).fetchone()
            if not row:
                lines.append(f"  🆕 `{mid}` — 신규 등록")
                continue
            existing = dict(row)
            changes: list[str] = []
            for k, new_v in a.items():
                if k not in SAFE_UPDATE_FIELDS or new_v is None or new_v == "":
                    continue
                old_v = existing.get(k)
                if str(old_v or "") != str(new_v):
                    changes.append(f"    `{k}`: {old_v or '(없음)'} → {new_v}")
            if changes:
                lines.append(f"  ✏️ `{mid}` 변경:")
                lines.extend(changes)
            else:
                lines.append(f"  ✓ `{mid}` — 변경 없음")
    return lines


# ---------- Upsert ----------

def upsert(animals: list[dict]) -> dict[str, int]:
    """Write animals to DB using the SAFE_UPDATE_FIELDS allowlist.

    Returns {"inserted": n, "updated": n, "skipped": n}.
    Raises sqlite3.Error on DB failure (caller should catch).
    """
    now = _now()
    inserted = updated = skipped = 0
    with _db() as con:
        for a in animals:
            mid = a.get("mouse_id")
            if not mid:
                skipped += 1
                continue
            row = con.execute(
                "SELECT id FROM animals WHERE mouse_id=?", (mid,)
            ).fetchone()
            if row:
                fields = {k: v for k, v in a.items()
                          if k in SAFE_UPDATE_FIELDS and v is not None and v != ""}
                if fields:
                    set_clause = ", ".join(f"{k}=?" for k in fields)
                    con.execute(
                        f"UPDATE animals SET {set_clause}, synced_at=? WHERE mouse_id=?",
                        [*fields.values(), now, mid],
                    )
                updated += 1
            else:
                con.execute("""
                    INSERT INTO animals
                      (mouse_id, project, condition, cohort, strain,
                       dob, sex, dos, date_cond1, date_cond2, notes, barcode, synced_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    mid,
                    a.get("project"), a.get("condition"), a.get("cohort"),
                    a.get("strain"),  a.get("dob"),       a.get("sex"),
                    a.get("dos"),
                    a.get("date_cond1"), a.get("date_cond2"),
                    a.get("notes"), a.get("barcode"), now,
                ))
                inserted += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


# ---------- CLI entry point (for Claude Code / direct use) ----------

def register_from_image(image_path: str, project: str = "") -> dict:
    """Full pipeline: image path → OCR → parse → upsert.

    Returns {"animals": [...], "diff": [...], "result": {...}, "parser": "gpt"|"rule"}.
    Raises on OCR failure.

    Example (from Claude Code or terminal):
        from animal_updater import register_from_image
        r = register_from_image("/path/to/cage_card.jpg", project="TeMP_dark")
        print(r["diff"])   # preview
        # upsert already done — or pass dry_run=True to skip
    """
    from ocr_engine import ocr_image
    from cage_card_parser import parse_ocr_lines

    lines = ocr_image(image_path)
    if not lines:
        raise ValueError("OCR returned no text — image may be blank or unreadable")

    gpt_animals = parse_with_gpt(lines)
    if gpt_animals is not None:
        animals = gpt_animals
        parser = "gpt"
    else:
        _, animals = parse_ocr_lines(lines)
        parser = "rule"

    if project:
        for a in animals:
            if not a.get("project"):
                a["project"] = project

    warnings = validate(animals)
    diff = build_diff(animals)
    result = upsert(animals)

    return {
        "animals": animals,
        "diff": diff,
        "warnings": warnings,
        "result": result,
        "parser": parser,
        "ocr_lines": lines,
    }
