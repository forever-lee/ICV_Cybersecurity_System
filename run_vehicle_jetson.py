#!/usr/bin/env python3
"""Jetson TX2 车端一键启动文件。

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

# 当前摄像头实际输出 H.265；如摄像头配置改为 H.264，可用环境变量覆盖。
RTSP_CODEC = os.getenv("RTSP_CODEC", "h265").lower()

# cpolar 对应的 WebSocket 云端入口；随机域名变化后修改这里。
CLOUD_WS_URL = os.getenv(
    "CLOUD_WS_URL", "ws://6bf794aa.r7.nas.cpolar.cn"
)

VEHICLE_ID = os.getenv("VEHICLE_ID", "VHC-001")

# 必须与云端 run_cloud.py 中的 INGEST_TOKEN 完全一致。
INGEST_TOKEN = os.getenv(
    "VEHICLE_INGEST_TOKEN",
    "vcl_687Nfse29GsoYlX0j8hPaK4ctMv_5g4nXBeYpy1Obu0",
)

# TX2 全硬件 H.264/fMP4 公网稳定档：使用 20 FPS 和 2.2 Mbps CBR，
# 为 cpolar、WebSocket 开销及移动网络波动保留更充足的带宽余量。
MAX_WIDTH = int(os.getenv("VIDEO_MAX_WIDTH", "1280"))
TARGET_FPS = int(os.getenv("VIDEO_TARGET_FPS", "20"))
H264_BITRATE_KBPS = int(os.getenv("VIDEO_BITRATE_KBPS", "2200"))
H264_SEGMENT_MS = int(os.getenv("VIDEO_SEGMENT_MS", "1000"))
BUFFER_SECONDS = float(os.getenv("VIDEO_BUFFER_SECONDS", "300"))
SEND_TIMEOUT = float(os.getenv("VIDEO_SEND_TIMEOUT", "60"))
GST_LAUNCH_PATH = os.getenv("GST_LAUNCH_PATH", "gst-launch-1.0")
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


def preflight_report():
    checks = []
    required_plugins = (
        "rtspsrc",
        "nvv4l2decoder",
        "nvvidconv",
        "nvv4l2h264enc",
        "videorate",
        "h264parse",
        "mp4mux",
        "fdsink",
        "rtp{}depay".format(RTSP_CODEC),
        "{}parse".format(RTSP_CODEC),
    )
    cli_pipeline_ready = command_available(GST_LAUNCH_PATH) and all(
        plugin_available(name)
        for name in required_plugins
    )
    architecture = platform.machine().lower()
    python_version = "{}.{}.{}".format(*sys.version_info[:3])
    checks.append(("Python 版本", sys.version_info >= (3, 6), python_version))
    checks.append(("AArch64 架构", architecture in {"aarch64", "arm64"}, architecture))
    checks.append(
        (
            "Jetson 系统标识",
            os.path.exists("/etc/nv_tegra_release")
            or os.path.exists("/etc/nv_boot_control.conf"),
            "/etc/nv_tegra_release",
        )
    )
    checks.append(("GStreamer 工具", command_available(GST_LAUNCH_PATH), GST_LAUNCH_PATH))
    checks.append(
        ("NVIDIA 硬件解码", plugin_available("nvv4l2decoder"), "nvv4l2decoder")
    )
    checks.append(("NVIDIA 硬件缩放", plugin_available("nvvidconv"), "nvvidconv"))
    checks.append(("NVIDIA H.264 硬编码", plugin_available("nvv4l2h264enc"), "nvv4l2h264enc"))
    checks.append(("fMP4 封装", plugin_available("mp4mux"), "mp4mux"))

    try:
        import websockets

        checks.append(("WebSockets", True, websockets.__version__))
    except Exception as exc:
        checks.append(("WebSockets", False, str(exc)))

    try:
        import dataclasses  # noqa: F401 - Python 3.6 uses the backport package.

        checks.append(("Dataclasses", True, "available"))
    except Exception as exc:
        checks.append(("Dataclasses", False, str(exc)))

    checks.append(
        (
            "Jetson H.264/fMP4 管线",
            cli_pipeline_ready,
            "全部插件可用" if cli_pipeline_ready else "缺少必要的 GStreamer 插件",
        )
    )

    print("-" * 68)
    print("Jetson TX2 运行环境检查")
    for label, passed, detail in checks:
        print("[{:<4}] {:<20} {}".format("OK" if passed else "WARN", label, detail))
    print("-" * 68)
    return checks


def main():
    check_only = "--check-only" in sys.argv[1:]
    checks = preflight_report()
    failed = [label for label, passed, _ in checks if not passed]
    if check_only:
        if failed:
            print("自检完成，以下项目需要处理：{}".format("、".join(failed)))
            return 1
        print("自检通过，可以直接启动 TX2 车端。")
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
        "--encoder", "jetson",
        "--gst-launch", GST_LAUNCH_PATH,
        "--rtsp-codec", RTSP_CODEC,
        "--send-timeout", str(SEND_TIMEOUT),
        "--navigation-file", NAVIGATION_FILE,
    ]

    from h264_vehicle_agent import main as run_agent

    print("=" * 68)
    print("V-SHIELD Jetson TX2 车端正在启动")
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
    print("视频后端：nvv4l2decoder + nvvidconv + nvv4l2h264enc + fMP4")
    print("按 Ctrl+C 可停止车端程序")
    print("=" * 68)
    run_agent()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
