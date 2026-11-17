import sys
from types import SimpleNamespace


sys.modules.setdefault("websockets", SimpleNamespace())

from jetson_edge import h264_vehicle_agent


def test_ffmpeg_encoder_available_matches_encoder_listing(monkeypatch):
    monkeypatch.setattr(
        h264_vehicle_agent,
        "ffmpeg_executable",
        lambda _value: "/usr/bin/ffmpeg",
    )
    monkeypatch.setattr(
        h264_vehicle_agent.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (
            b"Encoders:\n V..... libx264             H.264 / AVC / MPEG-4 AVC\n"
        ),
    )

    assert h264_vehicle_agent.ffmpeg_encoder_available("libx264") is True
    assert h264_vehicle_agent.ffmpeg_encoder_available("h264_nvenc") is False


def test_websocket_timeout_defaults_tolerate_public_tunnel_jitter(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["h264_vehicle_agent.py"])

    args = h264_vehicle_agent.parse_args()

    assert args.ws_open_timeout == 45
    assert args.ws_ping_interval == 30
    assert args.ws_ping_timeout == 90
    assert args.fragment_stall_timeout == 20
