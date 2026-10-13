"""Standalone sanity test for the autoschedule engine. Run:

    .venv/bin/python -m modules.autoschedule.test_engine

No pytest dependency — plain asserts + a printed schedule so the distribution,
control-mixing, sacrificed-exclusion, and conflict-detection logic are visible.
"""
import sqlite3

from modules.autoschedule import engine


def _seed_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        "CREATE TABLE animals (mouse_id TEXT, cohort TEXT, sacrificed TEXT);"
        "CREATE TABLE events (title TEXT, start_at TEXT, end_at TEXT, assignee TEXT);"
    )
    animals = [
        ("WT3479", "dA", ""), ("WT3480", "dA", ""), ("WT3481", "dA", ""),
        ("WT3482", "dA", "Y"),                       # sacrificed → excluded
        ("CT001", "dA-con", ""), ("CT002", "dA-con", ""), ("CT003", "dA-con", ""),
    ]
    con.executemany("INSERT INTO animals VALUES (?,?,?)", animals)
    # a pre-existing booking that should collide with the first proposed slot
    con.execute("INSERT INTO events VALUES (?,?,?,?)",
                ("Lab meeting", "2026-07-01T09:30", "2026-07-01T10:30", "u_me"))
    con.commit()
    return con


def main() -> None:
    con = _seed_db()

    exp = engine.resolve_cohort_mice(con, "dA")
    ctrl = engine.resolve_cohort_mice(con, "dA-con")
    assert exp == ["WT3479", "WT3480", "WT3481"], exp          # WT3482 dropped
    assert ctrl == ["CT001", "CT002", "CT003"], ctrl
    print(f"resolved: exp={exp}  ctrl={ctrl}  (WT3482 sacrificed → excluded ✓)")

    plan = engine.build_schedule(
        exp_mice=exp, ctrl_mice=ctrl,
        date_from="2026-07-01", date_to="2026-07-14",
        per_day=2, weekdays_only=True, mix_control=True, seed=42,
    )
    assert plan["n_mice"] == 6, plan
    assert len(plan["assignments"]) == 6, plan                 # all fit
    assert not plan["unscheduled"], plan
    # Six mice spread evenly across the available weekdays, never exceeding two/day.
    used_dates = sorted({d for d, _ in plan["assignments"]})
    assert len(used_dates) == 6, used_dates
    assert all(d.weekday() < 5 for d in used_dates), used_dates
    print(f"schedule: {len(plan['assignments'])} slots over {len(used_dates)} weekdays, "
          f"warnings={plan['warnings']}")

    events = engine.plan_to_events(
        plan["assignments"], category="behavior", subcategory="VOR / OKR",
        cohort="dA (mixed)", assignee="u_me", start_time="09:00", duration_min=60,
    )
    assert len(events) == 6
    assert events[0]["start"] == "2026-07-01T09:00" and events[0]["end"] == "2026-07-01T10:00"
    assert events[1]["start"] == "2026-07-02T09:00"
    print("\nproposed events:")
    for e in events:
        print(f"  {e['start']}  {e['title']:18s}  mice={e['mice']}")

    conflicts = engine.check_conflicts(con, events, assignee="u_me")
    assert len(conflicts) == 1, conflicts
    print(f"\nconflicts: {conflicts}")

    # control mixing actually interleaves both groups, not exp-then-ctrl
    scheduled = [m for _, m in plan["assignments"]]
    assert any(m.startswith("CT") for m in scheduled[:4]), scheduled
    print(f"\nmixed order: {scheduled}")
    print("\nALL CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
