import pytest


pytest.importorskip("cv2")
pytest.importorskip("numpy")
pytest.importorskip("fastapi")

from server import VehicleState, now_ms


def navigation(captured_at_ms, longitude=106.6006, latitude=29.5345):
    return {
        "longitude": longitude,
        "latitude": latitude,
        "captured_at_ms": captured_at_ms,
        "coordinate_system": "WGS84",
        "source": "EDGE-GNSS",
    }


def test_navigation_history_keeps_complete_current_session_and_deduplicates_replay():
    state = VehicleState("VHC-001")
    started_at_ms = now_ms() - 1000
    first = navigation(started_at_ms + 100)
    second = navigation(started_at_ms + 200, longitude=106.6007)

    assert state.record_navigation(first, "session-a", started_at_ms) is True
    assert state.record_navigation(first, "session-a", started_at_ms) is True
    assert state.record_navigation(second, "session-a", started_at_ms) is True

    assert state.navigation_session_id == "session-a"
    assert len(state.navigation_history) == 2
    snapshot = state.snapshot(include_navigation_history=True)
    assert snapshot["navigation_history_count"] == 2
    assert snapshot["navigation_history"] == state.navigation_history


def test_new_edge_session_clears_previous_history_and_rejects_stale_navigation_file():
    state = VehicleState("VHC-001")
    first_started_at_ms = now_ms() - 2000
    state.record_navigation(
        navigation(first_started_at_ms + 100), "session-a", first_started_at_ms
    )
    second_started_at_ms = now_ms() - 500

    state.begin_navigation_session("session-b", second_started_at_ms)

    assert state.navigation is None
    assert state.navigation_history == []
    assert state.record_navigation(
        navigation(first_started_at_ms + 200), "session-b", second_started_at_ms
    ) is False
    assert state.navigation_history == []

    assert state.record_navigation(
        navigation(second_started_at_ms + 100), "session-b", second_started_at_ms
    ) is True
    assert len(state.navigation_history) == 1
