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

    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-tag:v") + 1] == "hvc1"
    assert command[command.index("-movflags") + 1] == (
        "+frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset"
    )
    assert command[command.index("-frag_duration") + 1] == "1000000"
