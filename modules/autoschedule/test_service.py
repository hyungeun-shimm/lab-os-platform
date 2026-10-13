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
        "CREATE TABLE team_members (id TEXT, name TEXT);"
        """
        CREATE TABLE events (
          id TEXT, title TEXT, category TEXT, subcategory TEXT, subcategory2 TEXT,
          cohort TEXT, project_slug TEXT, status TEXT, start_at TEXT, end_at TEXT,
          all_day INTEGER, notes TEXT, mice TEXT, assignee TEXT, location TEXT, rig TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO animals VALUES (?,?,?)",
        [("WT3479", "dA", ""), ("WT3480", "dA", ""), ("WT3481", "dA", ""),
         ("CT001", "dA-con", ""), ("CT002", "dA-con", "")],
    )
    con.executemany(
        "INSERT INTO team_members VALUES (?,?)",
        [("u_me", "Test User"), ("8486ade0eb", "Your_Name")],
    )
    con.executemany(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("meet1", "Lab meeting", "deadlines", "", "", "", "", "planned",
             "2026-07-01T09:30", "2026-07-01T10:30", 0, "", "[]", "u_me", "", ""),
            ("okr1", "VOR / OKR · WT3479", "behavior", "VOR / OKR", "", "dA", "",
             "planned", "2026-07-02T09:00", "2026-07-02T10:00", 0, "", "[\"WT3479\"]", "", "", ""),
            ("okr2", "VOR / OKR · WT3480", "behavior", "VOR / OKR", "", "dA", "",
             "planned", "2026-07-03T09:00", "2026-07-03T10:00", 0, "", "[\"WT3480\"]", "", "", ""),
            ("surg1", "Surgery · WT3481", "surgery", "Eye surgery", "", "dA", "",
             "planned", "2026-07-03T11:00", "2026-07-03T12:00", 0, "", "[\"WT3481\"]", "", "", ""),
            ("okr3", "VOR / OKR · CT001", "behavior", "VOR / OKR", "", "dA-con", "",
             "planned", "2026-08-01T09:00", "2026-08-01T10:00", 0, "", "[\"CT001\"]", "", "", ""),
            ("okrj1", "VOR / OKR · WT3479", "behavior", "VOR / OKR", "", "dA", "",
             "planned", "2026-06-23T09:00", "2026-06-23T10:00", 0, "", "[\"WT3479\"]", "", "", ""),
            ("okrj2", "VOR / OKR · WT3480", "behavior", "VOR / OKR", "", "dA", "",
             "planned", "2026-06-28T09:00", "2026-06-28T10:00", 0, "", "[\"WT3480\"]", "", "", ""),
        ],
    )
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
    # Test assignee resolution: name → ID
    assert all(e["assignee"] == "u_me" for e in out["events"]), "assignee not set from function param"

    # Test assignee from params (name resolution)
    params_with_assignee = {**params, "assignee": "Your_Name"}
    out_with_assignee = service.preview_from_params(con, params_with_assignee)
    assert all(e["assignee"] == "8486ade0eb" for e in out_with_assignee["events"]), \
        f"assignee name not resolved to ID: {out_with_assignee['events'][0]['assignee']}"

    print("summary:", out["summary"])
    print("first event:", out["events"][0]["start"], out["events"][0]["title"])
    print("conflicts:", out["conflicts"])

    # empty cohort must raise cleanly
    try:
        service.preview_from_params(con, {**params, "cohort": ""}, assignee="u_me")
        raise SystemExit("expected RuntimeError for empty cohort")
    except RuntimeError:
        pass

    delete_out = service.preview_from_params(con, {
        "action": "delete", "category": "behavior", "subcategory": "OKR",
        "query": "OKR", "date_from": "2026-07-01", "date_to": "2026-07-10",
    })
    assert delete_out["action"] == "delete"
    assert delete_out["delete_ids"] == ["okr1", "okr2"], delete_out["delete_ids"]
    assert delete_out["summary"]["matched"] == 2, delete_out["summary"]

    plain_delete = service.preview(
        con,
        "can you cancel the OKR schedules for this week (from 6/23 to 6/28)?",
        today="2026-06-24",
    )
    assert plain_delete["action"] == "delete"
    assert plain_delete["params"]["date_from"] == "2026-06-23", plain_delete["params"]
    assert plain_delete["params"]["date_to"] == "2026-06-28", plain_delete["params"]
    assert plain_delete["delete_ids"] == ["okrj1", "okrj2"], plain_delete["delete_ids"]

    print("\nSERVICE CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
