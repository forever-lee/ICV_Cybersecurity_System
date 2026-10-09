#!/usr/bin/env python3
"""Jetson 车端一键启动文件（TX2 / Orin NX / Orin Nano 自适应）。

直接运行：
    python3 run_vehicle_jetson.py

只检查板端环境：
    python3 run_vehicle_jetson.py --check-only
"""

import os
import platform
import shutil
import subprocess
import sys


# ======================== 板端配置 ========================
# 车载以太网摄像头地址。
RTSP_URL = os.getenv("RTSP_URL", "rtsp://192.168.4.88:8554/main")

# 当前摄像头日志已确认直接输出 H.265/HEVC；也支持切换为 H.264。
RTSP_CODEC = os.getenv("RTSP_CODEC", "h265").lower()

# copy：不解码、不重编码，只把摄像头 H.264 封装为浏览器可播的 fMP4。
# encode：保留 NVIDIA/CPU 编码管线作为兼容回退。
VIDEO_MODE = os.getenv("VIDEO_MODE", "copy").lower()

# cpolar 对应的 WebSocket 云端入口；随机域名变化后修改这里。
CLOUD_WS_URL = os.getenv(
    "CLOUD_WS_URL", "ws://719b8bb7.r7.nas.cpolar.cn"
)

VEHICLE_ID = os.getenv("VEHICLE_ID", "VHC-001")

# 必须与云端 run_cloud.py 中的 INGEST_TOKEN 完全一致。
INGEST_TOKEN = os.getenv(
    "VEHICLE_INGEST_TOKEN",
    "vcl_687Nfse29GsoYlX0j8hPaK4ctMv_5g4nXBeYpy1Obu0",
)

# Jetson H.264/fMP4 公网稳定档：使用 20 FPS 和 2.2 Mbps，
# 为 cpolar、WebSocket 开销及移动网络波动保留更充足的带宽余量。
MAX_WIDTH = int(os.getenv("VIDEO_MAX_WIDTH", "1280"))
TARGET_FPS = int(os.getenv("VIDEO_TARGET_FPS", "20"))
H264_BITRATE_KBPS = int(os.getenv("VIDEO_BITRATE_KBPS", "2200"))
H264_SEGMENT_MS = int(os.getenv("VIDEO_SEGMENT_MS", "1000"))
BUFFER_SECONDS = float(os.getenv(
    "VIDEO_BUFFER_SECONDS", "3" if VIDEO_MODE == "copy" else "300"
))
SEND_TIMEOUT = float(os.getenv("VIDEO_SEND_TIMEOUT", "60"))
GST_LAUNCH_PATH = os.getenv("GST_LAUNCH_PATH", "gst-launch-1.0")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
SOFTWARE_PRESET = os.getenv("VIDEO_SOFTWARE_PRESET", "veryfast")
# Navigation_Module_Board.py 持续原子更新该文件；视频上传程序每秒将其
# 随车辆遥测发送到云端。数据始终来自边缘端，不读取浏览器定位。
DEFAULT_NAVIGATION_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "navigation_live.json"
)
NAVIGATION_FILE = os.getenv("VEHICLE_NAVIGATION_FILE", DEFAULT_NAVIGATION_FILE)
# ===========================================================


def command_available(name):
    return shutil.which(name) is not None


def plugin_available(name):
    if not command_available("gst-inspect-1.0"):
        return False
    try:
        result = subprocess.run(
            ["gst-inspect-1.0", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ffmpeg_encoder_available(name):
    executable = shutil.which(FFMPEG_PATH)
    if not executable:
        return False
    try:
        output = subprocess.check_output(
            [executable, "-hide_banner", "-encoders"],
            stderr=subprocess.STDOUT,
            timeout=10,
        ).decode("utf-8", "replace")
        return name in output
    except (OSError, subprocess.SubprocessError):
        return False


def jetson_model():
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            with open(path, "rb") as handle:
                value = handle.read().replace(b"\x00", b"").decode("utf-8", "replace").strip()
            if value:
                return value
        except OSError:
            pass
    return "未知 Jetson 型号"


def preflight_report():
    passthrough_depay = "rtph265depay" if RTSP_CODEC == "h265" else "rtph264depay"
    passthrough_parser = "h265parse" if RTSP_CODEC == "h265" else "h264parse"
    passthrough_plugins = (
        "rtspsrc", passthrough_depay, passthrough_parser, "mp4mux", "fdsink",
    )
    passthrough_gst_ready = command_available(GST_LAUNCH_PATH) and all(
        plugin_available(name) for name in passthrough_plugins
    )
    passthrough_ffmpeg_ready = command_available(FFMPEG_PATH)
    common_plugins = (
        "rtspsrc",
        "nvv4l2decoder",
        "nvvidconv",
        "videorate",
        "h264parse",
        "mp4mux",
        "fdsink",
        "rtp{}depay".format(RTSP_CODEC),
        "{}parse".format(RTSP_CODEC),
    )
    gst_common_ready = command_available(GST_LAUNCH_PATH) and all(
        plugin_available(name) for name in common_plugins
    )
    hardware_encoder = plugin_available("nvv4l2h264enc")
    software_gst_encoder = plugin_available("x264enc")
    hardware_pipeline_ready = gst_common_ready and hardware_encoder
    software_gst_pipeline_ready = gst_common_ready and software_gst_encoder
    ffmpeg_pipeline_ready = ffmpeg_encoder_available("libx264")
    if VIDEO_MODE == "copy":
        pipeline_ready = passthrough_ffmpeg_ready
        if passthrough_ffmpeg_ready:
            selected_backend = "RTSP/{} → FFmpeg fMP4（零转码）".format(RTSP_CODEC.upper())
        else:
            selected_backend = "不可用：透传模式需要安装 FFmpeg"
    else:
        pipeline_ready = (
            hardware_pipeline_ready or software_gst_pipeline_ready or ffmpeg_pipeline_ready
        )
        if hardware_pipeline_ready:
            selected_backend = "NVDEC/VIC/nvv4l2h264enc（硬件编码）"
        elif software_gst_pipeline_ready:
            selected_backend = "NVDEC/VIC/x264enc（硬件解码 + CPU 编码）"
        elif ffmpeg_pipeline_ready:
            selected_backend = "FFmpeg/libx264（CPU 解码 + CPU 编码）"
        else:
            selected_backend = "不可用：请安装 GStreamer x264enc 或 FFmpeg libx264"

    # Fourth tuple item controls whether a missing capability blocks startup.
    checks = []
    architecture = platform.machine().lower()
    python_version = "{}.{}.{}".format(*sys.version_info[:3])
    checks.append(("Python 版本", sys.version_info >= (3, 8), python_version, True))
    checks.append(("AArch64 架构", architecture in {"aarch64", "arm64"}, architecture, True))
    checks.append(
        (
            "Jetson 实际型号",
            os.path.exists("/etc/nv_tegra_release")
            or os.path.exists("/etc/nv_boot_control.conf"),
            jetson_model(),
            True,
        )
    )
    checks.append(("GStreamer 工具", command_available(GST_LAUNCH_PATH), GST_LAUNCH_PATH, False))
    checks.append(
        ("NVIDIA 硬件解码", plugin_available("nvv4l2decoder"), "nvv4l2decoder", False)
    )
    checks.append(("NVIDIA 硬件缩放", plugin_available("nvvidconv"), "nvvidconv", False))
    checks.append(
        (
            "NVIDIA H.264 硬编码",
            hardware_encoder,
            "nvv4l2h264enc（Orin Nano 无此硬件，缺少时自动软件编码）",
            False,
        )
    )
    checks.append(("GStreamer x264 软件编码", software_gst_encoder, "x264enc", False))
    checks.append(("FFmpeg libx264 备用编码", ffmpeg_pipeline_ready, "libx264", False))
    checks.append(("fMP4 封装", plugin_available("mp4mux"), "mp4mux", False))

    try:
        import websockets

        checks.append(("WebSockets", True, websockets.__version__, True))
    except Exception as exc:
        checks.append(("WebSockets", False, str(exc), True))

    try:
        import dataclasses  # noqa: F401

        checks.append(("Dataclasses", True, "available", True))
    except Exception as exc:
        checks.append(("Dataclasses", False, str(exc), True))

    checks.append(
        (
            "Jetson H.264/fMP4 管线",
            pipeline_ready,
            selected_backend,
            True,
        )
    )

    print("-" * 68)
    print("Jetson 运行环境检查")
    for label, passed, detail, required in checks:
        status = "OK" if passed else ("WARN" if required else "INFO")
        print("[{:<4}] {:<24} {}".format(status, label, detail))
    print("-" * 68)
    return checks


def main():
    check_only = "--check-only" in sys.argv[1:]
    if VIDEO_MODE not in {"copy", "encode"}:
        raise SystemExit("VIDEO_MODE 只能设置为 copy 或 encode")
    checks = preflight_report()
    failed = [label for label, passed, _, required in checks if required and not passed]
    if check_only:
        if failed:
            print("自检完成，以下项目需要处理：{}".format("、".join(failed)))
            return 1
        print("自检通过，可以直接启动 Jetson 车端。")
        return 0
    if failed:
        raise SystemExit(
            "Jetson 环境未就绪，请先处理：{}".format("、".join(failed))
        )

    if RTSP_CODEC not in {"h264", "h265"}:
        raise SystemExit("RTSP_CODEC 只能设置为 h264 或 h265")
    if not INGEST_TOKEN or INGEST_TOKEN == "change-me-in-production":
        raise SystemExit("请先配置与云端一致的 VEHICLE_INGEST_TOKEN")

    sys.argv = [
        sys.argv[0],
        "--source", RTSP_URL,
        "--cloud", CLOUD_WS_URL,
        "--vehicle-id", VEHICLE_ID,
        "--token", INGEST_TOKEN,
        "--width", str(MAX_WIDTH),
        "--fps", str(TARGET_FPS),
        "--bitrate-kbps", str(H264_BITRATE_KBPS),
        "--segment-ms", str(H264_SEGMENT_MS),
        "--buffer-seconds", str(BUFFER_SECONDS),
        "--encoder", "copy" if VIDEO_MODE == "copy" else "jetson",
        "--gst-launch", GST_LAUNCH_PATH,
        "--ffmpeg", FFMPEG_PATH,
        "--software-preset", SOFTWARE_PRESET,
        "--rtsp-codec", RTSP_CODEC,
        "--send-timeout", str(SEND_TIMEOUT),
        "--navigation-file", NAVIGATION_FILE,
    ]

    import h264_vehicle_agent as vehicle_agent

    # Different JetPack images bundle different FFmpeg/Libav option sets.
    # This RTSP source is a client connection and works without an explicit
    # read-timeout flag; passing rw_timeout/stimeout breaks some Orin builds.
    vehicle_agent.ffmpeg_rtsp_timeout_args = lambda _executable: []
    run_agent = vehicle_agent.main

    print("=" * 68)
    print("V-SHIELD Jetson 车端正在启动")
    print("车端版本：2026-09-11-h265-copy-v4-no-timeout")
    print("系统型号：{}".format(jetson_model()))
    print("车辆编号：{}".format(VEHICLE_ID))
    print("摄像头：{} ({})".format(RTSP_URL, RTSP_CODEC.upper()))
    print("云端：{}".format(CLOUD_WS_URL))
    print(
        "视频档位：{}x{} / {} FPS / H.264 High / {} Kbps".format(
            MAX_WIDTH,
            int(round(MAX_WIDTH * 9 / 16)),
            TARGET_FPS,
            H264_BITRATE_KBPS,
        )
    )
    if VIDEO_MODE == "copy":
        print("视频后端：摄像头 {} 原码流 → FFmpeg 仅封装 fMP4 → WebSocket".format(RTSP_CODEC.upper()))
        print("注意：分辨率、FPS、码率和关键帧间隔由摄像头端控制")
    elif plugin_available("nvv4l2h264enc"):
        print("视频后端：nvv4l2decoder + nvvidconv + nvv4l2h264enc + fMP4")
    elif plugin_available("x264enc"):
        print("视频后端：nvv4l2decoder + nvvidconv + x264enc(CPU) + fMP4")
    else:
        print("视频后端：FFmpeg + libx264(CPU) + fMP4")
    print("按 Ctrl+C 可停止车端程序")
    print("=" * 68)
    run_agent()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
