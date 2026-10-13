import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
import server
from modules.core import auth, animal_sync
from modules.core.db_schema import init_eln_tables
from modules.eln.tracking import LAB_TZ


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "test.db")
    server.init_db()
    server.migrate_db()
    with server.db_conn() as con:
        init_eln_tables(con)
        for uid, role in (("owner", "admin"), ("member", "member"), ("other", "member")):
            con.execute("INSERT INTO eln_users(id,username,display_name,role,pin_hash,pin_salt,created_at,approved) "
                        "VALUES(?,?,?,?,?,?,?,1)", (uid, uid, uid.title(), role, "unused", "unused", "2026-01-01"))
        con.execute("INSERT INTO eln_projects(slug,title,kind,owner_id,created_at) "
                    "VALUES('project','Project','assigned','owner','2026-01-01')")
        for uid in ("owner", "member"):
            con.execute("INSERT INTO eln_project_members(project_slug,user_id,role,assigned_at) VALUES('project',?,'write','2026-01-01')", (uid,))
        con.execute("INSERT INTO team_members(id,name,eln_user_id,created_at) VALUES('team-member','Member','member','2026-01-01')")
        tokens = {}
        for uid in ("owner", "member", "other"):
            token, _ = auth.create_session(con, uid)
            auth.set_session_mode(con, token, "admin" if uid == "owner" else "member")
            tokens[uid] = token
    clients = {}
    for uid, token in tokens.items():
        client = TestClient(server.app)
        client.cookies.set(auth.SESSION_COOKIE, token)
        clients[uid] = client
    return clients


def create_event(client, **overrides):
    today = datetime.now(LAB_TZ).date().isoformat()
    body = {"title": "OKR", "category": "experiments", "subcategory": "Behavior", "subcategory2": "OKR",
            "project_slug": "project", "cohort": "I", "assignee": "team-member", "status": "in_progress",
            "start": today + "T09:00:00", "end": today + "T10:00:00", "mice": ["TEST01"],
            "recur": {"freq": "weekly", "until": "2027-01-01"}, "delay_reason": "Retest"}
    body.update(overrides)
    response = client.post("/api/events", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_mobile_partial_edit_preserves_tracking_fields(lab):
    event = create_event(lab["owner"])
    response = lab["member"].put("/api/events/" + event["id"], json={"title": "OKR retest"})
    assert response.status_code == 200
    result = response.json()
    for key in ("project_slug", "cohort", "assignee", "status", "recur", "subcategory2", "delay_reason"):
        assert result[key] == event[key]


def test_bad_event_time_does_not_change_saved_event(lab):
    event = create_event(lab["owner"])
    response = lab["member"].put("/api/events/" + event["id"], json={"end": "2020-01-01T08:00:00"})
    assert response.status_code == 400
    assert lab["member"].get("/api/events").json()["events"][0]["end"] == event["end"]


def test_schedule_note_creation_is_idempotent_and_private(lab):
    event = create_event(lab["owner"])
    path = "/api/eln/events/" + event["id"] + "/note"
    payload = {"project_slug": "project", "experiment_type": "behavior"}
    first = lab["member"].post(path, json=payload)
    assert first.status_code == 200, first.text
    note = first.json()["note"]
    assert note["payload"]["entries"][0]["mouse_id"] == "TEST01"
    assert note["status"] == "draft"
    assert lab["member"].post(path, json=payload).json()["note"]["id"] == note["id"]
    assert lab["other"].get("/api/eln/events/" + event["id"] + "/notes").json()["notes"] == []
    assert lab["other"].post(path, json=payload).status_code == 403
    assert lab["owner"].get("/api/eln/events/" + event["id"] + "/notes").json()["notes"][0]["id"] == note["id"]


def test_review_workflow_and_payload_preservation(lab):
    event = create_event(lab["owner"])
    note = lab["member"].post("/api/eln/events/" + event["id"] + "/note", json={"project_slug": "project", "experiment_type": "behavior"}).json()["note"]
    path = "/api/eln/notes/" + note["id"]
    updated = lab["member"].patch(path, json={"payload": {"entries": []}})
    assert updated.json()["note"]["payload"]["schedule_context"]["event_id"] == event["id"]
    assert lab["member"].post(path + "/submit").status_code == 200
    assert lab["member"].patch(path, json={"payload": {"entries": []}}).status_code == 403
    assert lab["owner"].get("/api/eln/dashboard").json()["users"][0]["submitted"] == 1
    assert lab["owner"].get("/api/eln/dashboard").json()["users"][0]["finalized"] == 0
    assert lab["owner"].get("/api/eln/workbench").json()["submitted"][0]["id"] == note["id"]
    assert lab["owner"].post(path + "/approve").status_code == 200
    assert lab["owner"].get("/api/eln/workbench").json()["submitted"] == []
    assert lab["owner"].post(path + "/return").json()["note"]["status"] == "draft"


def test_workbench_uses_lab_timezone_and_team_mapping(lab):
    day = datetime.now(LAB_TZ).date()
    local = datetime.combine(day, datetime.min.time(), LAB_TZ) + timedelta(hours=23)
    from datetime import timezone
    event = create_event(lab["owner"], start=local.astimezone(timezone.utc).isoformat(),
                         end=(local + timedelta(minutes=30)).astimezone(timezone.utc).isoformat(), recur=None)
    mine = lab["member"].get("/api/eln/workbench").json()
    assert [e["id"] for e in mine["today"]] == [event["id"]]
    assert lab["other"].get("/api/eln/workbench").json()["today"] == []


def test_sync_history_survives_reload_and_reports_bad_rows(lab):
    with server.db_conn() as con:
        report = animal_sync.run_sync(con, [{"mouse_id":"TEST01", "sex":"M", "dob":"bad"}], source="test")
    result = lab["owner"].get("/api/animals/sync-history").json()["reports"][0]
    assert result["sync_id"] == report["sync_id"]
    assert result["counts"]["rejected"] == 1
    assert result["rejected"][0]["mouse_id"] == "TEST01"
    assert lab["owner"].get("/api/animals/sync-status").json()["last_report"]["sync_id"] == result["sync_id"]
    assert TestClient(server.app).get("/api/animals/sync-history").status_code == 401


def test_failed_source_write_leaves_animal_cache_unchanged(lab, monkeypatch):
    with server.db_conn() as con:
        con.execute("INSERT INTO animals(mouse_id,sex,condition,synced_at) VALUES('TEST01','M','NR','2026-01-01')")
        con.execute("INSERT INTO animal_config(key,value) VALUES('sheet_id','test-sheet')")
    monkeypatch.setattr(server, "_write_animals_to_sheet", lambda rows: {"written":[], "errors":["offline"]}, raising=False)
    response = lab["owner"].put("/api/animals/TEST01", json={"condition":"DT"})
    assert response.status_code == 502
    with server.db_conn() as con:
        assert con.execute("SELECT condition FROM animals WHERE mouse_id='TEST01'").fetchone()[0] == "NR"


def test_auto_schedule_honors_daily_capacity():
    from modules.autoschedule.engine import build_schedule
    result = build_schedule(exp_mice=[f"TEST{i}" for i in range(7)], date_from="2026-10-14",
                            date_to="2026-10-15", per_day=2)
    assert len(result["assignments"]) == 4
    assert len(result["unscheduled"]) == 3
    assert result["warnings"]


def test_tracking_migrations_preserve_existing_links(lab):
    event = create_event(lab["owner"])
    result = lab["member"].post("/api/eln/events/" + event["id"] + "/note", json={"project_slug":"project", "experiment_type":"behavior"}).json()
    server.migrate_db()
    with server.db_conn() as con:
        init_eln_tables(con)
    saved = lab["member"].get("/api/eln/events/" + event["id"] + "/notes").json()
    assert saved["notes"][0]["id"] == result["note"]["id"]


def test_source_master_blocks_cache_only_removal(lab):
    with server.db_conn() as con:
        con.execute("INSERT INTO animals(mouse_id,sex,synced_at) VALUES('TEST01','M','2026-01-01')")
        con.execute("INSERT INTO animal_config(key,value) VALUES('sheet_id','test-sheet')")
    assert lab["owner"].delete("/api/animals/TEST01").status_code == 409
    assert lab["owner"].post("/api/animals/TEST01/archive", json={"reason":"sacrificed"}).status_code == 409
    assert lab["owner"].get("/api/animals").json()["animals"][0]["mouse_id"] == "TEST01"


def test_local_archive_preserves_owner(lab):
    with server.db_conn() as con:
        con.execute("INSERT INTO animals(mouse_id,sex,synced_at,assigned_to) VALUES('TEST01','M','2026-01-01','member')")
    result = lab["owner"].post("/api/animals/TEST01/archive", json={"reason":"sacrificed"})
    assert result.status_code == 200, result.text
    archive = lab["owner"].get("/api/animals/archive").json()["animals"]
    assert archive[0]["assigned_to"] == "member"
