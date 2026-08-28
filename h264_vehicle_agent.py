"""Vehicle-side RTSP to H.264 fragmented-MP4 cloud uploader."""

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import quote

import websockets

from stream_protocol import FMP4_KIND_INIT, FMP4_KIND_MEDIA, now_ms, pack_fmp4


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("h264-vehicle-agent")


def read_navigation_file(path):
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
class SharedStream:
    init_segment: bytes = None
    # Encoding runs in its own thread, so network sends never block FFmpeg
    # stdout draining. The queue length is replaced from CLI settings in main.
    fragments: deque = field(default_factory=lambda: deque(maxlen=40))
    sequence: int = 0
    encoded_fragments: int = 0
    encoded_bytes: int = 0
    dropped_fragments: int = 0
    capture_reconnects: int = 0
    source_status: str = "starting"
    encoder: str = ""
    pipeline: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)


class Mp4BoxParser:
    """Incrementally groups ftyp+moov and moof+mdat from FFmpeg stdout."""

    def __init__(self, on_init, on_fragment):
        self.buffer = bytearray()
        self.init_parts = []
        self.fragment_parts = []
        self.seen_moof = False
        self.on_init = on_init
        self.on_fragment = on_fragment

    def feed(self, data):
        self.buffer.extend(data)
        while True:
            if len(self.buffer) < 8:
                return
            size = struct.unpack_from(">I", self.buffer, 0)[0]
            header_size = 8
            if size == 1:
                if len(self.buffer) < 16:
                    return
                size = struct.unpack_from(">Q", self.buffer, 8)[0]
                header_size = 16
            if size == 0 or size < header_size or size > 64 * 1024 * 1024:
                raise ValueError("invalid ISO-BMFF box size")
            if len(self.buffer) < size:
                return
            box = bytes(self.buffer[:size])
            del self.buffer[:size]
            box_type = box[4:8]
            if box_type in (b"ftyp", b"moov") and not self.seen_moof:
                self.init_parts.append(box)
                if box_type == b"moov":
                    self.on_init(b"".join(self.init_parts))
                    self.init_parts = []
                continue
            if box_type == b"moof":
                if self.fragment_parts and self.seen_moof:
                    self.fragment_parts = []
                self.seen_moof = True
                self.fragment_parts.append(box)
                continue
            if self.seen_moof:
                self.fragment_parts.append(box)
                if box_type == b"mdat":
                    self.on_fragment(b"".join(self.fragment_parts))
                    self.fragment_parts = []
                    self.seen_moof = False


def ffmpeg_executable(value):
    if value and os.path.isfile(value):
        return value
    found = shutil.which(value or "ffmpeg")
    if not found:
        raise RuntimeError("FFmpeg not found")
    return found


def gst_launch_executable(value):
    if value and os.path.isfile(value):
        return value
    found = shutil.which(value or "gst-launch-1.0")
    if not found:
        raise RuntimeError("gst-launch-1.0 not found")
    return found


def gst_encoder_properties():
    """Return nvv4l2h264enc properties exposed by the installed JetPack."""
    inspector = shutil.which("gst-inspect-1.0")
    if not inspector:
        return set()
    try:
        output = subprocess.check_output(
            [inspector, "nvv4l2h264enc"], stderr=subprocess.STDOUT
        ).decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(
        match.group(1).lower()
        for match in re.finditer(r"^\s*([A-Za-z][A-Za-z0-9_-]*)\s*:", output, re.MULTILINE)
    )


def encoder_pid_file(source):
    source_hash = hashlib.sha1(source.encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), "vshield-ffmpeg-{}.pid".format(source_hash))


def acquire_instance_lock(source):
    """Allow only one H.264 capture process per RTSP source."""
    path = encoder_pid_file(source) + ".lock"
    handle = None
    try:
        handle = open(path, "a+b")
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        if handle is not None:
            handle.close()
        return None
    return handle


def cleanup_stale_encoder(source):
    """Stop an FFmpeg child orphaned by a forced IDE/PyCharm termination."""
    path = encoder_pid_file(source)
    try:
        with open(path, "r", encoding="ascii") as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return
    try:
        if os.name == "nt":
            listing = subprocess.check_output(
                ["tasklist", "/FI", "PID eq {}".format(pid), "/FO", "CSV", "/NH"],
                stderr=subprocess.STDOUT,
            ).decode("utf-8", "replace").lower()
            if "ffmpeg.exe" in listing:
                LOGGER.warning("stale_encoder_cleanup pid=%s", pid)
                subprocess.call(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        else:
            command_path = "/proc/{}/cmdline".format(pid)
            with open(command_path, "rb") as handle:
                command = handle.read().decode("utf-8", "replace")
            if ("ffmpeg" in command or "gst-launch" in command) and source in command:
                os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        os.remove(path)
    except OSError:
        pass


def record_encoder_pid(source, pid):
    path = encoder_pid_file(source)
    try:
        with open(path, "w", encoding="ascii") as handle:
            handle.write(str(pid))
    except OSError:
        pass


def clear_encoder_pid(source, pid):
    path = encoder_pid_file(source)
    try:
        with open(path, "r", encoding="ascii") as handle:
            recorded = int(handle.read().strip())
        if recorded == pid:
            os.remove(path)
    except (OSError, ValueError):
        pass


def build_command(args, encoder, gpu_pipeline=False):
    executable = ffmpeg_executable(args.ffmpeg)
    is_lavfi = args.source.startswith("lavfi:")
    gpu_pipeline = bool(gpu_pipeline and encoder == "h264_nvenc" and not is_lavfi)
    if is_lavfi:
        source = ["-f", "lavfi", "-re", "-i", args.source[6:]]
    else:
        source = ["-rtsp_transport", "tcp", "-timeout", "15000000"]
        if gpu_pipeline:
            # Keep decoded frames on the GPU through scaling and NVENC. FFmpeg
            # selects H.264 or HEVC NVDEC from the actual RTSP input codec.
            source += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        source += ["-i", args.source]
    base = [executable, "-hide_banner", "-loglevel", "warning"] + source + ["-an", "-sn", "-dn"]
    # A one-second GOP keeps every fragment independently decodable while
    # avoiding the severe quality loss caused by the previous 250 ms GOP.
    gop_frames = max(1, int(round(args.fps * args.segment_ms / 1000.0)))
    if encoder == "h264_nvenc":
        if gpu_pipeline:
            video_filter = "fps={0},scale_cuda={1}:-2:format=nv12:interp_algo=bilinear".format(
                args.fps, args.width
            )
        else:
            video_filter = "fps={0},scale={1}:-2:flags=bilinear,format=nv12".format(
                args.fps, args.width
            )
        video = [
            "-vf", video_filter,
            "-c:v", "h264_nvenc", "-preset", "p7", "-tune", "hq", "-profile:v", "high", "-level:v", "4.1",
            "-rc", "cbr", "-b:v", "{}k".format(args.bitrate_kbps),
            "-maxrate", "{}k".format(args.bitrate_kbps),
            "-bufsize", "{}k".format(args.bitrate_kbps * 2),
            "-multipass", "fullres", "-rc-lookahead", str(max(20, args.fps)),
            "-spatial-aq", "1", "-temporal-aq", "1", "-aq-strength", "8",
            "-bf", "3", "-b_ref_mode", "middle",
            "-g", str(gop_frames), "-keyint_min", str(gop_frames), "-forced-idr", "1",
        ]
    else:
        video = [
            "-vf", "fps={0},scale={1}:-2:flags=lanczos,format=yuv420p".format(args.fps, args.width),
            "-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-level:v", "4.1",
            "-b:v", "{}k".format(args.bitrate_kbps),
            "-maxrate", "{}k".format(args.bitrate_kbps),
            "-bufsize", "{}k".format(args.bitrate_kbps * 2), "-bf", "3",
            "-g", str(gop_frames), "-keyint_min", str(gop_frames), "-sc_threshold", "0",
        ]
    mux = [
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
        "-frag_duration", str(args.segment_ms * 1000),
        "-video_track_timescale", "90000", "-f", "mp4", "pipe:1",
    ]
    return base + video + mux


def build_jetson_gstreamer_command(args, output_fd=1):
    """Build a TX2 NVDEC/VIC/NVENC pipeline that emits fragmented MP4."""
    executable = gst_launch_executable(args.gst_launch)
    codec = "h265" if args.rtsp_codec in ("h265", "hevc") else "h264"
    depay = "rtph265depay" if codec == "h265" else "rtph264depay"
    parser = "h265parse" if codec == "h265" else "h264parse"
    height = int(round(args.width * 9 / 16))
    gop_frames = max(1, int(round(args.fps * args.segment_ms / 1000.0)))
    properties = gst_encoder_properties()

    encoder = [
        "nvv4l2h264enc",
        "bitrate={}".format(args.bitrate_kbps * 1000),
        # JetPack 4 / TX2 exposes 0=VBR and 1=CBR.
        "control-rate=1",
        "iframeinterval={}".format(gop_frames),
        "idrinterval={}".format(gop_frames),
        "insert-sps-pps=true",
    ]
    optional_properties = (
        ("profile", "profile=4"),              # High profile
        ("preset-level", "preset-level=4"),  # Slow/high-quality preset
        ("maxperf-enable", "maxperf-enable=true"),
        ("enabletwopasscbr", "EnableTwopassCBR=true"),
    )
    for property_name, assignment in optional_properties:
        if property_name in properties:
            encoder.append(assignment)

    return [
        executable,
        "-q",
        "-e",
        "rtspsrc",
        "location={}".format(args.source),
        "protocols=tcp",
        # A deeper RTSP jitter buffer favors continuous playback over latency.
        "latency=500",
        "drop-on-latency=false",
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
        "video/x-raw,width={},height={},format=I420".format(args.width, height),
        "!",
        "videorate",
        "!",
        "video/x-raw,format=I420,framerate={}/1".format(args.fps),
        "!",
        "nvvidconv",
        "!",
        "video/x-raw(memory:NVMM),format=NV12,width={},height={}".format(
            args.width, height
        ),
        "!",
    ] + encoder + [
        "!",
        "h264parse",
        "config-interval=-1",
        "!",
        "video/x-h264,stream-format=avc,alignment=au",
        "!",
        "mp4mux",
        "fragment-duration={}".format(args.segment_ms),
        "streamable=true",
        "!",
        "fdsink",
        "fd={}".format(output_fd),
        "sync=false",
    ]


def capture_loop(shared, args):
    if args.encoder == "jetson":
        pipelines = [("jetson", False, "NVDEC/VIC/nvv4l2h264enc")]
    elif args.encoder == "auto":
        pipelines = [
            ("h264_nvenc", True, "NVDEC/CUDA/NVENC"),
            ("h264_nvenc", False, "software-decode/NVENC"),
            ("libx264", False, "software/libx264"),
        ]
    elif args.encoder == "h264_nvenc":
        pipelines = [
            ("h264_nvenc", True, "NVDEC/CUDA/NVENC"),
            ("h264_nvenc", False, "software-decode/NVENC"),
        ]
    else:
        pipelines = [("libx264", False, "software/libx264")]
    if args.source.startswith("lavfi:"):
        pipelines = [item for item in pipelines if not item[1]]
    pipeline_index = 0
    while not shared.stop.is_set():
        encoder, gpu_pipeline, pipeline_name = pipelines[pipeline_index]
        LOGGER.info("encoder_start pipeline=%s source=%s", pipeline_name, args.source)
        with shared.lock:
            fragments_before = shared.encoded_fragments

        if encoder == "jetson":
            # Some JetPack 4 NVIDIA libraries print nvbuf_utils diagnostics to
            # stdout.  Keep fragmented MP4 on a dedicated inherited FD so
            # those messages can never corrupt the binary media stream.
            media_read_fd, media_write_fd = os.pipe()
            command = build_jetson_gstreamer_command(args, media_write_fd)
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    pass_fds=(media_write_fd,),
                )
            except Exception:
                os.close(media_read_fd)
                os.close(media_write_fd)
                raise
            os.close(media_write_fd)
            media_stream = os.fdopen(media_read_fd, "rb", buffering=0)
        else:
            command = build_command(args, encoder, gpu_pipeline)
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            media_stream = process.stdout

        record_encoder_pid(args.source, process.pid)
        stderr_lines = deque(maxlen=12)

        def read_stderr():
            for raw in iter(process.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    stderr_lines.append(line)

        threading.Thread(target=read_stderr, daemon=True).start()

        def on_init(payload):
            with shared.lock:
                shared.init_segment = payload
                shared.fragments.clear()
                shared.source_status = "online"
                shared.encoder = "nvv4l2h264enc" if encoder == "jetson" else encoder
                shared.pipeline = pipeline_name

        def on_fragment(payload):
            created = now_ms()
            with shared.lock:
                shared.sequence = (shared.sequence + 1) & 0xFFFFFFFF
                if len(shared.fragments) == shared.fragments.maxlen:
                    shared.dropped_fragments += 1
                shared.fragments.append((shared.sequence, created, payload))
                shared.encoded_fragments += 1
                shared.encoded_bytes += len(payload)

        parser = Mp4BoxParser(on_init, on_fragment)
        try:
            while not shared.stop.is_set():
                chunk = media_stream.read(65536)
                if not chunk:
                    break
                parser.feed(chunk)
        except Exception:
            LOGGER.exception("encoder_output_failure")
        finally:
            try:
                media_stream.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        clear_encoder_pid(args.source, process.pid)
        with shared.lock:
            shared.source_status = "reconnecting"
            shared.capture_reconnects += 1
        if shared.stop.is_set():
            break
        stderr_text = " | ".join(stderr_lines)
        with shared.lock:
            produced_media = shared.encoded_fragments > fragments_before
        if produced_media:
            # A normal RTSP reconnect should retry the best GPU path rather
            # than permanently degrading after a temporary source outage.
            pipeline_index = 0
            LOGGER.warning("encoder_stopped retry_in=2s error=%s", stderr_text[-800:])
            time.sleep(2)
        elif pipeline_index + 1 < len(pipelines):
            previous = pipeline_name
            pipeline_index += 1
            LOGGER.warning(
                "pipeline_unavailable current=%s fallback=%s error=%s",
                previous, pipelines[pipeline_index][2], stderr_text[-800:],
            )
        else:
            # Retry the preferred path later so GPU recovery does not require
            # restarting the edge agent.
            pipeline_index = 0
            LOGGER.warning("all_pipelines_failed retry_in=2s error=%s", stderr_text[-800:])
            time.sleep(2)


async def upload_loop(shared, args):
    reconnect_delay = 1.0
    reconnects = 0
    while not shared.stop.is_set():
        url = "{}/ws/ingest-fmp4/{}?token={}".format(
            args.cloud.rstrip("/"), quote(args.vehicle_id, safe=""), quote(args.token, safe="")
        )
        try:
            async with websockets.connect(
                url, max_size=None, max_queue=2, ping_interval=20, ping_timeout=20,
                close_timeout=3, write_limit=1024 * 1024,
            ) as socket:
                LOGGER.info("cloud_connected vehicle_id=%s", args.vehicle_id)
                reconnect_delay = 1.0
                last_init = None
                last_sequence = None
                sent_bytes = 0
                sent_fragments = 0
                stats_at = time.monotonic()
                next_telemetry = stats_at + 1.0
                while not shared.stop.is_set():
                    with shared.lock:
                        init_segment = shared.init_segment
                        fragments = list(shared.fragments)
                        source_status = shared.source_status
                        encoder = shared.encoder
                        pipeline = shared.pipeline
                        dropped = shared.dropped_fragments
                        capture_reconnects = shared.capture_reconnects
                    if init_segment and init_segment is not last_init:
                        await asyncio.wait_for(
                            socket.send(pack_fmp4(FMP4_KIND_INIT, 0, now_ms(), init_segment)),
                            timeout=args.send_timeout,
                        )
                        last_init = init_segment
                        last_sequence = None
                    if last_init and fragments:
                        available = [item for item in fragments if last_sequence is None or item[0] > last_sequence]
                        max_pending = shared.fragments.maxlen
                        # Quality-first mode deliberately replays every retained
                        # fragment after an uplink interruption.  Preserve FIFO
                        # order instead of jumping to the newest segment.
                        if len(available) > max_pending:
                            available = available[-max_pending:]
                        for sequence, created, payload in available:
                            packet = pack_fmp4(FMP4_KIND_MEDIA, sequence, created, payload)
                            await asyncio.wait_for(socket.send(packet), timeout=args.send_timeout)
                            last_sequence = sequence
                            sent_bytes += len(packet)
                            sent_fragments += 1
                    now = time.monotonic()
                    if now >= next_telemetry:
                        elapsed = max(0.001, now - stats_at)
                        telemetry = {
                            "type": "fmp4_telemetry", "transport": "WebSocket / H.264 fMP4",
                            "codec": "H.264", "encoder": encoder, "pipeline": pipeline,
                            "width": args.width,
                            "height": int(round(args.width * 9 / 16)), "encoded_fps": args.fps,
                            "fps": args.fps, "bitrate_kbps": args.bitrate_kbps,
                            "upload_kbps": round(sent_bytes * 8 / elapsed / 1000, 1),
                            "sent_fragments": sent_fragments,
                            "segment_seconds": round(args.segment_ms / 1000.0, 3),
                            "queue_dropped_fragments": dropped,
                            "queue_seconds": round(
                                sum(1 for item in fragments if last_sequence is None or item[0] > last_sequence)
                                * args.segment_ms / 1000.0,
                                2,
                            ),
                            "source_status": source_status,
                            "capture_reconnects": capture_reconnects, "agent_reconnects": reconnects,
                        }
                        navigation = read_navigation_file(args.navigation_file)
                        if navigation:
                            telemetry["navigation"] = navigation
                        await asyncio.wait_for(
                            socket.send(json.dumps(telemetry, ensure_ascii=False)),
                            timeout=args.send_timeout,
                        )
                        sent_bytes = 0
                        sent_fragments = 0
                        stats_at = now
                        next_telemetry = now + 1.0
                    await asyncio.sleep(0.02)
        except Exception as exc:
            reconnects += 1
            LOGGER.warning("cloud_disconnected retry_in=%.1fs error=%s", reconnect_delay, exc)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(15.0, reconnect_delay * 2)


def parse_args():
    parser = argparse.ArgumentParser(description="Vehicle RTSP H.264/fMP4 uploader")
    parser.add_argument("--source", default=os.getenv("RTSP_URL", "rtsp://192.168.4.88:8554/main"))
    parser.add_argument("--cloud", default=os.getenv("CLOUD_WS_URL", "ws://127.0.0.1:8000"))
    parser.add_argument("--vehicle-id", default=os.getenv("VEHICLE_ID", "VHC-001"))
    parser.add_argument("--token", default=os.getenv("VEHICLE_INGEST_TOKEN", "change-me-in-production"))
    parser.add_argument("--navigation-file", default=os.getenv("VEHICLE_NAVIGATION_FILE", ""))
    parser.add_argument("--ffmpeg", default=os.getenv("FFMPEG_PATH", "ffmpeg"))
    parser.add_argument(
        "--encoder", choices=("auto", "jetson", "h264_nvenc", "libx264"), default="auto"
    )
    parser.add_argument("--gst-launch", default=os.getenv("GST_LAUNCH_PATH", "gst-launch-1.0"))
    parser.add_argument(
        "--rtsp-codec", choices=("h264", "h265"), default=os.getenv("RTSP_CODEC", "h265")
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--bitrate-kbps", type=int, default=2800)
    parser.add_argument("--segment-ms", type=int, choices=(250, 500, 1000), default=1000)
    parser.add_argument("--buffer-seconds", type=float, default=300.0)
    parser.add_argument("--send-timeout", type=float, default=60.0)
    return parser.parse_args()


def main():
    args = parse_args()
    instance_lock = acquire_instance_lock(args.source)
    if instance_lock is None:
        raise RuntimeError("another vehicle agent is already reading this RTSP source")
    cleanup_stale_encoder(args.source)
    if args.width < 160 or args.fps < 1 or args.bitrate_kbps < 100:
        raise ValueError("invalid video profile")
    if args.buffer_seconds <= 0 or args.send_timeout <= 0:
        raise ValueError("buffer and timeout values must be positive")
    queue_segments = max(4, int(math.ceil(args.buffer_seconds * 1000 / args.segment_ms)))
    shared = SharedStream(fragments=deque(maxlen=queue_segments))
    capture = threading.Thread(target=capture_loop, args=(shared, args), daemon=True)
    capture.start()
    try:
        if hasattr(asyncio, "run"):
            asyncio.run(upload_loop(shared, args))
        else:  # JetPack 4 / Ubuntu 18.04 commonly ships Python 3.6.
            loop = asyncio.get_event_loop()
            loop.run_until_complete(upload_loop(shared, args))
    except KeyboardInterrupt:
        pass
    finally:
        shared.stop.set()
        capture.join(timeout=5)
        instance_lock.close()


if __name__ == "__main__":
    main()
