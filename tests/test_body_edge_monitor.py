from fastapi.testclient import TestClient

import server
from body_blf_monitor import edge_monitor_for


def test_inspector_ingest_is_isolated_from_body_state(monkeypatch):
    monkeypatch.setattr(server, "DASHBOARD_ACCESS_TOKEN", "dashboard-only-test-token")
    client = TestClient(server.app)
    vehicle_id = "VHC-INSPECTOR-TEST"
    body = server.registry.get(vehicle_id).body_snapshot()
    url = "/api/vehicles/{}/body/edge-monitor".format(vehicle_id)
    frames = [{"can_id": 0x1D1, "data_hex": "80008000580002cc", "received_at_ms": 1000},
              {"can_id": 0x228, "data_hex": "0000000000000000", "received_at_ms": 1010}]
    assert client.post(url, json={"frames": frames}).status_code == 401
    response = client.post(url, json={"frames": frames},
                           headers={"Authorization": "Bearer {}".format(server.INGEST_TOKEN)})
    assert response.status_code == 200
    client.cookies.set("vcl_access", "dashboard-only-test-token")
    summary = client.get(url).json()
    assert [(frame["id"], frame["count"]) for frame in summary["frames"]] == [
        ("0x1D1", 1), ("0x228", 1)]
    assert summary["frames"][0]["signals"][0]["value"] == 0
    assert server.registry.get(vehicle_id).body_snapshot() == body
    assert edge_monitor_for("VHC-OTHER").snapshot()["frames"][0]["count"] == 0
