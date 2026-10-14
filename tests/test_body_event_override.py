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
        "status": "online",
        "data_mode": "real",
        "can_status": "normal",
        "Door_FL": False,
        "Door_FR": False,
        "Door_RL": False,
        "Door_RR": False,
        "CentralLockState": "unlocked",
        "LowBeam": False,
        "HighBeam": False,
        "TurnLeft": False,
        "TurnRight": False,
        "Hazard": False,
        "updated_at_ms": now_ms,
        "captured_at_ms": now_ms,
        "link": {
            "mcu_status": "online",
            "edge_status": "online",
            "upload_status": "online",
        },
    }
    state.body_last_seen_ms = now_ms
    return state


def test_web_event_temporarily_overrides_real_periodic_state(monkeypatch, tmp_path):
    clock = {"now": 100000}
    monkeypatch.setattr(server, "now_ms", lambda: clock["now"])
    monkeypatch.setattr(server, "persist_body_frame", lambda *_: None)
    monkeypatch.setattr(server, "BODY_DATABASE_PATH", tmp_path / "body-event-test.db")
    server.init_body_database()
    state = make_real_state(clock["now"])

    body, frames, label, value = server.record_body_event_override(state, "door_fl")

    assert label == "左前门"
    assert value is True
    assert body["Door_FL"] is True
    assert body["data_mode"] == "event"
    assert body["event_override"]["active"] is True
    assert body["event_override"]["remaining_ms"] == 5000
    assert frames[0]["send_type"] == "Web事件触发"

    # Real periodic data keeps updating underneath the temporary Web event.
    clock["now"] = 104999
    periodic_snapshot, _ = server.record_body_update(state, {
        "Door_FL": False,
        "LowBeam": True,
        "captured_at_ms": clock["now"],
        "link": {
            "mcu_status": "online",
            "edge_status": "online",
            "upload_status": "online",
            "udp_received_at_ms": clock["now"],
        },
    })
    assert periodic_snapshot["Door_FL"] is True
    assert state.domains["body"]["LowBeam"] is True

    # At exactly five seconds the overlay is removed and the latest real data is shown.
    clock["now"] = 105000
    restored = state.body_snapshot()
    assert restored["Door_FL"] is False
    assert restored["LowBeam"] is True
    assert restored["data_mode"] == "real"
    assert restored["event_override"]["active"] is False


def test_each_new_event_extends_window_and_keeps_prior_event_values(monkeypatch):
    clock = {"now": 200000}
    monkeypatch.setattr(server, "now_ms", lambda: clock["now"])
    monkeypatch.setattr(server, "persist_body_frame", lambda *_: None)
    state = make_real_state(clock["now"])

    server.record_body_event_override(state, "door_fr")
    clock["now"] += 4000
    body, _, _, _ = server.record_body_event_override(state, "low_beam")

    assert body["Door_FR"] is True
    assert body["LowBeam"] is True
    assert body["event_override"]["expires_at_ms"] == 209000

    clock["now"] = 208999
    assert state.body_snapshot()["event_override"]["active"] is True
    clock["now"] = 209000
    assert state.body_snapshot()["event_override"]["active"] is False


def test_hazard_event_synchronizes_both_turn_lamps(monkeypatch):
    monkeypatch.setattr(server, "now_ms", lambda: 300000)
    monkeypatch.setattr(server, "persist_body_frame", lambda *_: None)
    state = make_real_state(300000)

    body, frames, _, value = server.record_body_event_override(state, "hazard")

    assert value is True
    assert body["Hazard"] is True
    assert body["TurnLeft"] is True
    assert body["TurnRight"] is True
    assert frames[0]["signal_changes"] == {
        "TurnLeft": True,
        "TurnRight": True,
        "Hazard": True,
    }
