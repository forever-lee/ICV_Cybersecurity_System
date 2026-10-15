"""CRC-protected BLE clock exchange and short-lived monotonic clock mapping."""

import base64
import binascii
import re
import secrets
import struct
import threading
from collections import deque


MAX_FRAME_BYTES = 256
MAX_COUNTER = (1 << 32) - 1
MAX_TICK_US = (1 << 64) - 1
V2_REQUEST_PREFIX = b"BTSYNC|2|Q|"
V2_RESPONSE_PREFIX = b"BTSYNC|2|R|"
TELEMETRY_PATTERN = re.compile(
    r"^(S32K344_HELLO_\d{2,4}|"
    r"S32K344_ABNORMAL_\d{4};TYPE=BLE_ATTACK;LEVEL=HIGH)"
    r";SEQ=(\d{1,10});TICK_US=(\d{1,20});SYNC_ID=(\d{1,10})"
    r"(?:;CRC=([0-9A-F]{4}))?$"
)


def _integer(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("invalid Bluetooth timing integer")
    return value


def _pack(fields):
    body = "|".join(str(field) for field in fields)
    encoded = body.encode("ascii")
    if len(encoded) + 6 > MAX_FRAME_BYTES:
        raise ValueError("Bluetooth timing frame too large")
    return "{}|{:04X}#".format(body, binascii.crc_hqx(encoded, 0xFFFF))


def _unpack(frame, kind, operation, count):
    if isinstance(frame, bytes):
        frame = frame.decode("ascii")
    if not isinstance(frame, str) or not frame.endswith("#"):
        raise ValueError("incomplete Bluetooth timing frame")
    if len(frame.encode("ascii")) > MAX_FRAME_BYTES:
        raise ValueError("Bluetooth timing frame too large")
    fields = frame[:-1].split("|")
    if (len(fields) != count or fields[:2] != [kind, "1"] or
            fields[3] != operation or not re.fullmatch(r"[0-9A-F]{4}", fields[-1])):
        raise ValueError("invalid Bluetooth timing frame")
    body = "|".join(fields[:-1]).encode("ascii")
    if binascii.crc_hqx(body, 0xFFFF) != int(fields[-1], 16):
        raise ValueError("Bluetooth timing CRC mismatch")
    return fields


def _pack_v2(prefix, payload):
    crc = binascii.crc_hqx(prefix + payload, 0xFFFF)
    return (prefix + base64.urlsafe_b64encode(payload + struct.pack("!H", crc)) + b"#").decode("ascii")


def _unpack_v2(frame, prefix, payload_bytes):
    if isinstance(frame, str):
        frame = frame.encode("ascii")
    if (not isinstance(frame, bytes) or not frame.startswith(prefix) or
            not frame.endswith(b"#") or len(frame) > MAX_FRAME_BYTES):
        raise ValueError("invalid Bluetooth timing frame")
    try:
        packed = base64.b64decode(frame[len(prefix):-1], altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid Bluetooth timing encoding") from exc
    if len(packed) != payload_bytes + 2:
        raise ValueError("invalid Bluetooth timing length")
    payload, received_crc = packed[:-2], struct.unpack("!H", packed[-2:])[0]
    if binascii.crc_hqx(prefix + payload, 0xFFFF) != received_crc:
        raise ValueError("Bluetooth timing CRC mismatch")
    return payload


def make_request(sync_id, t1_ms, version=1):
    if version == 2:
        return _pack_v2(V2_REQUEST_PREFIX, struct.pack(
            "!IQ", _integer(sync_id, 1, MAX_COUNTER),
            _integer(t1_ms, 1, 9999999999999)))
    if version != 1:
        raise ValueError("unsupported Bluetooth timing version")
    return _pack(("BTSYNC", 1, _integer(sync_id, 1, MAX_COUNTER), "REQ",
                  _integer(t1_ms, 1, 9999999999999)))


def parse_request(frame):
    if frame.startswith(V2_REQUEST_PREFIX if isinstance(frame, bytes) else V2_REQUEST_PREFIX.decode("ascii")):
        sync_id, t1_ms = struct.unpack("!IQ", _unpack_v2(frame, V2_REQUEST_PREFIX, 12))
        return {"sync_id": _integer(sync_id, 1, MAX_COUNTER),
                "t1_ms": _integer(t1_ms, 1, 9999999999999)}
    fields = _unpack(frame, "BTSYNC", "REQ", 6)
    if not fields[2].isdigit() or not fields[4].isdigit():
        raise ValueError("invalid Bluetooth sync request")
    return {"sync_id": _integer(int(fields[2]), 1, MAX_COUNTER),
            "t1_ms": _integer(int(fields[4]), 1, 9999999999999)}


def make_response(sync_id, t1_ms, u2_us, u3_us, version=1):
    if u3_us < u2_us:
        raise ValueError("nonmonotonic Bluetooth sync response")
    if version == 2:
        duration_us = u3_us - u2_us
        return _pack_v2(V2_RESPONSE_PREFIX, struct.pack(
            "!IQI", _integer(sync_id, 1, MAX_COUNTER),
            _integer(u2_us, 1, MAX_TICK_US),
            _integer(duration_us, 0, MAX_COUNTER)))
    if version != 1:
        raise ValueError("unsupported Bluetooth timing version")
    return _pack(("BTSYNC", 1, _integer(sync_id, 1, MAX_COUNTER), "RESP",
                  _integer(t1_ms, 1, 9999999999999),
                  _integer(u2_us, 1, MAX_TICK_US),
                  _integer(u3_us, 1, MAX_TICK_US)))


def parse_response(frame):
    if frame.startswith(V2_RESPONSE_PREFIX if isinstance(frame, bytes) else V2_RESPONSE_PREFIX.decode("ascii")):
        sync_id, u2_us, duration_us = struct.unpack(
            "!IQI", _unpack_v2(frame, V2_RESPONSE_PREFIX, 16))
        if not 0 < sync_id <= MAX_COUNTER or not 0 < u2_us <= MAX_TICK_US - duration_us:
            raise ValueError("invalid Bluetooth sync response")
        return {"sync_id": sync_id, "t1_ms": None,
                "u2_us": u2_us, "u3_us": u2_us + duration_us}
    fields = _unpack(frame, "BTSYNC", "RESP", 8)
    if not all(field.isdigit() for field in (fields[2], fields[4], fields[5], fields[6])):
        raise ValueError("invalid Bluetooth sync response")
    result = {"sync_id": _integer(int(fields[2]), 1, MAX_COUNTER),
              "t1_ms": _integer(int(fields[4]), 1, 9999999999999),
              "u2_us": _integer(int(fields[5]), 1, MAX_TICK_US),
              "u3_us": _integer(int(fields[6]), 1, MAX_TICK_US)}
    if result["u3_us"] < result["u2_us"]:
        raise ValueError("nonmonotonic Bluetooth sync response")
    return result


def parse_timed_telemetry(text):
    match = TELEMETRY_PATTERN.fullmatch(text)
    if match is None:
        return None
    sequence, tick_us, sync_id = map(int, (match.group(2), match.group(3), match.group(4)))
    if match.group(5) is not None:
        body = text.rsplit(";CRC=", 1)[0].encode("ascii")
        if binascii.crc_hqx(body, 0xFFFF) != int(match.group(5), 16):
            return None
    if sequence > MAX_COUNTER or not 0 < tick_us <= MAX_TICK_US or sync_id > MAX_COUNTER:
        return None
    return {"text": match.group(1), "sender_sequence": sequence,
            "source_tick_us": tick_us, "sync_id": sync_id}


class ClockSynchronizer:
    """Associate only CRC-valid, recent, low-RTT exchanges with telemetry."""

    def __init__(self, max_age_seconds=30.0, max_rtt_ms=500.0,
                 request_timeout_seconds=2.0, max_offset_jump_ms=100.0):
        self.max_age_ms = max_age_seconds * 1000
        self.max_rtt_ms = max_rtt_ms
        self.request_timeout_ms = request_timeout_seconds * 1000
        self.max_offset_jump_ms = max_offset_jump_ms
        self.lock = threading.Lock()
        self.next_id = secrets.randbelow(MAX_COUNTER - 1) + 1
        self.pending = None
        self.samples = deque(maxlen=8)

    def begin(self, epoch_ms, monotonic_ms):
        with self.lock:
            sync_id = self.next_id
            self.next_id = 1 if sync_id == MAX_COUNTER else sync_id + 1
            self.pending = {"sync_id": sync_id, "t1_ms": epoch_ms,
                            "sent_mono_ms": monotonic_ms}
        return make_request(sync_id, epoch_ms, version=2)

    def cancel(self):
        with self.lock:
            self.pending = None

    def accept(self, response, t4_ms, t4_mono_ms):
        sample, _reason = self.accept_detailed(response, t4_ms, t4_mono_ms)
        return sample

    def accept_detailed(self, response, t4_ms, t4_mono_ms):
        """Return the accepted sample or a precise, non-secret rejection reason."""
        with self.lock:
            pending = self.pending
            if pending is None:
                return None, "没有待确认请求（响应可能超过下一次对时周期）"
            if response["sync_id"] != pending["sync_id"]:
                return None, "ID 不匹配：响应={} 当前={}".format(
                    response["sync_id"], pending["sync_id"])
            if response["t1_ms"] is not None and response["t1_ms"] != pending["t1_ms"]:
                return None, "T1 不匹配：响应={} 当前={}".format(
                    response["t1_ms"], pending["t1_ms"])
            self.pending = None
            wall_elapsed = t4_ms - pending["t1_ms"]
            mono_elapsed = t4_mono_ms - pending["sent_mono_ms"]
            if wall_elapsed < 0 or mono_elapsed < 0:
                return None, "时钟倒退：墙钟={:.1f} ms 单调钟={:.1f} ms".format(
                    wall_elapsed, mono_elapsed)
            if mono_elapsed > self.request_timeout_ms:
                return None, "响应超时：{:.1f} ms（限值 {:.1f} ms）".format(
                    mono_elapsed, self.request_timeout_ms)
            if abs(wall_elapsed - mono_elapsed) > self.max_offset_jump_ms:
                return None, "Jetson 时钟跳变：墙钟={:.1f} ms 单调钟={:.1f} ms".format(
                    wall_elapsed, mono_elapsed)
            rtt_ms = wall_elapsed - (response["u3_us"] - response["u2_us"]) / 1000
            if not 0 <= rtt_ms <= self.max_rtt_ms:
                return None, "RTT={:.1f} ms（限值 0–{:.1f} ms；总往返 {:.1f} ms）".format(
                    rtt_ms, self.max_rtt_ms, wall_elapsed)
            offset_ms = ((pending["t1_ms"] + t4_ms) / 2 -
                         (response["u2_us"] + response["u3_us"]) / 2000)
            recent = [sample for sample in self.samples
                      if t4_mono_ms - sample["accepted_mono_ms"] <= self.max_age_ms]
            if recent:
                offset_delta = offset_ms - min(recent, key=lambda s: s["rtt_ms"])["offset_ms"]
                if abs(offset_delta) > self.max_offset_jump_ms:
                    return None, "映射偏移跳变：{:.1f} ms（限值 {:.1f} ms）".format(
                        offset_delta, self.max_offset_jump_ms)
            sample = {"sync_id": response["sync_id"], "offset_ms": offset_ms,
                      "rtt_ms": rtt_ms, "accepted_mono_ms": t4_mono_ms,
                      "u3_us": response["u3_us"]}
            self.samples.append(sample)
            return dict(sample), None

    def map_tick(self, tick_us, sync_id, received_epoch_ms, received_mono_ms):
        with self.lock:
            if not 0 < sync_id <= MAX_COUNTER or not 0 < tick_us <= MAX_TICK_US:
                return None
            # A sender can stamp telemetry with a sync ID whose response is later
            # lost on the K344 uplink. All accepted samples describe the same
            # sender monotonic clock, so a nearby, still-fresh sample can map it.
            candidates = sorted(self.samples, key=lambda item: (
                item["sync_id"] != sync_id, -item["accepted_mono_ms"]))
            for sample in candidates:
                sample_age_ms = received_mono_ms - sample["accepted_mono_ms"]
                tick_delta_us = tick_us - sample["u3_us"]
                if (not 0 <= sample_age_ms <= self.max_age_ms or
                        abs(tick_delta_us) > self.max_age_ms * 1000):
                    continue
                source_ms = tick_us / 1000 + sample["offset_ms"]
                transit_ms = received_epoch_ms - source_ms
                if not 0 <= transit_ms <= 60000:
                    continue
                return {"source_time_ms": round(source_ms, 2),
                        "udp_received_at_ms": received_epoch_ms,
                        "sync_id": sync_id,
                        "mapping_sync_id": sample["sync_id"],
                        "sync_rtt_ms": round(sample["rtt_ms"], 2),
                        "clock_offset_ms": round(sample["offset_ms"], 2),
                        "latency_quality": "synced"}
            return None
