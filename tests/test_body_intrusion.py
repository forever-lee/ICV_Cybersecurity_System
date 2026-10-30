import asyncio
import sqlite3
import sys
import types

try:
    import cv2  # noqa: F401
except ModuleNotFoundError:
    sys.modules["cv2"] = types.ModuleType("cv2")

import server


def make_state(timestamp=500000):
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    state = server.VehicleState("VHC-CANOE-TEST")
    state.body_canoe_attack_command = {
        "sequence": 42, "attack": "door_fl_tamper", "status": "injecting",
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


def test_canoe_attack_sequence_fits_signed_int32(monkeypatch):
    monkeypatch.setattr(server, "now_ms", lambda: 0xFFFFFFFF)
    state = server.VehicleState("VHC-CANOE-INT32")
    assert state.body_canoe_attack_sequence == 0x7FFFFFFF


def test_existing_body_database_is_migrated_for_intrusion_fields(monkeypatch, tmp_path):
    database = tmp_path / "legacy-body.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE body_frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT, vehicle_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, can_id TEXT NOT NULL, dlc INTEGER NOT NULL,
                data_hex TEXT NOT NULL, signals_json TEXT NOT NULL,
                captured_at_ms INTEGER NOT NULL, received_at_ms INTEGER NOT NULL,
                valid INTEGER NOT NULL, invalid_reason TEXT NOT NULL,
                device_id TEXT NOT NULL, interface_path TEXT NOT NULL,
                source TEXT NOT NULL, is_test INTEGER NOT NULL
            )"""
        )
    monkeypatch.setattr(server, "BODY_DATABASE_PATH", database)
    server.init_body_database()
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(body_frames)")}
    assert {"is_intrusion", "intrusion_type", "intrusion_reason"} <= columns
