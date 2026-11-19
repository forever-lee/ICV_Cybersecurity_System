import asyncio
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


def test_video_timeout_defaults_recover_stalled_public_tunnel(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["h264_vehicle_agent.py"])

    args = h264_vehicle_agent.parse_args()

    assert args.ws_open_timeout == 45
    assert args.ws_ping_interval == 30
    assert args.ws_ping_timeout == 90
    assert args.fragment_stall_timeout == 8
    assert args.fragment_startup_timeout == 45


def test_cloud_connection_timeout_supports_legacy_websockets(monkeypatch):
    captured = {}

    class FakeSocket:
        closed = False

        async def close(self):
            self.closed = True

    socket = FakeSocket()

    async def create_socket():
        return socket

    def connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return create_socket()

    monkeypatch.setattr(h264_vehicle_agent.websockets, "connect", connect, raising=False)
    args = SimpleNamespace(
        ws_open_timeout=45,
        ws_ping_interval=30,
        ws_ping_timeout=90,
    )

    async def exercise():
        async with h264_vehicle_agent.open_cloud_websocket("wss://example.test/ingest", args) as opened:
            assert opened is socket

    asyncio.run(exercise())

    assert captured["url"] == "wss://example.test/ingest"
    assert "open_timeout" not in captured["kwargs"]
    assert captured["kwargs"]["ping_interval"] == 30
    assert captured["kwargs"]["ping_timeout"] == 90
    assert socket.closed is True


def test_copy_command_matches_integration_five_domains(monkeypatch):
    args = SimpleNamespace(
        ffmpeg="ffmpeg",
        source="rtsp://192.168.4.88:8554/main",
        rtsp_codec="h265",
        fps=25,
        segment_ms=1000,
    )
    monkeypatch.setattr(
        h264_vehicle_agent,
        "ffmpeg_executable",
        lambda _value: "/usr/bin/ffmpeg",
    )
    monkeypatch.setattr(
        h264_vehicle_agent,
        "ffmpeg_rtsp_timeout_args",
        lambda _executable: [],
    )

    command = h264_vehicle_agent.build_command(args, "copy")

    assert command[command.index("-fflags") + 1] == "+genpts+discardcorrupt"
    assert command[command.index("-use_wallclock_as_timestamps") + 1] == "1"
    assert command[command.index("-analyzeduration") + 1] == "15000000"
    assert command[command.index("-probesize") + 1] == "10000000"
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-tag:v") + 1] == "hvc1"
    assert command[command.index("-movflags") + 1] == (
        "+frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset"
    )
    assert command[command.index("-frag_duration") + 1] == "1000000"
    assert command[command.index("-avoid_negative_ts") + 1] == "make_zero"
