from fastapi.testclient import TestClient

import server


def test_one_shot_211_compares_two_can0_readbacks_without_changing_inspector(monkeypatch):
    monkeypatch.setattr(server, "CANOE_AGENT_AUTOSTART_ON_ATTACK", False)
    monkeypatch.setattr(server, "DASHBOARD_ACCESS_TOKEN", "")
    client = TestClient(server.app)
    vehicle_id = "VHC-CANOE-211-TEST"
    before = server.registry.get(vehicle_id).body_snapshot()
    attack_url = "/api/vehicles/{}/body/canoe-attack".format(vehicle_id)
    response = client.post(attack_url, json={
        "attack": "door_fl_tamper", "attack_type": "CAN消息重放"})
    assert response.status_code == 200
    command = response.json()["command"]
    frame_url = attack_url + "/frame"
    frames = [
        {"can_id": 0x211, "data_hex": "2a3054618800016d",
         "received_at_ms": command["requested_at_ms"] + 1},
        {"can_id": 0x211, "data_hex": "2a3055618800016d",
         "received_at_ms": command["requested_at_ms"] + 21},
    ]
    assert client.post(frame_url, json={"frames": frames}).status_code == 401
    response = client.post(frame_url, json={"frames": frames}, headers={
        "Authorization": "Bearer {}".format(server.INGEST_TOKEN)})
    assert response.status_code == 200
    result = response.json()["command"]
    assert result["status"] == "detected"
    assert result["detected_frame"]["changed_bytes"] == [2]
    assert result["detected_frame"]["changed_functions"] == ["动力总成真实轮端扭矩"]
    assert result["detected_frame"]["baseline_hex"] == "2A 30 54 61 88 00 01 6D"
    assert server.registry.get(vehicle_id).body_sequence == 0
    assert before["frame_count"] == 0


def test_211_status_bit_reports_its_actual_function(monkeypatch):
    monkeypatch.setattr(server, "CANOE_AGENT_AUTOSTART_ON_ATTACK", False)
    monkeypatch.setattr(server, "DASHBOARD_ACCESS_TOKEN", "")
    client = TestClient(server.app)
    vehicle_id = "VHC-CANOE-211-STATUS"
    attack_url = "/api/vehicles/{}/body/canoe-attack".format(vehicle_id)
    command = client.post(attack_url, json={"attack_type": "CAN消息重放"}).json()["command"]
    frames = [
        {"can_id": 0x211, "data_hex": "2a3054618800016d",
         "received_at_ms": command["requested_at_ms"] + 1},
        {"can_id": 0x211, "data_hex": "2a3054618800016d",
         "received_at_ms": command["requested_at_ms"] + 11},
        {"can_id": 0x211, "data_hex": "2a3054608800016d",
         "received_at_ms": command["requested_at_ms"] + 21},
    ]
    # Byte 3 bit 0 is the ESP torque request active status.
    response = client.post(attack_url + "/frame", json={"frames": frames}, headers={
        "Authorization": "Bearer {}".format(server.INGEST_TOKEN)})
    assert response.json()["command"]["detected_frame"]["changed_functions"] == [
        "ESP扭矩请求激活"]
