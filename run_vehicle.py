"""PyCharm 直接运行：读取车载 RTSP 视频并上传云端。"""

import os
import sys


# ======================== 可修改配置 ========================
# 当前车载以太网摄像头地址。
RTSP_URL = "rtsp://192.168.4.88:8554/main"

# 使用 RTX GPU 硬件解码。文件不存在时 vehicle_agent.py 会自动回退 OpenCV。
FFMPEG_PATH = r"D:\ffpg\ffmpeg-8.1.1-essentials_build\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe"

# auto：Windows 自动使用上面的 FFmpeg CUDA；复制到 Jetson TX2 后自动使用
# nvv4l2decoder + nvvidconv。当前摄像头实际输出 HEVC，程序会自动识别。
VIDEO_BACKEND = "auto"
RTSP_CODEC = "h265"

# 当前 run_vehicle.py 与 run_cloud.py 运行在同一台电脑，车端数据必须走
# 本机回环，不能先经过 cpolar 再绕回本机；公网回环实测只剩约 2 FPS。
# Jetson 真车端仍在 run_vehicle_jetson.py 中使用公网 WSS 地址。
CLOUD_WS_URL = os.getenv("CLOUD_WS_URL", "ws://127.0.0.1:8001")

VEHICLE_ID = "VHC-001"

# 必须与 run_cloud.py 中的 INGEST_TOKEN 完全一致。
INGEST_TOKEN = "vcl_687Nfse29GsoYlX0j8hPaK4ctMv_5g4nXBeYpy1Obu0"

# 视频质量配置：现有 cpolar 公网链路的流畅优先档。实测可用带宽约
# 3.5 Mbps，因此默认使用 720p / 30 FPS / H.264 2.8 Mbps 严格限速，
# 同时保留 P7、双遍、前向分析、AQ 和 B 帧以提高有限码率下的画质。
MAX_WIDTH = int(os.getenv("VIDEO_MAX_WIDTH", "1280"))
TARGET_FPS = int(os.getenv("VIDEO_TARGET_FPS", "30"))
JPEG_QUALITY = 62
H264_BITRATE_KBPS = int(os.getenv("VIDEO_BITRATE_KBPS", "2800"))
H264_SEGMENT_MS = int(os.getenv("VIDEO_SEGMENT_MS", "1000"))

# 目标上行带宽，单位 Kbps。超过后会逐步降低 JPEG 质量。
# 设置为 0 可关闭带宽自适应。
TARGET_KBPS = 3800

# H.264 片段采用 5 分钟 FIFO。网络恢复后按原顺序补传，不主动追帧。
BUFFER_SECONDS = float(os.getenv("VIDEO_BUFFER_SECONDS", "300"))

# 允许多张 JPEG 同时进入 TCP 发送窗口，充分利用公网带宽。
WS_WRITE_LIMIT_BYTES = 256 * 1024
MAX_FRAME_AGE_SECONDS = 10

# GNSS/CAN 适配程序持续更新的 JSON 文件。留空时不上传导航数据。
# 示例字段：latitude、longitude、speed_kph、heading_deg、accuracy_m、captured_at_ms。
NAVIGATION_FILE = os.getenv("VEHICLE_NAVIGATION_FILE", "")
# ===========================================================


def main():
    # 复用 vehicle_agent.py 的成熟采流、重连和上传逻辑。
    # 通过参数注入避免在 PyCharm 中手工配置运行参数。
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
        "--send-timeout", os.getenv("VIDEO_SEND_TIMEOUT", "60"),
        "--ffmpeg", FFMPEG_PATH,
        "--encoder", "auto",
        "--navigation-file", NAVIGATION_FILE,
    ]

    # 需要在导入 OpenCV 之前写入低延迟参数。
    os.environ.setdefault(
        "OPENCV_FFMPEG_CAPTURE_OPTIONS",
        "rtsp_transport;tcp|stimeout;5000000",
    )

    from h264_vehicle_agent import main as run_agent

    print("=" * 64)
    print("车端视频上传程序正在启动")
    print("车辆编号：{}".format(VEHICLE_ID))
    print("摄像头地址：{}".format(RTSP_URL))
    print("云端地址：{}".format(CLOUD_WS_URL))
    print("视频后端：NVDEC/CUDA/NVENC 全 GPU（自动逐级回退）")
    print(
        "视频档位：{}px / {} FPS / H.264 {} Kbps / {} ms 连续片段".format(
            MAX_WIDTH, TARGET_FPS, H264_BITRATE_KBPS, H264_SEGMENT_MS
        )
    )
    print("按 Ctrl+C 可停止车端程序")
    print("=" * 64)
    run_agent()


if __name__ == "__main__":
    main()
