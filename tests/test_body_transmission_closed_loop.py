import asyncio
import sys
import types

try:
    import cv2  # noqa: F401
except ModuleNotFoundError:
    sys.modules["cv2"] = types.ModuleType("cv2")

import server


def make_state():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    state = server.VehicleState("VHC-CONTROL-TEST")
    state.domains["body"] = {
        "status": "online",
        "transmission_enabled": True,
        "link": {},
    }
    state.body_control = {
        "sequence": 123,
        "action": "stop",
        "desired_state": "stopped",
        "status": "delivering",
        "requested_at_ms": 1000,
        "dispatched_at_ms": 1100,
        "acknowledged_at_ms": None,
        "completed_at_ms": None,
        "actual_state": None,
        "error": "",
    }
    return state


def test_udp_send_alone_does_not_complete_cloud_command():
    state = make_state()
    assert state.body_control["status"] == "delivering"
    assert state.body_control["acknowledged_at_ms"] is None


def test_only_matching_k3_ack_and_actual_state_completes_command():
    state = make_state()
    body = {"transmission_enabled": True}
    server.apply_body_transmission_ack(state, {
        "transmission_ack": {
            "sequence": 123, "action": "stop", "result": "applied",
        },
    }, body, 1200)
    assert state.body_control["status"] == "error"

    state = make_state()
    body = {"transmission_enabled": False}
    server.apply_body_transmission_ack(state, {
        "transmission_ack": {
            "sequence": 122, "action": "stop", "result": "applied",
        },
    }, body, 1200)
    assert state.body_control["status"] == "delivering"

    server.apply_body_transmission_ack(state, {
        "transmission_ack": {
            "sequence": 123, "action": "stop", "result": "applied",
        },
    }, body, 1300)
    assert state.body_control["status"] == "applied"
    assert state.body_control["actual_state"] == "stopped"


def test_command_expires_without_k3_ack(monkeypatch):
    state = make_state()
    monkeypatch.setattr(server, "now_ms", lambda: 1000 + server.BODY_CONTROL_TIMEOUT_MS)
    snapshot = state.body_snapshot()
    assert snapshot["transmission_control"]["status"] == "expired"
    assert "超时" in snapshot["transmission_control"]["error"]
