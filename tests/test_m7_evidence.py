import pytest
from fastapi.testclient import TestClient

import app.config as config
import app.modules.m7_evidence.auth as evidence_auth
import app.modules.m7_evidence.store as evidence_store
import app.store as store
from app.main import app
from app.modules.m7_evidence import materials, service

ADMIN = {"X-Admin-Token": "scanfolio-admin-dev"}
AUDIT = {"X-Audit-Token": "scanfolio-audit-dev"}

BATCH = (
    "issue,edition,page,file_name,sha256,phash,clarity,header_issue\n"
    "2026-11-01,01,001,a-001.tif," + "a" * 64 + ",0f0f,90,2026-11-01\n"
    "2026-11-01,01,004,a-004.tif," + "b" * 64 + ",0f1f,71,2026-11-01\n"
    "2026-11-02,01,002,b-002.tif," + "c" * 64 + ",0f2f,88,2026-11-02\n"
    "2026-11-03,01,003,c-003.tif," + "d" * 64 + ",0f3f,80,2026-11-03\n"
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "m7.sqlite3")
    monkeypatch.setattr(evidence_store.app_store, "DATABASE_PATH", tmp_path / "m7.sqlite3")
    evidence_auth._terminated_digests.clear()
    with TestClient(app) as test_client:
        yield test_client


def make_station(client, station_id, credential=None):
    response = client.post(
        "/api/evidence/stations", json={"station_id": station_id}, headers=ADMIN
    )
    assert response.status_code == 200, response.text
    return response.json()["credential"]


def register_batch(client, batch_id="B-2026-11", mapping=None):
    payload = {"batch_id": batch_id, "content": BATCH}
    if mapping is not None:
        payload["row_stations"] = mapping
    response = client.post("/api/evidence/materials", json=payload, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def default_mapping():
    return {"2": "station-a", "3": "station-a", "4": "station-b", "5": "station-c"}


def test_material_registration_keeps_original_rows(client):
    summary = register_batch(client, mapping=default_mapping())
    assert summary["row_count"] == 4
    assert summary["accepted"] == 4
    assert summary["stations"] == {
        "station-a": {"rows": 2, "first_line": 2, "last_line": 3},
        "station-b": {"rows": 1, "first_line": 4, "last_line": 4},
        "station-c": {"rows": 1, "first_line": 5, "last_line": 5},
    }


def test_station_delivery_is_clipped_to_own_rows(client):
    register_batch(client, mapping=default_mapping())
    credential_a = make_station(client, "station-a")
    response = client.post(
        "/api/evidence/stations/station-a/deliver",
        json={"batch_id": "B-2026-11"},
        headers={"X-Station-Credential": credential_a},
    )
    body = response.json()
    assert response.status_code == 200
    assert [row["line_no"] for row in body["rows"]] == [2, 3]
    assert all(row["station_id"] == "station-a" for row in body["rows"])
    assert "b-002" not in str(body) and "c-003" not in str(body)


def test_other_station_credential_cannot_take_rows(client):
    register_batch(client, mapping=default_mapping())
    credential_b = make_station(client, "station-b")
    response = client.post(
        "/api/evidence/stations/station-a/deliver",
        json={"batch_id": "B-2026-11"},
        headers={"X-Station-Credential": credential_b},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "STATION_FORBIDDEN"


def test_requesting_foreign_line_numbers_rejected(client):
    register_batch(client, mapping=default_mapping())
    credential_a = make_station(client, "station-a")
    response = client.post(
        "/api/evidence/stations/station-a/deliver",
        json={"batch_id": "B-2026-11", "line_numbers": [2, 4]},
        headers={"X-Station-Credential": credential_a},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "ROW_FORBIDDEN"


def test_missing_or_bad_station_credential_rejected(client):
    register_batch(client, mapping=default_mapping())
    missing = client.post(
        "/api/evidence/stations/station-a/deliver", json={"batch_id": "B-2026-11"}
    )
    assert missing.status_code == 401
    bad = client.post(
        "/api/evidence/stations/station-a/deliver",
        json={"batch_id": "B-2026-11"},
        headers={"X-Station-Credential": "nope"},
    )
    assert bad.status_code == 401


def test_audit_read_requires_purpose_and_logs_access(client):
    register_batch(client, mapping=default_mapping())
    no_purpose = client.post(
        "/api/evidence/audit/read",
        json={"batch_id": "B-2026-11", "request_id": "req-1"},
        headers=AUDIT,
    )
    assert no_purpose.status_code == 422
    assert no_purpose.json()["code"] == "PURPOSE_REQUIRED"

    response = client.post(
        "/api/evidence/audit/read",
        json={"batch_id": "B-2026-11", "purpose": "FULL_AUDIT", "request_id": "req-1"},
        headers=AUDIT,
    )
    assert response.status_code == 200
    assert response.json()["content"] == BATCH

    records = client.get("/api/evidence/access", params={"request_id": "req-1"}).json()[
        "records"
    ]
    assert len(records) == 1
    assert records[0]["purpose"] == "FULL_AUDIT"
    assert records[0]["status"] == "delivered"


def test_audit_read_requires_valid_audit_token(client):
    register_batch(client, mapping=default_mapping())
    response = client.post(
        "/api/evidence/audit/read",
        json={"batch_id": "B-2026-11", "purpose": "X", "request_id": "req-x"},
        headers={"X-Audit-Token": "wrong"},
    )
    assert response.status_code == 401
    assert client.get("/api/evidence/access").json()["records"] == []


def test_audit_retry_does_not_duplicate_access_log(client):
    register_batch(client, mapping=default_mapping())
    payload = {"batch_id": "B-2026-11", "purpose": "DOUBLE_CHECK", "request_id": "req-retry"}
    first = client.post("/api/evidence/audit/read", json=payload, headers=AUDIT)
    second = client.post("/api/evidence/audit/read", json=payload, headers=AUDIT)
    assert first.status_code == second.status_code == 200
    assert second.json()["content"] == BATCH
    records = client.get("/api/evidence/access").json()["records"]
    assert len(records) == 1


def test_audit_request_id_cannot_be_reused_for_another_batch(client):
    register_batch(client, "B-1", mapping=default_mapping())
    register_batch(client, "B-2", mapping=default_mapping())
    payload = {"batch_id": "B-1", "purpose": "CHECK", "request_id": "req-shared"}
    assert client.post("/api/evidence/audit/read", json=payload, headers=AUDIT).status_code == 200
    conflict = client.post(
        "/api/evidence/audit/read",
        json={"batch_id": "B-2", "purpose": "CHECK", "request_id": "req-shared"},
        headers=AUDIT,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "REQUEST_CONFLICT"


def test_audit_log_failure_returns_no_content(client, monkeypatch):
    register_batch(client, mapping=default_mapping())

    def boom(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(
        "app.modules.m7_evidence.access_log.record_access", boom
    )
    with pytest.raises(RuntimeError):
        client.post(
            "/api/evidence/audit/read",
            json={"batch_id": "B-2026-11", "purpose": "CHECK", "request_id": "req-fail"},
            headers=AUDIT,
        )
    assert client.get("/api/evidence/access").json()["records"] == []


def test_audit_interruption_resumes_same_request(client, monkeypatch):
    register_batch(client, mapping=default_mapping())
    payload = {"batch_id": "B-2026-11", "purpose": "RESUME", "request_id": "req-resume"}

    original_mark = __import__(
        "app.modules.m7_evidence.access_log", fromlist=["mark_delivered"]
    ).mark_delivered

    monkeypatch.setattr(
        "app.modules.m7_evidence.access_log.mark_delivered",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("interrupted")),
    )
    with pytest.raises(RuntimeError):
        client.post("/api/evidence/audit/read", json=payload, headers=AUDIT)

    pending = client.get("/api/evidence/access", params={"request_id": "req-resume"}).json()[
        "records"
    ]
    assert len(pending) == 1 and pending[0]["status"] == "pending"

    monkeypatch.setattr(
        "app.modules.m7_evidence.access_log.mark_delivered", original_mark
    )
    resumed = client.post("/api/evidence/audit/read", json=payload, headers=AUDIT)
    assert resumed.status_code == 200
    assert resumed.json()["content"] == BATCH
    records = client.get("/api/evidence/access", params={"request_id": "req-resume"}).json()[
        "records"
    ]
    assert len(records) == 1 and records[0]["status"] == "delivered"


def test_terminated_credential_immediately_invalid(client):
    register_batch(client, mapping=default_mapping())
    credential_a = make_station(client, "station-a")
    terminated = client.post(
        "/api/evidence/stations/station-a/terminate", headers=ADMIN
    )
    assert terminated.status_code == 200
    response = client.post(
        "/api/evidence/stations/station-a/deliver",
        json={"batch_id": "B-2026-11"},
        headers={"X-Station-Credential": credential_a},
    )
    assert response.status_code == 410
    assert response.json()["code"] == "STATION_TERMINATED"


def test_termination_wins_against_concurrent_delivery(client, monkeypatch):
    register_batch(client, mapping=default_mapping())
    credential_a = make_station(client, "station-a")

    original_ensure_active = evidence_auth.ensure_active
    state = {"terminated": False}

    def terminate_then_check(db, station_id):
        if not state["terminated"]:
            state["terminated"] = True
            service.terminate_station("station-a")
        return original_ensure_active(db, station_id)

    monkeypatch.setattr(evidence_auth, "ensure_active", terminate_then_check)
    response = client.post(
        "/api/evidence/stations/station-a/deliver",
        json={"batch_id": "B-2026-11"},
        headers={"X-Station-Credential": credential_a},
    )
    assert response.status_code == 410


def test_legacy_export_requires_credentials_and_clips(client):
    register_batch(client, mapping=default_mapping())
    credential_a = make_station(client, "station-a")
    open_request = client.get("/api/export/evidence.csv")
    assert open_request.status_code == 401

    clipped = client.get(
        "/api/export/evidence.csv",
        params={"batch_id": "B-2026-11"},
        headers={"X-Station-Credential": credential_a},
    )
    assert clipped.status_code == 200
    assert "a-001" in clipped.text and "b-002" not in clipped.text


def test_material_registration_is_idempotent(client):
    first = register_batch(client, "B-repeat", mapping=default_mapping())
    second = register_batch(client, "B-repeat", mapping=default_mapping())
    assert first == second
    assert len(client.get("/api/evidence/materials").json()["materials"]) == 1


def test_scope_readback_shows_station_delivery_ranges(client):
    register_batch(client, mapping=default_mapping())
    make_station(client, "station-a")
    make_station(client, "station-b")
    make_station(client, "station-c")
    stations = client.get("/api/evidence/stations").json()["stations"]
    by_id = {item["station_id"]: item for item in stations}
    assert by_id["station-a"]["batches"]["B-2026-11"]["rows"] == 2
    assert by_id["station-b"]["batches"]["B-2026-11"]["rows"] == 1
    assert by_id["station-c"]["status"] == "active"


def test_station_scope_endpoint_returns_only_own_batches(client):
    register_batch(client, mapping=default_mapping())
    credential_b = make_station(client, "station-b")
    response = client.get(
        "/api/evidence/stations/station-b/scope",
        headers={"X-Station-Credential": credential_b},
    )
    assert response.status_code == 200
    assert response.json()["batches"]["B-2026-11"]["rows"] == 1

    forbidden = client.get(
        "/api/evidence/stations/station-a/scope",
        headers={"X-Station-Credential": credential_b},
    )
    assert forbidden.status_code == 403


def test_materials_parse_preserves_raw_rows(client):
    accepted, quarantined = materials.parse_material(BATCH)
    assert accepted[0]["line_no"] == 2
    assert accepted[0]["business_key"] == "2026-11-01|01|001"
    assert accepted[0]["fields"]["file_name"] == "a-001.tif"
    assert accepted[0]["raw_line"].startswith("2026-11-01,01,001,a-001.tif")


def test_delivery_page_is_readable(client):
    response = client.get("/evidence")
    assert response.status_code == 200 and "panel" in response.text


def test_station_cannot_probe_a_batch_with_no_own_rows(client):
    register_batch(client, "B-1", mapping={"2": "station-a", "3": "station-a", "4": "station-a", "5": "station-a"})
    credential_b = make_station(client, "station-b")
    response = client.post(
        "/api/evidence/stations/station-b/deliver",
        json={"batch_id": "B-1"},
        headers={"X-Station-Credential": credential_b},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "STATION_FORBIDDEN"


def test_station_requests_specific_owned_lines(client):
    register_batch(client, mapping=default_mapping())
    credential_a = make_station(client, "station-a")
    response = client.post(
        "/api/evidence/stations/station-a/deliver",
        json={"batch_id": "B-2026-11", "line_numbers": [3]},
        headers={"X-Station-Credential": credential_a},
    )
    assert response.status_code == 200
    assert [row["line_no"] for row in response.json()["rows"]] == [3]


def test_terminated_station_can_rehire_with_new_credential(client):
    register_batch(client, mapping=default_mapping())
    old_credential = make_station(client, "station-a")
    client.post("/api/evidence/stations/station-a/terminate", headers=ADMIN)
    new_credential = make_station(client, "station-a")
    assert new_credential != old_credential

    old = client.post(
        "/api/evidence/stations/station-a/deliver",
        json={"batch_id": "B-2026-11"},
        headers={"X-Station-Credential": old_credential},
    )
    assert old.status_code in (401, 410)

    fresh = client.post(
        "/api/evidence/stations/station-a/deliver",
        json={"batch_id": "B-2026-11"},
        headers={"X-Station-Credential": new_credential},
    )
    assert fresh.status_code == 200


def test_delivery_is_logged_with_station_scope(client):
    register_batch(client, mapping=default_mapping())
    credential_a = make_station(client, "station-a")
    client.post(
        "/api/evidence/stations/station-a/deliver",
        json={"batch_id": "B-2026-11"},
        headers={"X-Station-Credential": credential_a, "Idempotency-Key": "deliver-1"},
    )
    records = client.get("/api/evidence/access", params={"request_id": "deliver-1"}).json()[
        "records"
    ]
    assert len(records) == 1
    assert records[0]["actor_kind"] == "station"
    assert records[0]["scope"]["station_id"] == "station-a"


def test_station_delivery_retry_is_idempotent(client):
    register_batch(client, mapping=default_mapping())
    credential_a = make_station(client, "station-a")
    headers = {"X-Station-Credential": credential_a, "Idempotency-Key": "deliver-retry"}
    first = client.post(
        "/api/evidence/stations/station-a/deliver", json={"batch_id": "B-2026-11"}, headers=headers
    )
    second = client.post(
        "/api/evidence/stations/station-a/deliver", json={"batch_id": "B-2026-11"}, headers=headers
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["rows"] == second.json()["rows"]
    records = client.get("/api/evidence/access", params={"request_id": "deliver-retry"}).json()[
        "records"
    ]
    assert len(records) == 1


def test_batch_requires_registration(client):
    response = client.post(
        "/api/evidence/stations/station-a/deliver",
        json={"batch_id": "missing"},
        headers={"X-Station-Credential": "whatever"},
    )
    assert response.status_code == 401


def test_admin_token_required_to_register(client):
    response = client.post(
        "/api/evidence/materials",
        json={"batch_id": "B", "content": BATCH},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "MISSING_CREDENTIAL"


def test_conflicting_registration_rejected(client):
    register_batch(client, "B-conflict")
    response = client.post(
        "/api/evidence/materials",
        json={"batch_id": "B-conflict", "content": BATCH + "2026-11-04,01,001,z.tif," + "e" * 64 + ",0f00,70,2026-11-04\n"},
        headers=ADMIN,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "BATCH_CONFLICT"


def test_access_endpoint_can_filter_by_batch(client):
    register_batch(client, mapping=default_mapping())
    payload = {"batch_id": "B-2026-11", "purpose": "FILTER", "request_id": "req-filter"}
    client.post("/api/evidence/audit/read", json=payload, headers=AUDIT)
    records = client.get("/api/evidence/access", params={"batch_id": "B-2026-11"}).json()[
        "records"
    ]
    assert all(record["batch_id"] == "B-2026-11" for record in records)


def test_threaded_termination_beats_concurrent_delivery(client, monkeypatch):
    """取件已在进行、尚未完成交付时解除合作：以解除为准，正文不下发。"""
    import threading

    register_batch(client, mapping=default_mapping())
    credential_a = make_station(client, "station-a")

    original_finalize = evidence_auth.finalize_delivery
    in_flight = threading.Event()
    proceed = threading.Event()

    def held_finalize(db, station_id, build):
        in_flight.set()  # 取件已完成读取与记账，正等待交付闸门
        proceed.wait(timeout=5)
        return original_finalize(db, station_id, build)

    monkeypatch.setattr(evidence_auth, "finalize_delivery", held_finalize)
    outcome = {}

    def deliver():
        try:
            service.station_delivery("B-2026-11", "station-a", credential_a)
            outcome["delivery"] = "delivered"
        except Exception as exc:  # noqa: BLE001
            outcome["delivery"] = getattr(exc, "code", str(exc))

    delivery_thread = threading.Thread(target=deliver)
    delivery_thread.start()
    assert in_flight.wait(timeout=5)

    # 取件悬停在交付闸门前，此时完成解除合作。
    service.terminate_station("station-a")
    proceed.set()
    delivery_thread.join(timeout=5)

    assert outcome["delivery"] == "STATION_TERMINATED"
    logs = client.get("/api/evidence/access").json()["records"]
    assert len(logs) == 1 and logs[0]["status"] == "denied"
