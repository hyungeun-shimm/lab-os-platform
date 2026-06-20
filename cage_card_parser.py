"""Rule-based parser: OCR text → animal DB fields.

Handles two image types:
  1. Cage card photo  — extracts one animal's info
  2. DB screenshot    — extracts multiple rows (table format)
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional


# ── Condition mapping rules ──────────────────────────────────────────────────
# Rack / room text → condition code
RACK_CONDITION_MAP: list[tuple[str, str]] = [
    (r"dark\s*chamber",      "DT"),
    (r"dim\s*chamber\s*23",  "23dmr"),
    (r"dim\s*chamber\s*24",  "24dmr"),
    (r"dim\s*23",            "23dmr"),
    (r"dim\s*24",            "24dmr"),
    (r"rack\s*2[aA]",        "NR"),
    (r"\b2[aA]\b",           "NR"),
    (r"normal\s*rearing",    "NR"),
]

def _condition_from_text(text: str) -> str:
    tl = text.lower()
    for pattern, code in RACK_CONDITION_MAP:
        if re.search(pattern, tl):
            return code
    return ""


# ── Date helpers ─────────────────────────────────────────────────────────────
def _parse_date(s: str) -> Optional[str]:
    """Parse various date formats → YYYY-MM-DD or None."""
    s = s.strip()
    # Already ISO
    if re.match(r'\d{4}-\d{2}-\d{2}', s):
        return s[:10]
    # M/D/YY or M/D/YYYY
    m = re.match(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})$', s)
    if m:
        mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:
            yr += 2000
        try:
            return date(yr, mo, day).isoformat()
        except ValueError:
            pass
    return None


# ── Mouse ID normalisation ────────────────────────────────────────────────────
def _normalise_mouse_id(raw: str) -> str:
    """WT-3723 / WT 3723 / WT3723 → WT3723 (no dash)."""
    m = re.match(r'^([A-Za-z]{1,4})\s*[-_]?\s*(\d{3,6})$', raw.strip())
    if m:
        return f"{m.group(1).upper()}{m.group(2)}"
    return raw.strip()


# ── Sex extraction ────────────────────────────────────────────────────────────
def _extract_sex(text: str) -> str:
    tl = text.lower()
    has_m = bool(re.search(r'\b(male|♂|m\b)', tl))
    has_f = bool(re.search(r'\b(female|♀|f\b)', tl))
    if has_f and not has_m: return "F"
    if has_m and not has_f: return "M"
    if has_f and has_m:     return "F"   # cage card default: female listed first
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# CAGE CARD PARSER
# ═══════════════════════════════════════════════════════════════════════════════
# Patterns that look like field labels (to skip when grabbing values)
_LABEL_PATTERN = re.compile(
    r'^(transition|type|date|accli|surgery|something|pilot|ex\s|\d{2}/\d{2}/\d{4})',
    re.I,
)

def _is_label_line(s: str) -> bool:
    """Return True if the string looks like a field label rather than a value."""
    s = s.strip()
    if len(s) > 40:           return True   # long → probably a label sentence
    if s.endswith(':'):        return True
    if re.search(r'[:#]', s): return True   # contains label punctuation
    if _LABEL_PATTERN.match(s): return True
    return False

def _next_value_after(lines: list[str], keyword_re: str,
                      skip_if_date: bool = False,
                      require_date: bool = False) -> str:
    """Find lines matching keyword_re, then return the first non-label value line after it."""
    found_idx = -1
    for i, line in enumerate(lines):
        if re.search(keyword_re, line, re.I):
            found_idx = i
            break
    if found_idx < 0:
        return ""
    for line in lines[found_idx + 1:]:
        v = line.strip()
        if not v:
            continue
        if _is_label_line(v):
            continue
        if skip_if_date and re.match(r'^\d{1,3}/\d{2}', v):
            continue
        if require_date and not re.match(r'^\d{1,4}[/\-]', v):
            continue
        return v
    return ""


def parse_cage_card(lines: list[str]) -> dict[str, Any]:
    """Extract a single animal record from cage card OCR lines."""
    text = "\n".join(lines)
    data: dict[str, Any] = {}

    # Mouse ID  (WT-3723, WT3723, M001, etc.)
    m = re.search(r'\b([A-Za-z]{1,4}[-_\s]?\d{3,6})\b', text)
    if m:
        data["mouse_id"] = _normalise_mouse_id(m.group(1))

    # Strain — skip OCR noise words like "IVIICE" (misread of "MICE ID:")
    _STRAIN_NOISE = {'mice', 'iviice', 'mouse', 'id', 'td', 'cage', 'card', 'label'}
    strain_val = ""
    strain_cands = re.findall(r'Strain[:\s]+(\S+)', text, re.I)
    for cand in strain_cands:
        c = cand.rstrip(':').strip()
        if (not _is_label_line(c)
                and c.lower() not in _STRAIN_NOISE
                and re.match(r'^[A-Za-z][A-Za-z0-9/\-]{0,10}$', c)):
            strain_val = c
            break
    if not strain_val:
        # Known strain names anywhere in text (standalone or as prefix of mouse ID)
        for pat, name in [
            (r'\bWT\b',  'WT'),
            (r'\bC57\b', 'C57'),
            (r'\bBL6\b', 'C57BL/6'),
            (r'\bWT[-_]?\d{3,6}\b', 'WT'),   # "WT3723" → WT
            (r'\bC57[-_]?\d{3,6}\b', 'C57'),
        ]:
            if re.search(pat, text):
                strain_val = name
                break
    if strain_val:
        data["strain"] = strain_val

    # DOB — look near "DOB" label; also handle OCR-compressed dates like "118/26" → "1/18/26"
    _DATE_LIKE = re.compile(r'^\d{1,4}[/\-]\d{1,2}')  # looks like a date value

    dob_str = ""
    # Try inline: "DOB: 1/18/26" or "DOB 1/18/26"
    m = re.search(r'DOB[:\s]+(\S+)', text, re.I)
    if m:
        cand = m.group(1).strip()
        # Only accept if it looks like a date (starts with digit+slash) and not a label
        if _DATE_LIKE.match(cand) and not _is_label_line(cand):
            dob_str = cand
    if not dob_str:
        # DOB is on its own line — find next date-looking value skipping labels
        dob_str = _next_value_after(lines, r'^DOB\s*[:\s]?$', require_date=True)
    if not dob_str:
        # Broader: next non-label value after any line containing "DOB"
        for i, line in enumerate(lines):
            if re.search(r'\bDOB\b', line, re.I):
                for nxt in lines[i+1:]:
                    v = nxt.strip()
                    if v and not _is_label_line(v) and _DATE_LIKE.match(v):
                        dob_str = v
                        break
                break

    # Try to fix OCR-compressed date: "118/26" → "1/18/26", "1826" → skip
    def _fix_ocr_date(s: str) -> str:
        s = s.strip()
        # Already has slashes — parse directly
        if re.match(r'\d{1,2}/\d{1,2}/\d{2,4}', s):
            return s
        # Compressed: 3–4 digit + /YY(YY) → insert missing slash
        m2 = re.match(r'^(\d{3,4})/(\d{2,4})$', s)
        if m2:
            num, yr = m2.group(1), m2.group(2)
            if len(num) == 3:       # 118 → 1/18
                return f"{num[0]}/{num[1:]}/{yr}"
            elif len(num) == 4:     # 1218 → 12/18
                return f"{num[:2]}/{num[2:]}/{yr}"
        return s

    if dob_str:
        fixed = _fix_ocr_date(dob_str)
        parsed = _parse_date(fixed)
        # Reject far-future dates (likely cage expiry, not DOB)
        if parsed:
            try:
                dob_year = int(parsed[:4])
                if dob_year <= date.today().year + 1:
                    data["dob"] = parsed
            except Exception:
                data["dob"] = parsed

    # Sex
    sex = _extract_sex(text)
    if sex:
        data["sex"] = sex

    # Project — skip date-looking values; prefer known project names or checkbox lines
    proj = ""
    m = re.search(r'Project[:\s]+([^\n✓✗\[\]]+)', text, re.I)
    if m:
        cand = m.group(1).strip()
        # Reject if it looks like a date or a bare number
        if re.match(r'^\d', cand):
            cand = ""
        proj = cand
    if not proj:
        # Scan forward after "Project:" for the first non-date, non-label value
        proj = _next_value_after(lines, r'Project\s*:', skip_if_date=True)
    if not proj:
        # Checkbox fallback
        m2 = re.search(r'(?:✓|☑|✅|\[x\]|\[X\])\s*([A-Za-z][^\n]{2,40})', text)
        if m2:
            proj = m2.group(1).strip()
    # Normalise cage card project label → canonical project name
    # Cage card shows "Metaplasticity_dark / _tuning" but lab project is "TeMP_dark / TeMP_tuning"
    _PROJ_ALIASES = {
        'metaplasticity_dark':  'TeMP_dark',
        'metaplasticity_dim':   'TeMP_tuning',
        'metaplasticity_tuning':'TeMP_tuning',
        'temp_dark':            'TeMP_dark',
        'temp_tuning':          'TeMP_tuning',
    }
    if proj:
        data["project"] = _PROJ_ALIASES.get(proj.lower().replace(' ', '_'), proj)

    # Cohort — find value after "Cohort" that is short and not a label sentence
    cohort_val = _next_value_after(lines, r'Cohort\s*[#:\s]')
    if cohort_val and len(cohort_val) <= 30 and not _is_label_line(cohort_val):
        data["cohort"] = cohort_val

    # Condition from room/rack text (lower priority — overridden by transition dates below)
    cond = _condition_from_text(text)
    if cond:
        data["condition"] = cond

    # Transition dates override rack condition (more specific)
    # "L-D: 1/13/2026"  → date_cond1, condition = DT
    m = re.search(r'L[-\s]*D[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})', text, re.I)
    if m:
        data["date_cond1"] = _parse_date(m.group(1))
        data["condition"] = "DT"   # always overrides rack

    # "LDL - 2/1/2026"  → date_cond2, condition = DLT
    m = re.search(r'LDL[\s\-:]+(\d{1,2}/\d{1,2}/\d{2,4})', text, re.I)
    if m:
        data["date_cond2"] = _parse_date(m.group(1))
        data["condition"] = "DLT"  # always overrides DT

    # Notes (last line or explicit Notes: field)
    m = re.search(r'Note[s]?[:\s]+(.+)', text, re.I)
    if m:
        data["notes"] = m.group(1).strip()

    return data


# ═══════════════════════════════════════════════════════════════════════════════
# DB SCREENSHOT PARSER (table format)
# ═══════════════════════════════════════════════════════════════════════════════
# Expected column order from the user's DB screenshot:
# Use | Strain | Sex | Age | DOB | Mouse ID | Genotype | Cage ID | Room | Rack | Notes
_DB_HEADERS = ["use", "strain", "sex", "age", "dob", "mouse id", "genotype",
               "cage id", "room", "rack", "notes"]

def _is_header_row(line: str) -> bool:
    ll = line.lower()
    return sum(1 for h in _DB_HEADERS if h in ll) >= 3

def _rack_to_condition(rack: str, room: str = "") -> str:
    combined = f"{room} {rack}".strip()
    return _condition_from_text(combined) or ""

def parse_db_screenshot(lines: list[str]) -> list[dict[str, Any]]:
    """Parse a DB/Excel screenshot — returns list of animal dicts."""
    animals = []
    header_found = False
    col_order: list[str] = []

    for line in lines:
        if not header_found:
            if _is_header_row(line):
                header_found = True
                # Build column index from header tokens
                tokens = re.split(r'\s{2,}|\t', line)
                col_order = [t.strip().lower() for t in tokens]
            continue

        # Skip empty lines or separator lines
        if not line.strip() or re.match(r'^[-=\s]+$', line):
            continue

        # Don't strip before split — preserves leading empty token for "use" column
        tokens = [t.strip() for t in re.split(r'\s{2,}|\t', line)]
        if len([t for t in tokens if t]) < 3:
            continue

        row: dict[str, Any] = {}
        for i, val in enumerate(tokens):
            if i < len(col_order):
                col = col_order[i]
            else:
                break
            val = val.strip()
            if not val or val in ("-", "—", "N/A"):
                continue

            if "mouse" in col or col == "id":
                row["mouse_id"] = _normalise_mouse_id(val)
            elif col == "strain":
                row["strain"] = val
            elif col == "sex":
                row["sex"] = "M" if val.upper() in ("M", "MALE") else "F" if val.upper() in ("F", "FEMALE") else val
            elif col == "dob":
                row["dob"] = _parse_date(val)
            elif col == "rack":
                row["condition"] = _rack_to_condition(val, row.get("room", ""))
                row["_rack"] = val
            elif col == "room":
                row["room"] = val
            elif col == "cage" or "cage" in col:
                row["_cage_id"] = val
            elif col == "notes":
                row["notes"] = val
                # Check for transition date notes
                m = re.search(r'L[-\s]*D[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})', val, re.I)
                if m: row["date_cond1"] = _parse_date(m.group(1))
                m2 = re.search(r'LDL[\s\-:]+(\d{1,2}/\d{1,2}/\d{2,4})', val, re.I)
                if m2: row["date_cond2"] = _parse_date(m2.group(1))

        if row.get("mouse_id"):
            animals.append(row)

    return animals


# ═══════════════════════════════════════════════════════════════════════════════
# AUTO-DETECT which type of image was OCR'd
# ═══════════════════════════════════════════════════════════════════════════════
def _looks_like_cage_card(lines: list[str]) -> bool:
    """Cage cards have key:value label format, not tabular rows."""
    text = "\n".join(lines).lower()
    # Strong cage card signals
    card_signals = [
        r'cage\s*card', r'cohort\s*#', r'cohort:', r'dob\s*:',
        r'strain\s*:', r'l-d\s*:', r'ldl\s*[-:]',
    ]
    score = sum(1 for p in card_signals if re.search(p, text))
    # Key:value pairs also indicate a form/card
    kv_pairs = len(re.findall(r'\w[\w\s]{1,20}:\s*\S', text))
    return score >= 1 or kv_pairs >= 3

def _looks_like_db_table(lines: list[str]) -> bool:
    """DB screenshots have a header row with 4+ column names."""
    for line in lines[:8]:
        if _is_header_row(line):
            return True
    return False

def parse_ocr_lines(lines: list[str]) -> tuple[str, list[dict]]:
    """Auto-detect image type and parse.

    Returns: (image_type, list_of_animal_dicts)
      image_type: 'cage_card' | 'db_screenshot' | 'unknown'
    """
    if _looks_like_cage_card(lines):
        result = parse_cage_card(lines)
        return ("cage_card", [result] if result.get("mouse_id") else [])

    if _looks_like_db_table(lines):
        animals = parse_db_screenshot(lines)
        return ("db_screenshot", animals)

    # Last resort: try cage card if any mouse ID found
    result = parse_cage_card(lines)
    if result.get("mouse_id"):
        return ("cage_card", [result])

    return ("unknown", [])


# ── Pretty-print for Slack confirmation ──────────────────────────────────────
def fmt_parsed_animal(a: dict, idx: int = 0) -> str:
    fields = []
    if a.get("mouse_id"):    fields.append(f"🐭 ID: `{a['mouse_id']}`")
    if a.get("strain"):      fields.append(f"Strain: {a['strain']}")
    if a.get("sex"):         fields.append(f"Sex: {'♂' if a['sex']=='M' else '♀'}")
    if a.get("dob"):         fields.append(f"DOB: {a['dob']}")
    if a.get("condition"):   fields.append(f"Condition: *{a['condition']}*")
    if a.get("cohort"):      fields.append(f"Cohort: {a['cohort']}")
    if a.get("project"):     fields.append(f"Project: {a['project']}")
    if a.get("date_cond1"):  fields.append(f"Cond-1 date: {a['date_cond1']}")
    if a.get("date_cond2"):  fields.append(f"Cond-2 date: {a['date_cond2']}")
    if a.get("notes"):       fields.append(f"Notes: {a['notes'][:60]}")
    prefix = f"{idx}. " if idx else ""
    return prefix + " | ".join(fields)
