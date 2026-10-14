import asyncio
import sys
import types


# The five-domain cloud optionally imports OpenCV for cockpit image comparison.
# These tests exercise only body-state logic, so keep that unrelated optional
# dependency from blocking collection in lightweight test environments.
try:
    import cv2  # noqa: F401
except ModuleNotFoundError:
    sys.modules["cv2"] = types.ModuleType("cv2")

import server


def make_real_state(now_ms):
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    state = server.VehicleState("VHC-EVENT-TEST")
    state.domains["body"] = {
        "status": "online", "data_mode": "real", "Door_FL": False,
        "CentralLockState": "unlocked", "LowBeam": False,
        "TurnLeft": False, "TurnRight": False, "Hazard": False,
        "link": {"mcu_status": "online", "edge_status": "online", "upload_status": "online"},
    }
    state.body_last_seen_ms = now_ms
    return state


def test_event_is_queued_without_fabricating_telemetry(monkeypatch):
    monkeypatch.setattr(server, "now_ms", lambda: 100000)
    state = make_real_state(100000)
    command = server.queue_body_event_command(state, "door_fl")
    snapshot = state.body_snapshot()
    assert command["status"] == "pending"
    assert command["target"] is True
    assert snapshot["Door_FL"] is False
    assert snapshot["event_command"]["status"] == "pending"
    assert snapshot["frame_history"] == []


def test_only_matching_k3_can_ack_completes_command(monkeypatch, tmp_path):
    clock = {"now": 200000}
    monkeypatch.setattr(server, "now_ms", lambda: clock["now"])
    monkeypatch.setattr(server, "persist_body_frame", lambda *_: None)
    monkeypatch.setattr(server, "BODY_DATABASE_PATH", tmp_path / "body-event.db")
    server.init_body_database()
    state = make_real_state(clock["now"])
    command = server.queue_body_event_command(state, "low_beam")
    clock["now"] += 100
    body, _ = server.record_body_update(state, {
        "LowBeam": False, "captured_at_ms": clock["now"],
        "control_ack": {"sequence": command["sequence"], "result": "applied"},
    })
    assert body["event_command"]["status"] == "pending"
    clock["now"] += 100
    body, _ = server.record_body_update(state, {
        "LowBeam": True, "captured_at_ms": clock["now"],
        "control_ack": {"sequence": command["sequence"], "result": "applied"},
    })
    assert body["LowBeam"] is True
    assert body["event_command"]["status"] == "succeeded"

    clock["now"] += 5000
    body, _ = server.record_body_update(state, {
        "LowBeam": False, "captured_at_ms": clock["now"],
        "control_ack": {"sequence": command["sequence"], "result": "restored"},
    })
    assert body["LowBeam"] is False
    assert body["event_command"]["status"] == "restored"


def test_pending_command_times_out_without_real_readback(monkeypatch):
    clock = {"now": 300000}
    monkeypatch.setattr(server, "now_ms", lambda: clock["now"])
    state = make_real_state(clock["now"])
    server.queue_body_event_command(state, "hazard")
    clock["now"] += server.BODY_EVENT_TIMEOUT_MS
    snapshot = state.body_snapshot()
    assert snapshot["event_command"]["status"] == "timeout"
    assert snapshot["Hazard"] is False
