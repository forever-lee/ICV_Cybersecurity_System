"""Vehicle-side RTSP capture and outbound cloud uploader."""

import argparse
import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000|rw_timeout;5000000",
)

import cv2
import numpy as np
import websockets

from stream_protocol import now_ms, pack_frame, unpack_frame


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("vehicle-agent")
MIN_JPEG_QUALITY = max(45, min(90, int(os.getenv("JPEG_MIN_QUALITY", "52"))))


def read_navigation_file(path):
    """Read the latest edge navigation sample written by a GNSS/CAN adapter."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict) and isinstance(payload.get("navigation"), dict):
            payload = payload["navigation"]
        if not isinstance(payload, dict):
            return None
        navigation = dict(payload)
        navigation.setdefault("coordinate_system", "WGS84")
        navigation.setdefault("source", "EDGE-GNSS")
        navigation.setdefault("captured_at_ms", now_ms())
        return navigation
    except (OSError, ValueError, TypeError):
        return None


@dataclass
class SharedFrame:
    packet: bytes = None
    sequence: int = 0
    captured_at_ms: int = 0
    width: int = 0
    height: int = 0
    source_fps: float = 0.0
    encoded_fps: float = 0.0
    quality: int = 88
    source_status: str = "starting"
    capture_reconnects: int = 0
    capture_failures: int = 0
    encoded_bytes: int = 0
    capture_backend: str = "starting"
    # 有界 FIFO 用来吸收移动网络抖动；满队列时 deque 自动丢弃最旧帧，
    # 因此延迟不会无限增长。
    queue_capacity: int = 1
    packet_queue: deque = field(init=False)
    queue_dropped_frames: int = 0
    stale_dropped_frames: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self):
        self.packet_queue = deque(maxlen=max(1, int(self.queue_capacity)))


class CaptureWorker(threading.Thread):
    def __init__(
        self,
        source,
        shared,
        width,
        fps,
        initial_quality,
        target_kbps,
        open_timeout_ms=5000,
        read_timeout_ms=5000,
    ):
        super().__init__(name="rtsp-capture", daemon=True)
        self.source = source
        self.shared = shared
        self.max_width = width
        self.requested_fps = fps
        self.initial_quality = initial_quality
        self.target_kbps = target_kbps
        self.open_timeout_ms = open_timeout_ms
        self.read_timeout_ms = read_timeout_ms

    def run(self):
        quality = self.initial_quality
        sequence = 0
        next_frame_at = time.monotonic()
        window_started = time.monotonic()
        window_frames = 0
        window_bytes = 0
        while not self.shared.stop.is_set():
            if self.source == "synthetic":
                capture = None
                source_fps = float(self.requested_fps)
                self.shared.source_status = "synthetic"
            else:
                capture = self.open_capture()
                if capture is None:
                    self.wait_or_stop(1.0)
                    continue
                source_fps = capture.get(cv2.CAP_PROP_FPS) or float(self.requested_fps)

            failures = 0
            while not self.shared.stop.is_set():
                started = time.monotonic()
                if capture is None:
                    self.wait_or_stop(max(0.0, next_frame_at - started))
                    started = time.monotonic()
                    frame = self.synthetic_frame(sequence)
                    ok = True
                else:
                    # grab 持续排空 RTSP，但只在上传时间片到达时 retrieve/BGR 转换。
                    # 对 2880x1620 原始流可显著减少无效帧的 CPU 与内存复制开销。
                    ok = capture.grab()
                    frame = None
                    if ok and time.monotonic() < next_frame_at:
                        continue
                    if ok:
                        ok, frame = capture.retrieve()
                if not ok or frame is None:
                    failures += 1
                    with self.shared.lock:
                        self.shared.capture_failures += 1
                        self.shared.source_status = "recovering"
                    if failures >= 5:
                        break
                    self.wait_or_stop(0.05)
                    continue

                failures = 0
                # RTSP 必须持续按源帧率读取以排空 FFmpeg 缓冲；只降低编码/上传帧率。
                # 否则 25 FPS 的源流以 3~5 FPS 读取会在数秒后因缓冲堆积而卡住。
                captured_at = now_ms()
                frame = self.resize_frame(frame)
                height, width = frame.shape[:2]
                ok, encoded = cv2.imencode(
                    ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
                )
                if not ok:
                    continue
                sequence = (sequence + 1) & 0xFFFFFFFF
                packet = pack_frame(
                    sequence, captured_at, now_ms(), width, height, quality, encoded.tobytes()
                )
                window_frames += 1
                window_bytes += len(packet)
                elapsed = time.monotonic() - window_started
                measured_fps = window_frames / elapsed if elapsed > 0 else 0.0
                with self.shared.lock:
                    if len(self.shared.packet_queue) == self.shared.packet_queue.maxlen:
                        self.shared.queue_dropped_frames += 1
                    self.shared.packet_queue.append((sequence, packet))
                    self.shared.packet = packet
                    self.shared.sequence = sequence
                    self.shared.captured_at_ms = captured_at
                    self.shared.width = width
                    self.shared.height = height
                    self.shared.source_fps = source_fps
                    self.shared.encoded_fps = measured_fps
                    self.shared.quality = quality
                    self.shared.encoded_bytes = len(packet)
                    self.shared.source_status = "online"
                if elapsed >= 2.0:
                    measured_kbps = window_bytes * 8 / elapsed / 1000
                    if self.target_kbps > 0:
                        if measured_kbps > self.target_kbps * 1.12:
                            # 帧率优先：带宽吃紧时允许继续降低 JPEG 质量，
                            # 避免因单帧过大触发上传节流而直接损失帧率。
                            quality = max(MIN_JPEG_QUALITY, quality - 3)
                        elif measured_kbps < self.target_kbps * 0.72:
                            quality = min(self.initial_quality, quality + 2)
                    window_started = time.monotonic()
                    window_frames = 0
                    window_bytes = 0

                frame_interval = 1.0 / max(1, self.requested_fps)
                # 使用固定时间轴调度；启动或处理过慢时跳过已经错过的时间片，
                # 既不把 read/encode 耗时重复叠加，也不在恢复后瞬间超速补帧。
                next_frame_at += frame_interval
                current_time = time.monotonic()
                if next_frame_at <= current_time:
                    missed_slots = int((current_time - next_frame_at) / frame_interval) + 1
                    next_frame_at += missed_slots * frame_interval

            if capture is not None:
                capture.release()
            with self.shared.lock:
                self.shared.capture_reconnects += 1
                self.shared.source_status = "recovering"
            self.wait_or_stop(0.5)

    def open_capture(self):
        safe_source = redact_url(self.source)
        LOGGER.info("opening_rtsp source=%s", safe_source)
        params = []
        if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
            params.extend([cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.open_timeout_ms])
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            params.extend([cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.read_timeout_ms])
        try:
            capture = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG, params)
        except (TypeError, cv2.error):
            # Older JetPack OpenCV builds don't support constructor parameters.
            capture = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            LOGGER.error("rtsp_open_failed source=%s", safe_source)
            capture.release()
            return None
        LOGGER.info(
            "rtsp_opened source=%s resolution=%sx%s fps=%.1f",
            safe_source,
            int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            capture.get(cv2.CAP_PROP_FPS),
        )
        with self.shared.lock:
            self.shared.capture_backend = "opencv-ffmpeg"
        return capture

    def resize_frame(self, frame):
        if self.max_width <= 0 or frame.shape[1] <= self.max_width:
            return frame
        scale = self.max_width / float(frame.shape[1])
        return cv2.resize(frame, (self.max_width, int(frame.shape[0] * scale)), interpolation=cv2.INTER_AREA)

    def synthetic_frame(self, sequence):
        width = self.max_width if self.max_width > 0 else 1280
        height = int(width * 9 / 16)
        x = np.linspace(0, 1, width, dtype=np.float32)[None, :]
        y = np.linspace(0, 1, height, dtype=np.float32)[:, None]
        frame = np.empty((height, width, 3), dtype=np.uint8)
        frame[:, :, 0] = np.clip(28 + 42 * y + 12 * x, 0, 255)
        frame[:, :, 1] = np.clip(36 + 34 * y + 18 * x, 0, 255)
        frame[:, :, 2] = np.clip(40 + 25 * y, 0, 255)
        horizon = int(height * 0.46)
        cv2.rectangle(frame, (0, horizon), (width, height), (35, 39, 42), -1)
        vanishing = (width // 2, horizon)
        cv2.line(frame, vanishing, (int(width * 0.12), height), (132, 140, 139), 3)
        cv2.line(frame, vanishing, (int(width * 0.88), height), (132, 140, 139), 3)
        dash_offset = (sequence * 18) % 170
        for offset in range(-170, height, 170):
            y1 = horizon + offset + dash_offset
            y2 = min(height, y1 + 76)
            if y2 > horizon:
                ratio1 = max(0, (y1 - horizon) / max(1, height - horizon))
                ratio2 = max(0, (y2 - horizon) / max(1, height - horizon))
                p1 = (int(width / 2), int(max(horizon, y1)))
                p2 = (int(width / 2), int(y2))
                thickness = max(2, int(2 + ratio2 * 8))
                cv2.line(frame, p1, p2, (221, 229, 225), thickness)
        cv2.putText(frame, "VEHICLE CAMERA / SIMULATION", (32, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (223, 239, 235), 2)
        cv2.putText(frame, time.strftime("%Y-%m-%d %H:%M:%S"), (32, 84), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (162, 180, 177), 1)
        return frame

    def wait_or_stop(self, seconds):
        self.shared.stop.wait(seconds)


class FFmpegCaptureWorker(threading.Thread):
    """NVDEC decode + CUDA scale + MJPEG image pipe capture backend."""

    def __init__(self, ffmpeg_path, source, shared, width, fps, quality):
        super().__init__(name="ffmpeg-capture", daemon=True)
        self.ffmpeg_path = ffmpeg_path
        self.source = source
        self.shared = shared
        self.width = width
        self.height = int(round(width * 9 / 16))
        self.requested_fps = fps
        self.quality = quality

    def run(self):
        use_cuda = True
        while not self.shared.stop.is_set():
            command = self.build_command(use_cuda)
            LOGGER.info(
                "opening_rtsp_ffmpeg source=%s backend=%s output=%sx%s@%s",
                redact_url(self.source),
                "cuda" if use_cuda else "cpu",
                self.width,
                self.height,
                self.requested_fps,
            )
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = None
            frame_count = 0
            try:
                with self.shared.lock:
                    self.shared.capture_backend = "ffmpeg-cuda" if use_cuda else "ffmpeg-cpu"
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    creationflags=creation_flags,
                )
                frame_count = self.read_jpeg_stream(process)
            except Exception as exc:
                LOGGER.warning("ffmpeg_capture_failed backend=%s error=%s", "cuda" if use_cuda else "cpu", exc)
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()

            if self.shared.stop.is_set():
                break
            with self.shared.lock:
                self.shared.capture_reconnects += 1
                self.shared.capture_failures += 1
                self.shared.source_status = "recovering"
            if use_cuda and frame_count == 0:
                LOGGER.warning("cuda_pipeline_unavailable falling_back_to_cpu")
                use_cuda = False
            self.shared.stop.wait(1.0)

    def build_command(self, use_cuda):
        qscale = max(2, min(20, int(round((100 - self.quality) / 7.0)) + 1))
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
        ]
        if use_cuda:
            command.extend(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])
        command.extend(["-i", self.source, "-an"])
        if use_cuda:
            video_filter = "fps={},scale_cuda={}:{},hwdownload,format=nv12".format(
                self.requested_fps, self.width, self.height
            )
        else:
            video_filter = "fps={},scale={}:{}".format(
                self.requested_fps, self.width, self.height
            )
        command.extend(
            [
                "-vf", video_filter,
                "-c:v", "mjpeg",
                "-q:v", str(qscale),
                "-f", "image2pipe",
                "pipe:1",
            ]
        )
        return command

    def read_jpeg_stream(self, process):
        buffer = bytearray()
        sequence = 0
        frame_count = 0
        window_started = time.monotonic()
        window_frames = 0
        with self.shared.lock:
            self.shared.source_status = "connecting"
            self.shared.source_fps = 25.0

        while not self.shared.stop.is_set():
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 2:
                        del buffer[:-2]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start > 0:
                        del buffer[:start]
                    break
                jpeg = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                captured_at = now_ms()
                sequence = (sequence + 1) & 0xFFFFFFFF
                packet = pack_frame(
                    sequence,
                    captured_at,
                    captured_at,
                    self.width,
                    self.height,
                    self.quality,
                    jpeg,
                )
                frame_count += 1
                window_frames += 1
                elapsed = time.monotonic() - window_started
                measured_fps = window_frames / max(0.001, elapsed)
                with self.shared.lock:
                    if len(self.shared.packet_queue) == self.shared.packet_queue.maxlen:
                        self.shared.queue_dropped_frames += 1
                    self.shared.packet_queue.append((sequence, packet))
                    self.shared.packet = packet
                    self.shared.sequence = sequence
                    self.shared.captured_at_ms = captured_at
                    self.shared.width = self.width
                    self.shared.height = self.height
                    self.shared.source_fps = 25.0
                    self.shared.encoded_fps = measured_fps
                    self.shared.quality = self.quality
                    self.shared.encoded_bytes = len(packet)
                    self.shared.source_status = "online"
                if elapsed >= 2.0:
                    window_started = time.monotonic()
                    window_frames = 0
        return frame_count


class JetsonCaptureWorker(CaptureWorker):
    """Jetson NVDEC/VIC RTSP pipeline with automatic OpenCV fallback."""

    def __init__(self, source, shared, width, fps, initial_quality, target_kbps, rtsp_codec):
        super().__init__(source, shared, width, fps, initial_quality, target_kbps)
        self.rtsp_codec = (rtsp_codec or "h264").lower()

    def open_capture(self):
        pipeline = self.build_pipeline()
        LOGGER.info(
            "opening_rtsp_jetson source=%s codec=%s output=%sx%s@%s",
            redact_url(self.source),
            self.rtsp_codec,
            self.max_width,
            int(round(self.max_width * 9 / 16)),
            self.requested_fps,
        )
        capture = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        if capture.isOpened():
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            with self.shared.lock:
                self.shared.capture_backend = "jetson-nvv4l2"
            LOGGER.info("jetson_gstreamer_ready hardware_decode=true")
            return capture

        capture.release()
        LOGGER.warning(
            "jetson_gstreamer_unavailable falling_back_to_opencv "
            "check_opencv_gstreamer_and_nvv4l2decoder"
        )
        return super().open_capture()

    def build_pipeline(self):
        codec = "h265" if self.rtsp_codec in {"h265", "hevc"} else "h264"
        depay = "rtph265depay" if codec == "h265" else "rtph264depay"
        parser = "h265parse" if codec == "h265" else "h264parse"
        height = int(round(self.max_width * 9 / 16))
        source = self.source.replace("\\", "\\\\").replace('"', '\\"')
        return (
            'rtspsrc location="{}" protocols=tcp latency=80 drop-on-latency=true ! '
            '{} ! {} ! nvv4l2decoder enable-max-performance=1 ! '
            'nvvidconv ! video/x-raw,width={},height={},format=BGRx ! '
            'videoconvert ! video/x-raw,format=BGR ! '
            'appsink sync=false drop=true max-buffers=1'
        ).format(
            source,
            depay,
            parser,
            self.max_width,
            height,
        )


class JetsonGStreamerProcessWorker(CaptureWorker):
    """Jetson hardware pipeline that does not require OpenCV GStreamer support.

    Many JetPack images ship NVIDIA's GStreamer plugins while their cv2 wheel is
    built with ``GStreamer: NO``.  Running gst-launch directly keeps NVDEC/VIC
    available and avoids decoding the 2880x1620 source on the TX2 CPU.
    """

    def __init__(
        self,
        source,
        shared,
        width,
        fps,
        initial_quality,
        target_kbps,
        rtsp_codec,
        open_timeout_ms=5000,
        read_timeout_ms=5000,
    ):
        super().__init__(
            source,
            shared,
            width,
            fps,
            initial_quality,
            target_kbps,
            open_timeout_ms,
            read_timeout_ms,
        )
        self.rtsp_codec = (rtsp_codec or "h264").lower()
        self.sequence = 0

    def run(self):
        consecutive_start_failures = 0
        while not self.shared.stop.is_set():
            command = self.build_command()
            LOGGER.info(
                "opening_rtsp_jetson_cli source=%s codec=%s output=%sx%s@%s",
                redact_url(self.source),
                self.rtsp_codec,
                self.max_width,
                int(round(self.max_width * 9 / 16)),
                self.requested_fps,
            )
            process = None
            frame_count = 0
            try:
                with self.shared.lock:
                    self.shared.capture_backend = "jetson-gstreamer-cli"
                    self.shared.source_status = "connecting"
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                frame_count = self.read_jpeg_stream(process)
            except Exception as exc:
                LOGGER.warning("jetson_gstreamer_cli_failed error=%s", exc)
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()

            if self.shared.stop.is_set():
                break
            with self.shared.lock:
                self.shared.capture_reconnects += 1
                self.shared.capture_failures += 1
                self.shared.source_status = "recovering"
            if frame_count == 0:
                consecutive_start_failures += 1
            else:
                consecutive_start_failures = 0
            if consecutive_start_failures >= 2:
                LOGGER.warning(
                    "jetson_gstreamer_cli_unavailable falling_back_to_opencv"
                )
                super().run()
                return
            self.wait_or_stop(0.5)

    def build_command(self):
        codec = "h265" if self.rtsp_codec in {"h265", "hevc"} else "h264"
        depay = "rtph265depay" if codec == "h265" else "rtph264depay"
        parser = "h265parse" if codec == "h265" else "h264parse"
        height = int(round(self.max_width * 9 / 16))
        return [
            "gst-launch-1.0",
            "-q",
            "rtspsrc",
            "location={}".format(self.source),
            "protocols=tcp",
            "latency=100",
            "drop-on-latency=true",
            "tcp-timeout={}".format(max(1000, self.read_timeout_ms) * 1000),
            "!",
            depay,
            "!",
            parser,
            "!",
            "nvv4l2decoder",
            "enable-max-performance=1",
            "!",
            "nvvidconv",
            "!",
            "video/x-raw,width={},height={},format=I420".format(
                self.max_width, height
            ),
            "!",
            "videorate",
            "drop-only=true",
            "max-rate={}".format(max(1, self.requested_fps)),
            "!",
            "jpegenc",
            "quality={}".format(max(MIN_JPEG_QUALITY, min(96, self.initial_quality))),
            "!",
            "fdsink",
            "fd=1",
            "sync=false",
        ]

    def read_jpeg_stream(self, process):
        buffer = bytearray()
        frame_count = 0
        window_started = time.monotonic()
        window_frames = 0
        height = int(round(self.max_width * 9 / 16))
        with self.shared.lock:
            self.shared.source_fps = float(self.requested_fps)

        while not self.shared.stop.is_set():
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 2:
                        del buffer[:-2]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start > 0:
                        del buffer[:start]
                    break
                jpeg = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                captured_at = now_ms()
                self.sequence = (self.sequence + 1) & 0xFFFFFFFF
                packet = pack_frame(
                    self.sequence,
                    captured_at,
                    captured_at,
                    self.max_width,
                    height,
                    self.initial_quality,
                    jpeg,
                )
                frame_count += 1
                window_frames += 1
                elapsed = time.monotonic() - window_started
                with self.shared.lock:
                    if len(self.shared.packet_queue) == self.shared.packet_queue.maxlen:
                        self.shared.queue_dropped_frames += 1
                    self.shared.packet_queue.append((self.sequence, packet))
                    self.shared.packet = packet
                    self.shared.sequence = self.sequence
                    self.shared.captured_at_ms = captured_at
                    self.shared.width = self.max_width
                    self.shared.height = height
                    self.shared.encoded_fps = window_frames / max(0.001, elapsed)
                    self.shared.quality = self.initial_quality
                    self.shared.encoded_bytes = len(packet)
                    self.shared.source_status = "online"
                if elapsed >= 2.0:
                    window_started = time.monotonic()
                    window_frames = 0
        return frame_count


def opencv_has_gstreamer():
    try:
        return any(
            "GStreamer" in line and "YES" in line
            for line in cv2.getBuildInformation().splitlines()
        )
    except Exception:
        return False


def jetson_cli_pipeline_available(rtsp_codec):
    if not shutil.which("gst-launch-1.0") or not shutil.which("gst-inspect-1.0"):
        return False
    codec = "h265" if rtsp_codec in {"h265", "hevc"} else "h264"
    plugins = [
        "nvv4l2decoder",
        "nvvidconv",
        "jpegenc",
        "videorate",
        "rtp{}depay".format(codec),
        "{}parse".format(codec),
    ]
    try:
        return all(
            subprocess.run(
                ["gst-inspect-1.0", plugin],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            ).returncode
            == 0
            for plugin in plugins
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def is_jetson_platform():
    if not sys.platform.startswith("linux") or platform.machine().lower() not in {"aarch64", "arm64"}:
        return False
    return os.path.exists("/etc/nv_tegra_release") or os.path.exists("/etc/nv_boot_control.conf")


def redact_url(url):
    parsed = urlparse(url)
    if not parsed.password:
        return url
    host = parsed.hostname or ""
    if parsed.port:
        host += ":{}".format(parsed.port)
    user = parsed.username or ""
    return parsed._replace(netloc="{}:***@{}".format(user, host)).geturl()


async def upload_loop(args, shared):
    reconnects = 0
    backoff = 1.0
    while not shared.stop.is_set():
        # cpolar 会过滤 WebSocket 的自定义鉴权头，因此保留请求头的同时，
        # 通过 WSS 加密查询参数传递令牌作为兼容回退。
        url = "{}/ws/ingest/{}?token={}".format(
            args.cloud.rstrip("/"), args.vehicle_id, quote(args.token, safe="")
        )
        try:
            async with websockets.connect(
                url,
                extra_headers={"X-Vehicle-Token": args.token},
                max_size=args.max_frame_bytes,
                compression=None,
                # 32 KB 小于当前单张高清 JPEG；公网 RTT 较高时每帧都会等待
                # transport drain，吞吐会退化到约 1 Mbps。
                write_limit=args.websocket_write_limit_bytes,
                open_timeout=10,
                ping_interval=5,
                ping_timeout=15,
                close_timeout=3,
            ) as socket:
                LOGGER.info("cloud_connected vehicle_id=%s cloud=%s", args.vehicle_id, args.cloud)
                backoff = 1.0
                # 跨连接不回放旧画面；连接建立后才启用有界 FIFO 缓冲。
                with shared.lock:
                    shared.packet_queue.clear()
                    shared.queue_dropped_frames = 0
                    if shared.packet is not None:
                        shared.packet_queue.append((shared.sequence, shared.packet))
                sent_bytes = 0
                sent_frames = 0
                stats_started = time.monotonic()
                # 等待完整统计窗口后再上报，避免首帧用约 1ms 分母算出
                # 1000 FPS / 数百 Mbps 的虚假瞬时值。
                next_telemetry = stats_started + 1.0
                next_frame_send_at = time.monotonic()
                while not shared.stop.is_set():
                    now = time.monotonic()
                    with shared.lock:
                        if now >= next_frame_send_at and shared.packet_queue:
                            sequence, packet = shared.packet_queue.popleft()
                        else:
                            sequence, packet = None, None
                        capture_stats = {
                            "source_status": shared.source_status,
                            "source_fps": round(shared.source_fps, 1),
                            "encoded_fps": round(shared.encoded_fps, 1),
                            "width": shared.width,
                            "height": shared.height,
                            "jpeg_quality": shared.quality,
                            "capture_reconnects": shared.capture_reconnects,
                            "capture_failures": shared.capture_failures,
                            "capture_backend": shared.capture_backend,
                            "send_queue_depth": len(shared.packet_queue),
                            "send_queue_capacity": shared.packet_queue.maxlen,
                            "send_queue_seconds": round(
                                len(shared.packet_queue) / float(max(1, args.fps)), 2
                            ),
                            "buffer_limit_seconds": args.buffer_seconds,
                            "queue_dropped_frames": shared.queue_dropped_frames,
                            "stale_dropped_frames": shared.stale_dropped_frames,
                            "websocket_write_limit_bytes": args.websocket_write_limit_bytes,
                        }
                    if packet is not None:
                        try:
                            packet_age_ms = now_ms() - unpack_frame(packet)["captured_at_ms"]
                        except ValueError:
                            packet_age_ms = args.max_frame_age_seconds * 1000 + 1
                        if packet_age_ms > args.max_frame_age_seconds * 1000:
                            with shared.lock:
                                shared.stale_dropped_frames += 1
                            packet = None
                    if packet is not None:
                        # 某些反向代理半断开后 TCP 写入会长时间挂起。
                        # 超时会进入外层重连逻辑，而不是让页面永远停在最后一帧。
                        await asyncio.wait_for(
                            socket.send(packet), timeout=args.send_timeout
                        )
                        sent_bytes += len(packet)
                        sent_frames += 1
                        # 固定帧率节拍优先，带宽限制只在单帧过大时进一步放慢。
                        # 不追赶已经错过的时间片，避免公网隧道瞬间成批发送。
                        send_interval = 1.0 / max(1, args.fps)
                        if args.target_kbps > 0:
                            bandwidth_interval = len(packet) * 8 / float(args.target_kbps * 1000)
                            send_interval = max(send_interval, bandwidth_interval)
                        sent_at = time.monotonic()
                        next_frame_send_at = max(
                            next_frame_send_at + send_interval,
                            sent_at + send_interval * 0.5,
                        )

                    now = time.monotonic()
                    if now >= next_telemetry:
                        elapsed = max(0.001, now - stats_started)
                        telemetry = {
                            "type": "telemetry",
                            "schema_version": 1,
                            "vehicle_id": args.vehicle_id,
                            "fps": round(sent_frames / elapsed, 1),
                            "upload_kbps": round(sent_bytes * 8 / elapsed / 1000, 1),
                            "agent_reconnects": reconnects,
                            "transport": "WebSocket / JPEG",
                            "playback_mode": "quality-buffered",
                            "clock_epoch_ms": now_ms(),
                            "domains": {
                                "video": {"status": "active", "source": redact_url(args.source)},
                                "chassis": {"status": "reserved"},
                                "powertrain": {"status": "reserved"},
                                "body": {"status": "reserved"},
                                "cockpit": {"status": "reserved"},
                                "adas": {"status": "reserved"},
                            },
                        }
                        navigation = read_navigation_file(args.navigation_file)
                        if navigation:
                            telemetry["navigation"] = navigation
                        telemetry.update(capture_stats)
                        await asyncio.wait_for(
                            socket.send(json.dumps(telemetry, ensure_ascii=False)),
                            timeout=args.send_timeout,
                        )
                        next_telemetry = now + 1.0
                        if elapsed >= 2.0:
                            sent_bytes = 0
                            sent_frames = 0
                            stats_started = now
                    await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            reconnects += 1
            LOGGER.warning("cloud_disconnected retry_in=%.1fs error=%s", backoff, exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)


def parse_args():
    parser = argparse.ArgumentParser(description="Vehicle RTSP to cloud uploader")
    parser.add_argument("--source", default=os.getenv("RTSP_URL", "rtsp://192.168.4.88:8554/main"))
    parser.add_argument("--cloud", default=os.getenv("CLOUD_WS_URL", "ws://127.0.0.1:8000"))
    parser.add_argument("--vehicle-id", default=os.getenv("VEHICLE_ID", "VHC-001"))
    parser.add_argument("--token", default=os.getenv("VEHICLE_INGEST_TOKEN", "change-me-in-production"))
    parser.add_argument(
        "--navigation-file",
        default=os.getenv("VEHICLE_NAVIGATION_FILE", ""),
        help="JSON file containing the latest GNSS position and vehicle speed",
    )
    parser.add_argument("--width", type=int, default=int(os.getenv("VIDEO_MAX_WIDTH", "1920")))
    parser.add_argument("--fps", type=int, default=int(os.getenv("VIDEO_FPS", "25")))
    parser.add_argument("--quality", type=int, default=int(os.getenv("JPEG_QUALITY", "88")))
    parser.add_argument("--target-kbps", type=int, default=int(os.getenv("TARGET_KBPS", "12000")))
    parser.add_argument(
        "--buffer-seconds",
        type=float,
        default=float(os.getenv("VIDEO_BUFFER_SECONDS", "6")),
        help="maximum vehicle-side FIFO duration; capped at 18 seconds",
    )
    parser.add_argument(
        "--websocket-write-limit-bytes",
        type=int,
        default=int(os.getenv("WS_WRITE_LIMIT_BYTES", str(1024 * 1024))),
        help="outbound WebSocket high-water mark for high-RTT links",
    )
    parser.add_argument(
        "--max-frame-age-seconds",
        type=float,
        default=float(os.getenv("MAX_FRAME_AGE_SECONDS", "18")),
        help="drop queued frames older than this before upload",
    )
    parser.add_argument("--ffmpeg", default=os.getenv("FFMPEG_PATH", ""))
    parser.add_argument(
        "--backend",
        choices=["auto", "jetson", "ffmpeg", "opencv"],
        default=os.getenv("VIDEO_BACKEND", "auto"),
    )
    parser.add_argument(
        "--rtsp-codec",
        choices=["h264", "h265"],
        default=os.getenv("RTSP_CODEC", "h264"),
    )
    parser.add_argument("--max-frame-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument(
        "--send-timeout",
        type=float,
        default=float(os.getenv("WS_SEND_TIMEOUT", "3")),
        help="seconds before a stalled WebSocket send forces reconnection",
    )
    parser.add_argument(
        "--rtsp-open-timeout-ms",
        type=int,
        default=int(os.getenv("RTSP_OPEN_TIMEOUT_MS", "5000")),
    )
    parser.add_argument(
        "--rtsp-read-timeout-ms",
        type=int,
        default=int(os.getenv("RTSP_READ_TIMEOUT_MS", "5000")),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    args.buffer_seconds = max(0.0, min(18.0, args.buffer_seconds))
    args.max_frame_age_seconds = max(2.0, min(20.0, args.max_frame_age_seconds))
    args.websocket_write_limit_bytes = max(
        64 * 1024, min(4 * 1024 * 1024, args.websocket_write_limit_bytes)
    )
    queue_capacity = max(1, int(round(max(1, args.fps) * args.buffer_seconds)))
    shared = SharedFrame(
        quality=max(MIN_JPEG_QUALITY, min(96, args.quality)),
        queue_capacity=queue_capacity,
    )
    use_jetson = args.source != "synthetic" and (
        args.backend == "jetson" or (args.backend == "auto" and is_jetson_platform())
    )
    use_ffmpeg = args.source != "synthetic" and args.backend in {"auto", "ffmpeg"} and (
        args.ffmpeg and os.path.isfile(args.ffmpeg)
    )
    if use_jetson:
        worker_args = (
            args.source,
            shared,
            args.width,
            args.fps,
            shared.quality,
            args.target_kbps,
            args.rtsp_codec,
        )
        if not opencv_has_gstreamer() and jetson_cli_pipeline_available(args.rtsp_codec):
            worker = JetsonGStreamerProcessWorker(
                *worker_args,
                open_timeout_ms=args.rtsp_open_timeout_ms,
                read_timeout_ms=args.rtsp_read_timeout_ms,
            )
        else:
            worker = JetsonCaptureWorker(*worker_args)
            worker.open_timeout_ms = args.rtsp_open_timeout_ms
            worker.read_timeout_ms = args.rtsp_read_timeout_ms
    elif use_ffmpeg:
        worker = FFmpegCaptureWorker(
            args.ffmpeg, args.source, shared, args.width, args.fps, shared.quality
        )
    else:
        worker = CaptureWorker(
            args.source,
            shared,
            args.width,
            args.fps,
            shared.quality,
            args.target_kbps,
            args.rtsp_open_timeout_ms,
            args.rtsp_read_timeout_ms,
        )
    worker.start()
    try:
        asyncio.run(upload_loop(args, shared))
    except KeyboardInterrupt:
        LOGGER.info("shutdown_requested")
    finally:
        shared.stop.set()
        worker.join(timeout=3)


if __name__ == "__main__":
    main()
