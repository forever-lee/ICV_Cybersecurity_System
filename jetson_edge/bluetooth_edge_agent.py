#!/usr/bin/env python3
"""Receive Bluetooth gateway UDP packets and reliably forward them to the cloud."""

import json
import os
import queue
import re
import signal
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEDUP_WINDOW_SECONDS = 10.0
S32_SOURCE_TIME_PATTERN = re.compile(r"(?:^|;)TS=(\d{13})(?=;|$)")
S32_COMPACT_TIME_PATTERN = re.compile(r"^(S32K344_HELLO_\d{2})[0-9A-Za-z_-]{3}$")
S32_SHORT_ABNORMAL_PATTERN = re.compile(r"^S32K344_ABNORMAL_(\d{2})$")
S32_NORMAL_PATTERN = re.compile(r"^S32K344_HELLO_\d{2,4}$")
S32_ABNORMAL_PATTERN = re.compile(
    r"^S32K344_ABNORMAL_\d{4};TYPE=BLE_ATTACK;LEVEL=HIGH$"
)


def required_environment(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit("缺少环境变量 {}".format(name))
    return value


def integer_environment(name, default, minimum=1, maximum=65535):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise SystemExit("环境变量 {} 必须是整数".format(name))
    if value < minimum or value > maximum:
        raise SystemExit("环境变量 {} 超出范围".format(name))
    return value


def clean_text(value):
    return str(value).replace("\x00", "").strip().rstrip("#").strip()


def canonical_telemetry_text(text):
    """Accept only complete S32K344 telemetry, not bridge diagnostics or hex dumps."""
    text = clean_text(text)
    compact = S32_COMPACT_TIME_PATTERN.fullmatch(text)
    if compact:
        text = compact.group(1)
    else:
        text = S32_SOURCE_TIME_PATTERN.sub("", text).strip(";")
    short_abnormal = S32_SHORT_ABNORMAL_PATTERN.fullmatch(text)
    if short_abnormal:
        text = "S32K344_ABNORMAL_{:04d};TYPE=BLE_ATTACK;LEVEL=HIGH".format(
            int(short_abnormal.group(1))
        )
    if S32_NORMAL_PATTERN.fullmatch(text) or S32_ABNORMAL_PATTERN.fullmatch(text):
        return text
    return None


def is_recent_duplicate(cache, source, text, timestamp):
    key = (source, canonical_telemetry_text(text))
    previous = cache.get(key)
    cache[key] = timestamp
    if len(cache) > 512:
        cutoff = timestamp - DEDUP_WINDOW_SECONDS
        for stale_key, seen_at in list(cache.items()):
            if seen_at < cutoff:
                cache.pop(stale_key, None)
    return previous is not None and timestamp - previous < DEDUP_WINDOW_SECONDS


def decode_datagram(data, address, source_prefix="S32K344-ETH"):
    """Convert one UDP datagram into one or more cloud payloads."""
    decoded = data.decode("utf-8", errors="ignore").strip()
    if not decoded:
        return []
    source = "{}@{}:{}".format(source_prefix, address[0], address[1])
    try:
        envelope = json.loads(decoded)
    except (TypeError, ValueError):
        envelope = None

    if isinstance(envelope, dict):
        text = clean_text(envelope.get("text", envelope.get("data", "")))
        if not canonical_telemetry_text(text):
            return []
        payload = dict(envelope)
        payload["text"] = text
        payload.setdefault("source", source)
        payload.setdefault("byte_count", len(text.encode("utf-8")))
        return [payload]

    payloads = []
    for frame in decoded.replace("\r", "\n").split("#"):
        for line in frame.splitlines():
            text = clean_text(line)
            if not canonical_telemetry_text(text):
                continue
            payloads.append({
                "text": text,
                "source": source,
                "byte_count": len(text.encode("utf-8")),
            })
    return payloads


class CloudUploader(threading.Thread):
    def __init__(self, endpoint, token, pending, stop_event, timeout_seconds):
        super().__init__(name="bluetooth-cloud-uploader")
        self.endpoint = endpoint
        self.token = token
        self.pending = pending
        self.stop_event = stop_event
        self.timeout_seconds = timeout_seconds

    def upload(self, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": "Bearer {}".format(self.token),
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            response.read()
            if response.status < 200 or response.status >= 300:
                raise RuntimeError("HTTP {}".format(response.status))

    def run(self):
        while not self.stop_event.is_set() or not self.pending.empty():
            try:
                payload = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            retry_seconds = 1.0
            while not self.stop_event.is_set():
                try:
                    self.upload(payload)
                    print("[蓝牙上传成功] {}".format(payload.get("text", "")), flush=True)
                    break
                except (HTTPError, URLError, OSError, RuntimeError) as exc:
                    print(
                        "[蓝牙上传重试] {}，{:.0f} 秒后重试".format(exc, retry_seconds),
                        flush=True,
                    )
                    if self.stop_event.wait(retry_seconds):
                        break
                    retry_seconds = min(retry_seconds * 2, 30.0)
            self.pending.task_done()


def main():
    cloud_url = required_environment("CLOUD_HTTP_URL").rstrip("/")
    vehicle_id = required_environment("VEHICLE_ID")
    token = required_environment("VEHICLE_INGEST_TOKEN")
    udp_host = os.getenv("BLUETOOTH_UDP_HOST", "0.0.0.0").strip() or "0.0.0.0"
    udp_port = integer_environment("BLUETOOTH_UDP_PORT", 5000)
    queue_size = integer_environment("BLUETOOTH_QUEUE_SIZE", 1000, 1, 100000)
    timeout_seconds = float(os.getenv("BLUETOOTH_HTTP_TIMEOUT_SECONDS", "5"))
    source_prefix = os.getenv("BLUETOOTH_SOURCE_PREFIX", "S32K344-ETH").strip() or "S32K344-ETH"
    endpoint = "{}/api/vehicles/{}/bluetooth".format(cloud_url, quote(vehicle_id, safe=""))

    stop_event = threading.Event()

    def request_stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    pending = queue.Queue(maxsize=queue_size)
    recent_frames = {}
    uploader = CloudUploader(endpoint, token, pending, stop_event, timeout_seconds)
    uploader.start()

    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_socket.bind((udp_host, udp_port))
    udp_socket.settimeout(1.0)
    print(
        "蓝牙边缘代理已启动：UDP {}:{} -> {}，车辆 {}".format(
            udp_host, udp_port, cloud_url, vehicle_id
        ),
        flush=True,
    )
    try:
        while not stop_event.is_set():
            try:
                data, address = udp_socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if stop_event.is_set():
                    break
                raise
            for payload in decode_datagram(data, address, source_prefix):
                if is_recent_duplicate(
                    recent_frames, payload["source"], payload["text"], time.monotonic()
                ):
                    continue
                try:
                    pending.put(payload, timeout=1.0)
                except queue.Full:
                    print("[蓝牙队列已满] 暂停接收，等待云端恢复", flush=True)
                    while not stop_event.is_set():
                        try:
                            pending.put(payload, timeout=1.0)
                            break
                        except queue.Full:
                            continue
    finally:
        stop_event.set()
        udp_socket.close()
        uploader.join(timeout=timeout_seconds + 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
