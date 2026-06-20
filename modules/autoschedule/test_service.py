"""Sanity test for the autoschedule service orchestration, WITHOUT calling Gemini.

    .venv/bin/python -m modules.autoschedule.test_service

Exercises ``preview_from_params`` (the LLM-free half of ``preview``) against an
in-memory DB, so the parse → resolve → distribute → conflict wiring is verified
even with no API key present.
"""
import sqlite3

from modules.autoschedule import service


def _seed_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        "CREATE TABLE animals (mouse_id TEXT, cohort TEXT, sacrificed TEXT);"
        "CREATE TABLE events (title TEXT, start_at TEXT, end_at TEXT, assignee TEXT);"
    )
    con.executemany(
        "INSERT INTO animals VALUES (?,?,?)",
        [("WT3479", "dA", ""), ("WT3480", "dA", ""), ("WT3481", "dA", ""),
         ("CT001", "dA-con", ""), ("CT002", "dA-con", "")],
    )
    con.execute("INSERT INTO events VALUES (?,?,?,?)",
                ("Lab meeting", "2026-07-01T09:30", "2026-07-01T10:30", "u_me"))
    con.commit()
    return con


def main() -> None:
    con = _seed_db()
    # params as Gemini WOULD return them (we skip the network call here)
    params = {
        "cohort": "dA", "control_cohort": "dA-con", "category": "behavior",
        "subcategory": "VOR / OKR", "date_from": "2026-07-01", "date_to": "2026-07-10",
        "per_day": 2, "weekdays_only": True, "mix_control": True,
        "start_time": "09:00", "duration_min": 60,
    }
    out = service.preview_from_params(con, params, assignee="u_me")

    assert out["summary"]["mice"] == 5, out["summary"]
    assert out["summary"]["scheduled"] == 5, out["summary"]
    assert out["summary"]["conflicts"] >= 1, out["summary"]   # 09:00/10:00 vs meeting
    assert all(e["category"] == "behavior" for e in out["events"])
    assert out["events"][0]["cohort"] == "dA (mixed)"
    assert not out["warnings"], out["warnings"]

    print("summary:", out["summary"])
    print("first event:", out["events"][0]["start"], out["events"][0]["title"])
    print("conflicts:", out["conflicts"])

    # empty cohort must raise cleanly
    try:
        service.preview_from_params(con, {**params, "cohort": ""}, assignee="u_me")
        raise SystemExit("expected RuntimeError for empty cohort")
    except RuntimeError:
        pass

    print("\nSERVICE CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
