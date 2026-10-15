import threading
from types import SimpleNamespace

from jetson_edge.body_edge_agent import (
    service_transmission_command,
    validate_transmission_ack,
)


def runtime_state():
    return {"lock": threading.Lock(), "pending": None}


def test_transmission_command_retries_three_times_at_two_second_intervals():
    runtime = runtime_state()
    sent = []
    args = SimpleNamespace(
        control_retry_interval=2.0,
        control_timeout=10.0,
        control_max_attempts=3,
    )
    result = {
        "pending": True,
        "command": {"sequence": 42, "action": "stop"},
    }
    assert service_transmission_command(result, runtime, sent.append, args, 0.0) == 42
    assert service_transmission_command(result, runtime, sent.append, args, 1.0) == 0
    assert service_transmission_command(result, runtime, sent.append, args, 2.0) == 42
    assert service_transmission_command(result, runtime, sent.append, args, 4.0) == 42
    assert service_transmission_command(result, runtime, sent.append, args, 6.0) == 0
    assert len(sent) == 3


def test_ack_must_match_action_and_actual_board_state():
    runtime = runtime_state()
    runtime["pending"] = {
        "sequence": 42, "action": "stop", "confirmed": False,
    }
    decoded = {
        "transmissionEnabled": True,
        "transmissionAck": {
            "sequence": 42, "action": "stop", "action_code": 0,
            "result": "applied",
        },
    }
    assert validate_transmission_ack(decoded, runtime) is None
    assert runtime["pending"]["confirmed"] is False

    decoded["transmissionEnabled"] = False
    assert validate_transmission_ack(decoded, runtime)["sequence"] == 42
    assert runtime["pending"]["confirmed"] is True
