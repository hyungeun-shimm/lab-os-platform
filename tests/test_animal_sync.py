"""Tests for the safe animal sync (modules/core/animal_sync.py · ROADMAP §B v1).

Run:  .venv/bin/python -m pytest tests/test_animal_sync.py -q

Proves the Phase-1 completion criteria:
  * deliberately broken rows are rejected + audited and never pollute the DB,
  * a mouse_id that vanishes from the source is soft-removed (not hard-deleted),
  * a present-but-rejected mouse is NOT soft-removed,
  * pre-sync snapshots are stored,
  * assigned_to (app-only) survives a re-sync,
  * strain alias + date normalisation are applied.
"""
import sqlite3

import pytest

from modules.core import animal_sync


# ---------------------------------------------------------------------------
# Fixtures — real schema against a throwaway temp DB
# ---------------------------------------------------------------------------

@pytest.fixture()
def con(tmp_path, monkeypatch):
    import server
    db = tmp_path / "t.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    server.migrate_db()
    with server.db_conn() as c:
        from modules.core.db_schema import init_eln_tables
        init_eln_tables(c)
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    yield c
    c.close()


def _row(mouse_id, **kw):
    base = {"mouse_id": mouse_id, "sex": "M", "strain": "WT", "dob": "2025-01-01"}
    base.update(kw)
    return base


def _alive(con):
    return {r["mouse_id"]: r for r in
            con.execute("SELECT * FROM animals WHERE COALESCE(removed,0)=0").fetchall()}


def _all(con):
    return {r["mouse_id"]: r for r in con.execute("SELECT * FROM animals").fetchall()}


# ---------------------------------------------------------------------------
# Pure-function tests (no DB)
# ---------------------------------------------------------------------------

def test_normalize_strain_alias():
    n = animal_sync.normalize_row({"mouse_id": "X1", "strain": "GCaMP6f x L7-Cre"})
    assert n["strain"] == "L7-Ai95D"
    n2 = animal_sync.normalize_row({"mouse_id": "X1", "strain": "gcamp6f × l7cre"})
    assert n2["strain"] == "L7-Ai95D"


def test_normalize_dates_and_sex():
    n = animal_sync.normalize_row({"mouse_id": "X1", "sex": "male", "dob": "6/3/2026"})
    assert n["sex"] == "M"
    assert n["dob"] == "2026-06-03"


def test_validate_rejects_bad_sex_and_dob():
    valid, rejected = animal_sync.validate_rows([
        _row("GOOD1"),
        _row("BAD1", sex="Male mouse"),     # invalid sex
        _row("BAD2", dob="not-a-date"),     # unparseable date
    ])
    ids_valid = {v["mouse_id"] for v in valid}
    ids_rej = {r["mouse_id"] for r in rejected}
    assert ids_valid == {"GOOD1"}
    assert ids_rej == {"BAD1", "BAD2"}


def test_validate_conflicting_duplicate_rejects_both():
    # same id, DIFFERENT data → ambiguous → reject all
    valid, rejected = animal_sync.validate_rows([_row("DUP"), _row("DUP", strain="KO")])
    assert valid == []
    assert {r["mouse_id"] for r in rejected} == {"DUP"}
    assert len(rejected) == 2
    assert any("conflicting" in e for e in rejected[0]["errors"])


def test_validate_identical_duplicate_merges_to_one():
    # same id, IDENTICAL data → harmless double-entry → collapse to one + warn
    valid, rejected = animal_sync.validate_rows([_row("DUP"), _row("DUP")])
    assert rejected == []
    assert len(valid) == 1
    assert any("merged" in w for w in valid[0]["_warnings"])


# ---------------------------------------------------------------------------
# run_sync integration tests
# ---------------------------------------------------------------------------

def test_initial_sync_inserts_and_audits(con):
    rep = animal_sync.run_sync(con, [_row("A1"), _row("A2"), _row("A3")], source="test")
    assert rep["counts"] == {"added": 3, "modified": 0, "removed": 0, "merged": 0,
                             "rejected": 0, "unchanged": 0, "total_source_rows": 3}
    assert set(_alive(con)) == {"A1", "A2", "A3"}
    n_added = con.execute(
        "SELECT COUNT(*) n FROM animal_audit WHERE change_type='added'").fetchone()["n"]
    assert n_added == 3
    # a snapshot of the (empty) pre-sync table was stored
    snap = con.execute("SELECT row_count FROM animal_snapshots WHERE sync_id=?",
                       (rep["sync_id"],)).fetchone()
    assert snap["row_count"] == 0


def test_resync_modifies_with_field_level_audit(con):
    animal_sync.run_sync(con, [_row("A1", condition="NR")], source="test")
    rep = animal_sync.run_sync(con, [_row("A1", condition="DT")], source="test")
    assert rep["counts"]["modified"] == 1
    assert rep["counts"]["added"] == 0
    assert _alive(con)["A1"]["condition"] == "DT"
    aud = con.execute(
        "SELECT field, old_value, new_value FROM animal_audit "
        "WHERE change_type='modified' AND mouse_id='A1'").fetchone()
    assert aud["field"] == "condition"
    assert aud["old_value"] == "NR" and aud["new_value"] == "DT"


def test_identical_resync_is_idempotent(con):
    animal_sync.run_sync(con, [_row("A1"), _row("A2")], source="test")
    rep = animal_sync.run_sync(con, [_row("A1"), _row("A2")], source="test")
    assert rep["counts"]["unchanged"] == 2
    assert rep["counts"]["modified"] == 0
    assert rep["counts"]["removed"] == 0


def test_broken_row_does_not_pollute_canonical(con):
    animal_sync.run_sync(con, [_row("A1", condition="NR")], source="test")
    # A1 is present but BROKEN this round (bad sex); A2 is a new broken row.
    rep = animal_sync.run_sync(con, [
        _row("A1", sex="???", condition="DT"),   # rejected → A1 keeps old value
        _row("A2", sex="X"),                     # rejected → never inserted
    ], source="test")
    assert rep["counts"]["rejected"] == 2
    alive = _alive(con)
    assert "A2" not in alive
    assert alive["A1"]["condition"] == "NR"      # not overwritten by the bad row
    assert "A1" in alive and alive["A1"]["removed"] == 0   # present-but-rejected ≠ removed


def test_vanished_mouse_is_soft_removed_not_deleted(con):
    animal_sync.run_sync(con, [_row("A1"), _row("A2")], source="test")
    rep = animal_sync.run_sync(con, [_row("A1")], source="test")   # A2 dropped
    assert rep["counts"]["removed"] == 1
    assert rep["removed"] == ["A2"]
    allrows = _all(con)
    assert "A2" in allrows                       # still in the table (not deleted)
    assert allrows["A2"]["removed"] == 1
    assert "A2" not in _alive(con)               # excluded from normal reads
    aud = con.execute(
        "SELECT COUNT(*) n FROM animal_audit WHERE change_type='removed' AND mouse_id='A2'"
    ).fetchone()["n"]
    assert aud == 1


def test_reappeared_mouse_is_unremoved(con):
    animal_sync.run_sync(con, [_row("A1"), _row("A2")], source="test")
    animal_sync.run_sync(con, [_row("A1")], source="test")          # A2 soft-removed
    rep = animal_sync.run_sync(con, [_row("A1"), _row("A2")], source="test")  # A2 back
    assert _alive(con).get("A2") is not None
    assert _all(con)["A2"]["removed"] == 0
    un = con.execute(
        "SELECT COUNT(*) n FROM animal_audit WHERE change_type='unremoved' AND mouse_id='A2'"
    ).fetchone()["n"]
    assert un == 1
    assert rep["counts"]["modified"] >= 1


def test_assigned_to_survives_resync(con):
    animal_sync.run_sync(con, [_row("A1")], source="test")
    con.execute("UPDATE animals SET assigned_to='user_x' WHERE mouse_id='A1'")
    con.commit()
    animal_sync.run_sync(con, [_row("A1", condition="DT")], source="test")
    assert _all(con)["A1"]["assigned_to"] == "user_x"


def test_existing_db_identical_duplicate_is_collapsed(con):
    # Simulate the real WT3712 case: two IDENTICAL rows already in the DB
    # (from the old destructive sync), one carrying an owner.
    con.execute("INSERT INTO animals (mouse_id,sex,strain,dob,condition,synced_at,assigned_to) "
                "VALUES ('WT3712','M','WT','2025-11-21','NR','t','u_owner')")
    con.execute("INSERT INTO animals (mouse_id,sex,strain,dob,condition,synced_at) "
                "VALUES ('WT3712','M','WT','2025-11-21','NR','t')")
    con.commit()
    assert con.execute("SELECT COUNT(*) n FROM animals WHERE mouse_id='WT3712'").fetchone()["n"] == 2

    rep = animal_sync.run_sync(
        con, [_row("WT3712", sex="M", strain="WT", dob="2025-11-21", condition="NR")],
        source="test")

    assert rep["counts"]["merged"] == 1
    alive = _alive(con)
    assert list(con.execute(
        "SELECT COUNT(*) n FROM animals WHERE mouse_id='WT3712' AND COALESCE(removed,0)=0"
    ).fetchone())[0] == 1                       # exactly one live WT3712 now
    assert alive["WT3712"]["assigned_to"] == "u_owner"   # the owned row was kept
    assert con.execute("SELECT COUNT(*) n FROM animals WHERE mouse_id='WT3712'").fetchone()["n"] == 2  # twin retained (removed), not deleted
    assert con.execute(
        "SELECT COUNT(*) n FROM animal_audit WHERE change_type='merged'").fetchone()["n"] == 1


def test_existing_db_conflicting_duplicate_left_untouched(con):
    con.execute("INSERT INTO animals (mouse_id,sex,strain,dob,synced_at) VALUES ('C1','M','WT','2025-01-01','t')")
    con.execute("INSERT INTO animals (mouse_id,sex,strain,dob,synced_at) VALUES ('C1','F','KO','2024-01-01','t')")
    con.commit()
    rep = animal_sync.run_sync(con, [_row("C1", sex="M", strain="WT", dob="2025-01-01")], source="test")
    assert rep["counts"]["merged"] == 0
    assert "C1" in rep["conflicts"]
    # both conflicting rows remain alive (ambiguous → human must resolve)
    assert con.execute(
        "SELECT COUNT(*) n FROM animals WHERE mouse_id='C1' AND COALESCE(removed,0)=0"
    ).fetchone()["n"] == 2


def test_snapshot_captures_pre_sync_state(con):
    animal_sync.run_sync(con, [_row("A1"), _row("A2")], source="test")
    rep = animal_sync.run_sync(con, [_row("A1"), _row("A2"), _row("A3")], source="test")
    snap = con.execute("SELECT row_count, payload FROM animal_snapshots WHERE sync_id=?",
                       (rep["sync_id"],)).fetchone()
    assert snap["row_count"] == 2          # snapshot taken BEFORE A3 was added
    import json
    ids = {r["mouse_id"] for r in json.loads(snap["payload"])}
    assert ids == {"A1", "A2"}
