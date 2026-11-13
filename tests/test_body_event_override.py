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


def test_latest_overview_pins_event_can_id_then_returns_to_periodic(monkeypatch):
    clock = {"now": 400000}
    monkeypatch.setattr(server, "now_ms", lambda: clock["now"])
    state = make_real_state(clock["now"])
    command = server.queue_body_event_command(state, "door_fl")
    command = state.body_event_command
    command["status"] = "succeeded"
    command["completed_at_ms"] = clock["now"] + 100
    event_frame = {
        "can_id": "0x351", "frame_name": "BodyDoorStatus",
        "cloud_receive_time_ms": clock["now"] + 100,
    }
    periodic_frame = {
        "can_id": "0x180", "frame_name": "BodyStatus",
        "cloud_receive_time_ms": clock["now"] + 200,
    }
    state.body_frame_history.appendleft(event_frame)
    state.body_frame_history.appendleft(periodic_frame)

    clock["now"] += 1000
    snapshot = state.body_snapshot()
    assert snapshot["latest_frame"]["can_id"] == "0x351"

    clock["now"] = command["completed_at_ms"] + 5000
    snapshot = state.body_snapshot()
    assert snapshot["latest_frame"]["can_id"] == "0x180"


def test_event_actions_map_to_their_real_can_group():
    assert server.BODY_EVENT_CAN_IDS["door_fr"] == "0x351"
    assert server.BODY_EVENT_CAN_IDS["central_lock"] == "0x352"
    assert server.BODY_EVENT_CAN_IDS["hazard"] == "0x353"


def test_k3_ack_drives_event_overview_when_udp_raw_slot_is_only_0x180(monkeypatch):
    clock = {"now": 500000}
    monkeypatch.setattr(server, "now_ms", lambda: clock["now"])
    state = make_real_state(clock["now"])
    server.queue_body_event_command(state, "low_beam")
    command = state.body_event_command
    command["status"] = "succeeded"
    command["completed_at_ms"] = clock["now"] + 100
    state.domains["body"]["LowBeam"] = True
    state.body_frame_history.appendleft({
        "sequence": 88,
        "can_id": "0x180",
        "frame_name": "BodyStatus",
        "cycle_ms": 100,
        "send_type": "K3周期报文",
        "data_hex": "00 0F 01 01 00 00 58 59",
        "cloud_receive_time_ms": clock["now"] + 200,
    })

    clock["now"] += 1000
    snapshot = state.body_snapshot()
    assert snapshot["latest_frame"]["can_id"] == "0x353"
    assert snapshot["latest_frame"]["send_type"] == "Web事件触发/CAN回读"
    assert snapshot["latest_frame"]["overview_only"] is True
    assert snapshot["frame_history"][0]["can_id"] == "0x180"

    clock["now"] = command["completed_at_ms"] + 5000
    snapshot = state.body_snapshot()
    assert snapshot["latest_frame"]["can_id"] == "0x180"


def test_pending_door_and_lock_show_event_group_without_claiming_success(monkeypatch):
    clock = {"now": 700000}
    monkeypatch.setattr(server, "now_ms", lambda: clock["now"])
    state = make_real_state(clock["now"])
    state.body_frame_history.appendleft({
        "sequence": 99,
        "can_id": "0x180",
        "frame_name": "BodyStatus",
        "cycle_ms": 100,
        "simulation_step": 5,
        "cloud_receive_time_ms": clock["now"],
    })
    for action, expected_id in (("door_fl", "0x351"), ("central_lock", "0x352")):
        server.queue_body_event_command(state, action)
        snapshot = state.body_snapshot()
        assert snapshot["latest_frame"]["can_id"] == expected_id
        assert snapshot["latest_frame"]["event_pending"] is True
        assert snapshot["latest_frame"]["send_type"] == "Web事件待K3确认"
        assert snapshot["frame_history"][0]["can_id"] == "0x180"
        state.body_event_command["status"] = "failed"
        assert state.body_snapshot()["latest_frame"]["can_id"] == "0x353"


def test_periodic_overview_maps_k3_step_without_changing_raw_history(monkeypatch):
    clock = {"now": 600000}
    monkeypatch.setattr(server, "now_ms", lambda: clock["now"])
    state = make_real_state(clock["now"])

    for step, expected_id in ((1, "0x351"), (5, "0x353"), (0, "0x352")):
        state.body_frame_history.clear()
        state.body_frame_history.appendleft({
            "sequence": 90 + step,
            "can_id": "0x180",
            "frame_name": "BodyStatus",
            "cycle_ms": 100,
            "send_type": "K3周期报文",
            "data_hex": "00 0F 00 01 00 00 58 68",
            "simulation_step": step,
            "cloud_receive_time_ms": clock["now"],
        })
        snapshot = state.body_snapshot()
        assert snapshot["latest_frame"]["can_id"] == expected_id
        assert snapshot["latest_frame"]["raw_can_id"] == "0x180"
        assert snapshot["latest_frame"]["overview_only"] is True
        assert snapshot["frame_history"][0]["can_id"] == "0x180"
