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

try:
    from .bluetooth_control_protocol import make_command, parse_ack
    from .bluetooth_time_sync import ClockSynchronizer, parse_response, parse_timed_telemetry
except ImportError:  # Direct execution from jetson_edge/.
    from bluetooth_control_protocol import make_command, parse_ack
    from bluetooth_time_sync import ClockSynchronizer, parse_response, parse_timed_telemetry


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
    timed = parse_timed_telemetry(text)
    if timed is not None:
        return timed["text"]
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


def is_recent_duplicate(cache, source, text, timestamp, sender_sequence=None,
                        source_tick_us=None):
    # A HELLO label can repeat after a sender restart.  Retry detection must
    # use the identity of the timed frame, not only its display text.
    identity = ((sender_sequence, source_tick_us)
                if sender_sequence is not None and source_tick_us is not None
                else canonical_telemetry_text(text))
    key = (source, identity)
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
        base = canonical_telemetry_text(text)
        if not base:
            return []
        payload = {key: value for key, value in envelope.items() if key not in {
            "udp_received_at_ms", "sync_rtt_ms",
            "clock_offset_ms", "latency_quality", "sender_sequence",
            "source_tick_us", "sync_id", "source", "hex",
        }}
        payload["text"] = base
        timed = parse_timed_telemetry(text)
        if timed:
            payload.update({key: timed[key] for key in
                            ("sender_sequence", "source_tick_us", "sync_id")})
        payload["source"] = source
        payload["byte_count"] = len(base.encode("utf-8"))
        return [payload]

    payloads = []
    for frame in decoded.replace("\r", "\n").split("#"):
        for line in frame.splitlines():
            text = clean_text(line)
            base = canonical_telemetry_text(text)
            if not base:
                continue
            payload = {
                "text": base,
                "source": source,
                "byte_count": len(base.encode("utf-8")),
            }
            timed = parse_timed_telemetry(text)
            if timed:
                payload.update({key: timed[key] for key in
                                ("sender_sequence", "source_tick_us", "sync_id")})
            payloads.append(payload)
    return payloads


def decode_ack_datagram(data, address, device_host):
    """Accept only a complete, CRC-valid K344 ACK from the configured board."""
    if address[0] != device_host or not data.startswith(b"BTACK|"):
        raise ValueError("ACK source or type is invalid")
    frame = data.decode("ascii")
    # The existing K344 UART gateway strips '#' before forwarding a frame.
    if not frame.endswith("#"):
        frame += "#"
    return parse_ack(frame), frame


def decode_sync_datagram(data, address, device_host):
    if address[0] != device_host or not data.startswith(b"BTSYNC|"):
        raise ValueError("sync source or type is invalid")
    frame = data.decode("ascii")
    if not frame.endswith("#"):
        frame += "#"
    return parse_response(frame)


class SyncWorker(threading.Thread):
    """Send periodic hardware time probes without stopping while BLE TX is off."""

    def __init__(self, udp_socket, destination, synchronizer, stop_event, interval_seconds):
        super().__init__(name="bluetooth-clock-sync", daemon=True)
        self.udp_socket = udp_socket
        self.destination = destination
        self.synchronizer = synchronizer
        self.stop_event = stop_event
        self.interval_seconds = interval_seconds

    def run(self):
        while not self.stop_event.is_set():
            t1_ms = time.time_ns() // 1000000
            t1_mono_ms = time.monotonic_ns() / 1000000
            frame = self.synchronizer.begin(t1_ms, t1_mono_ms)
            try:
                self.udp_socket.sendto(frame.encode("ascii"), self.destination)
                print("[蓝牙对时请求已下发] {}".format(self.destination[0]), flush=True)
            except OSError as exc:
                self.synchronizer.cancel()
                if not self.stop_event.is_set():
                    print("[蓝牙对时 UDP 下发失败] {}".format(exc), flush=True)
            self.stop_event.wait(self.interval_seconds)


class CloudClockMonitor(threading.Thread):
    """Estimate cloud minus Jetson clock offset without blocking telemetry upload."""

    def __init__(self, cloud_url, stop_event, timeout_seconds, interval_seconds=10):
        super().__init__(name="bluetooth-cloud-clock", daemon=True)
        self.url = cloud_url.rstrip("/") + "/healthz"
        self.stop_event = stop_event
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self.lock = threading.Lock()
        self.samples = []

    def accept(self, cloud_ms, start_epoch_ms, end_epoch_ms,
               start_mono_ms, end_mono_ms):
        rtt_ms = end_mono_ms - start_mono_ms
        if (not isinstance(cloud_ms, (int, float)) or isinstance(cloud_ms, bool) or
                not 0 <= rtt_ms <= 100 or
                abs((end_epoch_ms - start_epoch_ms) - rtt_ms) > 50):
            return False
        sample = {"offset_ms": cloud_ms - (start_epoch_ms + end_epoch_ms) / 2,
                  "rtt_ms": rtt_ms, "sampled_mono_ms": end_mono_ms}
        with self.lock:
            self.samples = [item for item in self.samples
                            if end_mono_ms - item["sampled_mono_ms"] <= 30000]
            self.samples.append(sample)
            self.samples = self.samples[-8:]
        return True

    def latest(self, received_mono_ms):
        with self.lock:
            candidates = [item for item in self.samples
                          if 0 <= received_mono_ms - item["sampled_mono_ms"] <= 30000]
            if not candidates:
                return None
            sample = min(candidates, key=lambda item: item["rtt_ms"])
            return {"cloud_clock_offset_ms": round(sample["offset_ms"], 2),
                    "cloud_clock_rtt_ms": round(sample["rtt_ms"], 2),
                    "cloud_clock_age_ms": round(
                        received_mono_ms - sample["sampled_mono_ms"], 2)}

    def run(self):
        while not self.stop_event.is_set():
            start_epoch_ms = time.time_ns() / 1000000
            start_mono_ms = time.monotonic_ns() / 1000000
            try:
                with urlopen(self.url, timeout=self.timeout_seconds) as response:
                    cloud_ms = json.load(response).get("time_ms")
                end_epoch_ms = time.time_ns() / 1000000
                end_mono_ms = time.monotonic_ns() / 1000000
                self.accept(cloud_ms, start_epoch_ms, end_epoch_ms,
                            start_mono_ms, end_mono_ms)
            except (HTTPError, URLError, OSError, ValueError, AttributeError, TypeError):
                # Clock calibration is optional; never interrupt packet forwarding.
                pass
            self.stop_event.wait(self.interval_seconds)


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


class ControlWorker(threading.Thread):
    """Poll cloud and wait for the laptop's real BLE ACK before final success."""

    RETRY_DELAYS = (0.5, 1.0, 2.0, 2.0, 2.0)

    def __init__(self, endpoint, token, udp_socket, device_host, control_port,
                 received_acks, stop_event, poll_seconds, timeout_seconds,
                 http_timeout_seconds):
        super().__init__(name="bluetooth-control", daemon=True)
        self.endpoint = endpoint
        self.token = token
        self.udp_socket = udp_socket
        self.destination = (device_host, control_port)
        self.received_acks = received_acks
        self.stop_event = stop_event
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.http_timeout_seconds = http_timeout_seconds

    def request(self, method, suffix, payload=None):
        body = None if payload is None else json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request = Request(
            self.endpoint + suffix,
            data=body,
            headers={"Authorization": "Bearer {}".format(self.token),
                     "Content-Type": "application/json; charset=utf-8"},
            method=method,
        )
        with urlopen(request, timeout=self.http_timeout_seconds) as response:
            return json.load(response)

    def run(self):
        current = None
        final_payload = None
        while not self.stop_event.is_set():
            try:
                cloud = self.request("GET", "/next")
                command = cloud.get("command")
            except (HTTPError, URLError, OSError, ValueError) as exc:
                print("[蓝牙控制云端暂不可用] {}".format(exc), flush=True)
                self.stop_event.wait(self.poll_seconds)
                continue

            if final_payload is not None:
                try:
                    self.request("POST", "/ack", final_payload)
                    final_payload = None
                    current = None
                except HTTPError as exc:
                    if exc.code == 409:
                        final_payload = None
                        current = None
                    else:
                        print("[蓝牙控制结果待重传] HTTP {}".format(exc.code), flush=True)
                except (URLError, OSError, ValueError) as exc:
                    print("[蓝牙控制结果待重传] {}".format(exc), flush=True)
                self.stop_event.wait(self.poll_seconds)
                continue

            if not isinstance(command, dict):
                current = None
                self.stop_event.wait(self.poll_seconds)
                continue

            identity = (command.get("id"), command.get("nonce"))
            if current is None or current["identity"] != identity:
                try:
                    frame = make_command(
                        command["id"], command["command"],
                        command["created_at_ms"], command["nonce"]
                    ).encode("ascii")
                except (KeyError, TypeError, ValueError) as exc:
                    print("[蓝牙控制命令无效] {}".format(exc), flush=True)
                    self.stop_event.wait(self.poll_seconds)
                    continue
                current = {"identity": identity, "action": command["command"],
                           "frame": frame, "started": time.monotonic(),
                           "next_send": 0.0, "attempt": 0}

            while True:
                try:
                    ack, raw_frame, device_ack_ms = self.received_acks.get_nowait()
                except queue.Empty:
                    break
                if (ack["id"], ack["nonce"]) != current["identity"]:
                    continue
                if ack["status"] == "applied" and ack["state"] != current["action"]:
                    continue
                final_payload = {
                    "id": ack["id"], "nonce": ack["nonce"],
                    "status": ack["status"], "device_ack": raw_frame,
                    "device_ack_at_ms": device_ack_ms,
                    "message": "发送端确认" if ack["status"] == "applied" else "发送端拒绝",
                }
                break
            if final_payload is not None:
                continue

            now = time.monotonic()
            if now - current["started"] >= self.timeout_seconds:
                final_payload = {"id": current["identity"][0],
                                 "nonce": current["identity"][1],
                                 "status": "expired",
                                 "message": "未收到发送电脑的 BLE ACK"}
                continue
            if now >= current["next_send"]:
                try:
                    self.udp_socket.sendto(current["frame"], self.destination)
                    if current["attempt"] == 0:
                        try:
                            self.request("POST", "/ack", {
                                "id": current["identity"][0],
                                "nonce": current["identity"][1],
                                "status": "delivering",
                            })
                        except (HTTPError, URLError, OSError, ValueError):
                            pass
                except OSError as exc:
                    print("[蓝牙控制 UDP 下发失败] {}".format(exc), flush=True)
                delay = self.RETRY_DELAYS[min(current["attempt"], len(self.RETRY_DELAYS) - 1)]
                current["attempt"] += 1
                current["next_send"] = now + delay
            self.stop_event.wait(self.poll_seconds)


def main():
    cloud_url = required_environment("CLOUD_HTTP_URL").rstrip("/")
    vehicle_id = required_environment("VEHICLE_ID")
    token = required_environment("VEHICLE_INGEST_TOKEN")
    udp_host = os.getenv("BLUETOOTH_UDP_HOST", "0.0.0.0").strip() or "0.0.0.0"
    udp_port = integer_environment("BLUETOOTH_UDP_PORT", 5000)
    queue_size = integer_environment("BLUETOOTH_QUEUE_SIZE", 1000, 1, 100000)
    timeout_seconds = float(os.getenv("BLUETOOTH_HTTP_TIMEOUT_SECONDS", "5"))
    source_prefix = os.getenv("BLUETOOTH_SOURCE_PREFIX", "S32K344-ETH").strip() or "S32K344-ETH"
    device_host = os.getenv("BLUETOOTH_DEVICE_HOST", "192.168.4.20").strip()
    network_interface = os.getenv("BLUETOOTH_NETWORK_INTERFACE", "eno1").strip() or "eno1"
    jetson_address = os.getenv("BLUETOOTH_JETSON_ADDRESS", "192.168.4.100").strip()
    diagnostic_interval = max(1.0, float(
        os.getenv("BLUETOOTH_DIAGNOSTIC_INTERVAL_SECONDS", "5")
    ))
    control_port = integer_environment("BLUETOOTH_CONTROL_PORT", 5001)
    poll_seconds = max(0.1, float(os.getenv("BLUETOOTH_CONTROL_POLL_SECONDS", "0.5")))
    control_timeout_seconds = max(1.0, float(
        os.getenv("BLUETOOTH_CONTROL_TIMEOUT_SECONDS", "10")
    ))
    sync_interval_seconds = max(1.0, float(os.getenv("BLUETOOTH_TIME_SYNC_INTERVAL_SECONDS", "5")))
    synchronizer = ClockSynchronizer(
        max_age_seconds=max(1.0, float(os.getenv("BLUETOOTH_TIME_SYNC_MAX_AGE_SECONDS", "30"))),
        max_rtt_ms=max(1.0, float(os.getenv("BLUETOOTH_TIME_SYNC_MAX_RTT_MS", "500"))),
        request_timeout_seconds=min(sync_interval_seconds, max(0.1, float(
            os.getenv("BLUETOOTH_TIME_SYNC_REQUEST_TIMEOUT_SECONDS", "2")))),
        max_offset_jump_ms=max(1.0, float(os.getenv("BLUETOOTH_TIME_SYNC_MAX_OFFSET_JUMP_MS", "100"))),
    )
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
    received_acks = queue.Queue(maxsize=32)
    control = ControlWorker(
        endpoint + "/control", token, udp_socket, device_host, control_port,
        received_acks, stop_event, poll_seconds, control_timeout_seconds,
        timeout_seconds,
    )
    control.start()
    sync_worker = SyncWorker(udp_socket, (device_host, control_port), synchronizer,
                             stop_event, sync_interval_seconds)
    sync_worker.start()
    cloud_clock = CloudClockMonitor(cloud_url, stop_event, timeout_seconds)
    cloud_clock.start()
    print(
        "蓝牙边缘代理已启动：UDP {}:{} -> {}，车辆 {}".format(
            udp_host, udp_port, cloud_url, vehicle_id
        ),
        flush=True,
    )
    print(
        "[BT LINK] S32K344 {} -> cable/UDP -> switch -> Jetson {} {}:{}".format(
            device_host, network_interface, jetson_address, udp_port
        ),
        flush=True,
    )
    print(
        "[BT CONTROL] Jetson {} {} -> UDP -> S32K344 {}:{}".format(
            network_interface, jetson_address, device_host, control_port
        ),
        flush=True,
    )
    started_at = time.monotonic()
    last_packet_at = None
    next_diagnostic_at = started_at + diagnostic_interval
    packet_count = 0
    try:
        while not stop_event.is_set():
            try:
                data, address = udp_socket.recvfrom(65535)
                received_epoch_ms = time.time_ns() // 1000000
                received_mono_ms = time.monotonic_ns() / 1000000
            except socket.timeout:
                current_time = time.monotonic()
                if current_time >= next_diagnostic_at:
                    if last_packet_at is None:
                        print(
                            "[BT RX WAIT] {:.0f}s内未收到UDP报文；检查 {} -> {} {}:{}".format(
                                current_time - started_at, device_host,
                                network_interface, jetson_address, udp_port,
                            ),
                            flush=True,
                        )
                    else:
                        print(
                            "[BT RX WAIT] {:.0f}s内无新报文；累计收到 {} 个UDP报文".format(
                                current_time - last_packet_at, packet_count
                            ),
                            flush=True,
                        )
                    next_diagnostic_at = current_time + diagnostic_interval
                continue
            except OSError:
                if stop_event.is_set():
                    break
                raise
            packet_count += 1
            last_packet_at = time.monotonic()
            next_diagnostic_at = last_packet_at + diagnostic_interval
            packet_kind = (
                "BTACK" if data.startswith(b"BTACK|") else
                "BTSYNC" if data.startswith(b"BTSYNC|") else
                "BTDATA" if data.startswith(b"S32K344_") else "UNKNOWN"
            )
            print(
                "[BT RX OK] packet={} type={} source={}:{} bytes={} received_at={}".format(
                    packet_count, packet_kind, address[0], address[1], len(data),
                    time.strftime("%H:%M:%S"),
                ),
                flush=True,
            )
            if address[0] != device_host:
                print(
                    "[BT RX WARN] 非目标来源 {}:{}；期望 S32K344 {}".format(
                        address[0], address[1], device_host
                    ),
                    flush=True,
                )
            if data.startswith(b"BTACK|"):
                try:
                    ack, frame = decode_ack_datagram(data, address, device_host)
                    received_acks.put_nowait((ack, frame, int(time.time() * 1000)))
                except (UnicodeError, ValueError, queue.Full):
                    print("[蓝牙控制 ACK 已拒绝] 格式或校验错误", flush=True)
                continue
            if data.startswith(b"BTSYNC|"):
                try:
                    response = decode_sync_datagram(data, address, device_host)
                    sample, reason = synchronizer.accept_detailed(
                        response, received_epoch_ms, received_mono_ms)
                    if sample is not None:
                        print("[蓝牙对时成功] sync_id={} RTT={:.2f} ms".format(
                            sample["sync_id"], sample["rtt_ms"]), flush=True)
                    else:
                        print("[蓝牙对时响应未采用] {}".format(reason), flush=True)
                except (UnicodeError, ValueError) as exc:
                    print("[蓝牙对时响应已拒绝] {}；{} 字节：{}".format(
                        exc, len(data), data[:160].decode("ascii", "backslashreplace")), flush=True)
                continue
            payloads = decode_datagram(data, address, source_prefix)
            if not payloads and address[0] == device_host and data.startswith(b"S32K344_"):
                print("[蓝牙报文已拒绝] 格式或 CRC 错误；{} 字节：{}".format(
                    len(data), data[:120].decode("ascii", "backslashreplace")), flush=True)
            for payload in payloads:
                if (address[0] == device_host and "source_tick_us" in payload and
                        "sync_id" in payload):
                    mapping = synchronizer.map_tick(
                        payload["source_tick_us"], payload["sync_id"],
                        received_epoch_ms, received_mono_ms)
                    if mapping is not None:
                        payload.update(mapping)
                        cloud_mapping = cloud_clock.latest(received_mono_ms)
                        if cloud_mapping is not None:
                            payload.update(cloud_mapping)
                        print("[蓝牙报文已对时] SEQ={} 报文ID={} 映射ID={} 至Jetson={:.1f} ms".format(
                            payload.get("sender_sequence"), payload["sync_id"],
                            mapping["mapping_sync_id"],
                            received_epoch_ms - mapping["source_time_ms"]), flush=True)
                    else:
                        print("[蓝牙报文未对时] SEQ={} 报文ID={} TICK_US={}".format(
                            payload.get("sender_sequence"), payload["sync_id"],
                            payload["source_tick_us"]), flush=True)
                if is_recent_duplicate(
                    recent_frames, payload["source"], payload["text"], time.monotonic(),
                    payload.get("sender_sequence"), payload.get("source_tick_us")
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
        control.join(timeout=timeout_seconds + 2)
        sync_worker.join(timeout=2)
        cloud_clock.join(timeout=2)
        uploader.join(timeout=timeout_seconds + 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
