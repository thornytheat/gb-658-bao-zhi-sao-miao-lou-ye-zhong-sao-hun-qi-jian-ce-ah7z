from fastapi.testclient import TestClient
from app.main import app


def test_all_module_pages_accessible():
    client = TestClient(app)
    for path in ("/intake", "/issues", "/pages", "/rescans", "/mixed", "/batches"):
        response = client.get(path)
        assert response.status_code == 200 and "panel" in response.text

def test_full_sample_flow_has_business_results():
    client = TestClient(app)
    endpoints = ("/api/scans/import", "/api/issues/normalize", "/api/pages/check", "/api/rescans/evaluate", "/api/mixed/evaluate", "/api/reconcile")
    results = [client.post(path).json() for path in endpoints]
    assert results[0]["accepted"] == 2 and results[2]["missing"] == [3] and results[4]["decision"] == "MIXED_ISSUE"

def test_export_matches_page_evidence(tmp_path, monkeypatch):
    """原下载入口现已受控：必须指明批次并出示站点凭据，只导出本站行。"""
    import app.store as store
    import app.modules.m7_evidence.store as evidence_store

    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "e2e.sqlite3")
    monkeypatch.setattr(evidence_store.app_store, "DATABASE_PATH", tmp_path / "e2e.sqlite3")

    client = TestClient(app)
    admin = {"X-Admin-Token": "scanfolio-admin-dev"}
    content = (
        "issue,edition,page,file_name,sha256,phash,clarity,header_issue\n"
        "2026-11-01,01,004,own-004.tif," + "a" * 64 + ",0f0f,90,2026-11-01\n"
        "2026-11-02,02,007,other-007.tif," + "b" * 64 + ",0f1f,81,2026-11-02\n"
    )
    material = client.post(
        "/api/evidence/materials",
        json={
            "batch_id": "RUN-658",
            "content": content,
            "row_stations": {"2": "station-a", "3": "station-b"},
        },
        headers=admin,
    )
    assert material.status_code == 200
    credential = client.post(
        "/api/evidence/stations", json={"station_id": "station-a"}, headers=admin
    ).json()["credential"]

    open_entry = client.get("/api/export/evidence.csv")
    assert open_entry.status_code == 401

    response = client.get(
        "/api/export/evidence.csv",
        params={"batch_id": "RUN-658"},
        headers={"X-Station-Credential": credential},
    )
    assert response.status_code == 200 and "own-004" in response.text
    assert "other-007" not in response.text
