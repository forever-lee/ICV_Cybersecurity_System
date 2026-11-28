import asyncio
import sys
import types

import pytest

try:
    import cv2  # noqa: F401
except ModuleNotFoundError:
    sys.modules["cv2"] = types.ModuleType("cv2")

import server


def run_async(coroutine):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def make_state(timestamp=500000):
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    state = server.VehicleState("VHC-CANOE-TEST")
    state.body_canoe_attack_command = {
        "sequence": 42, "attack": "door_fl_tamper", "status": "injecting",
        "attack_type": "CAN消息重放",
        "requested_at_ms": timestamp - 100, "acknowledged_at_ms": timestamp - 50,
        "detected_at_ms": None, "completed_at_ms": None,
        "detected_frame": None, "error": "",
    }
    return state


def valid_tampered_frame():
    return {
        "sequence": 7, "can_id": "0x351", "dlc": 8,
        "data_hex": "01 00 00 00 00 00 05 06",
    }


def test_real_k3_readback_marks_command_detected():
    state = make_state()
    frame = valid_tampered_frame()
    alert = server.detect_canoe_body_tamper(
        state, frame, {"CentralLockState": "locked"}, 500000
    )
    assert alert["level"] == "critical"
    assert frame["is_intrusion"] is True
    assert frame["intrusion_type"] == "door_fl_tamper"
    assert state.body_canoe_attack_command["status"] == "detected"
    assert state.body_canoe_attack_command["detected_frame"]["can_id"] == "0x351"
    assert state.body_latest_intrusion["path"] == "CANoe → K3 → Jetson → 云端"
    assert state.body_latest_intrusion["attack_type"] == "CAN消息重放"
    assert alert["title"] == "检测到CAN消息重放"


def test_com_ack_alone_never_marks_attack_detected():
    state = make_state()
    assert state.body_canoe_attack_command["status"] == "injecting"
    assert state.body_canoe_attack_command["detected_frame"] is None


def test_wrong_frame_or_unlocked_state_is_not_detected():
    state = make_state()
    wrong_id = valid_tampered_frame()
    wrong_id["can_id"] = "0x352"
    assert server.detect_canoe_body_tamper(
        state, wrong_id, {"CentralLockState": "locked"}, 500000
    ) is None
    assert server.detect_canoe_body_tamper(
        state, valid_tampered_frame(), {"CentralLockState": "unlocked"}, 500000
    ) is None
    assert state.body_canoe_attack_command["status"] == "injecting"


def test_active_canoe_command_accepts_dbc_generated_protection_byte():
    state = make_state()
    frame = valid_tampered_frame()
    frame["data_hex"] = "01 00 00 00 00 00 05 98"
    alert = server.detect_canoe_body_tamper(
        state, frame, {"CentralLockState": "locked"}, 500000
    )
    assert alert["level"] == "critical"
    assert state.body_canoe_attack_command["status"] == "detected"


def test_queued_command_cannot_be_confirmed_by_unacknowledged_frame():
    state = make_state()
    state.body_canoe_attack_command["status"] = "queued"
    assert server.detect_canoe_body_tamper(
        state, valid_tampered_frame(), {"CentralLockState": "locked"}, 500000
    ) is None


def test_attack_times_out_after_cold_start_window(monkeypatch):
    state = make_state(500000)
    monkeypatch.setattr(server, "now_ms", lambda: 531000)
    snapshot = state.body_snapshot()
    assert snapshot["canoe_attack"]["status"] == "timeout"
    assert "30秒" in snapshot["canoe_attack"]["error"]


def test_all_document_attack_types_are_exposed_without_test_item_codes():
    assert server.BODY_CANOE_ATTACK_TYPES == (
        "CAN消息重放",
        "有线通信安全类工具",
        "获取车辆控制权限",
        "车辆通信协议攻击",
        "UDS",
        "有线通信协议及接口攻击",
    )
    assert all("PT_" not in label for label in server.BODY_CANOE_ATTACK_TYPES)
    state = make_state()
    assert state.body_snapshot()["canoe_attack_types"] == list(server.BODY_CANOE_ATTACK_TYPES)


def test_trigger_preserves_selected_attack_type(monkeypatch):
    class JsonRequest:
        async def json(self):
            return {"attack": "door_fl_tamper", "attack_type": "UDS"}

    monkeypatch.setattr(server, "registry", server.Registry())
    monkeypatch.setattr(server, "CANOE_AGENT_AUTOSTART_ON_ATTACK", False)
    result = run_async(server.trigger_body_canoe_attack("VHC-TYPE", JsonRequest()))
    assert result["command"]["attack_type"] == "UDS"
    assert result["body"]["canoe_attack"]["attack_type"] == "UDS"


def test_trigger_rejects_unlisted_attack_type(monkeypatch):
    class JsonRequest:
        async def json(self):
            return {"attack": "door_fl_tamper", "attack_type": "PT_CAN_002"}

    monkeypatch.setattr(server, "registry", server.Registry())
    with pytest.raises(server.HTTPException) as error:
        run_async(server.trigger_body_canoe_attack("VHC-TYPE", JsonRequest()))
    assert error.value.status_code == 400


def test_canoe_attack_sequence_fits_signed_int32(monkeypatch):
    monkeypatch.setattr(server, "now_ms", lambda: 0xFFFFFFFF)
    state = server.VehicleState("VHC-CANOE-INT32")
    assert state.body_canoe_attack_sequence == 0x7FFFFFFF


def test_body_history_uses_recent_memory_frames(monkeypatch):
    registry = server.Registry()
    monkeypatch.setattr(server, "registry", registry)
    state = registry.get("VHC-CANOE-TEST")
    for sequence in range(85):
        state.body_frame_history.appendleft({
            "sequence": sequence, "can_id": "0x351", "dlc": 8,
            "data_hex": "01 00 00 00 00 00 05 06",
            "signal_changes": {"Door_FL": True},
            "vehicle_time_ms": 500000 + sequence,
            "cloud_receive_time_ms": 500100 + sequence,
            "valid": True, "test": False,
            "is_intrusion": sequence == 84,
            "intrusion_type": "door_fl_tamper" if sequence == 84 else "",
        })
    result = run_async(server.body_history("VHC-CANOE-TEST", limit=1000))
    assert result["count"] == 80
    assert result["frames"][0]["sequence"] == 84
    assert result["frames"][0]["signals"] == {"Door_FL": True}
    assert result["frames"][0]["is_intrusion"] is True
    assert result["frames"][-1]["sequence"] == 5
