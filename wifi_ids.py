"""Rule-based IDS for simulated WiFi charging telemetry."""

import re
import struct
from collections import deque


CHARGE_PACKET_PATTERN = re.compile(
    r"^CHG,(START|DATA),SEQ=(\d+),SOC=(\d+(?:\.\d+)?),STATE=([A-Z_]+)"
    r"(?:,TS=(\d{10,16}))?$",
    re.IGNORECASE,
)
GBT74_PACKET_PATTERN = re.compile(
    r"^GBT74,(START|DATA),SEQ=(\d+),DATA=([0-9A-F]{10}),STATE=(CHARGING|FULL)"
    r"(?:,TS=(\d{10,16}))?$", re.IGNORECASE,
)


def parse_charging_packet(text):
    packet = str(text).strip()
    gbt = GBT74_PACKET_PATTERN.fullmatch(packet)
    if gbt:
        pgi, soc_raw, remaining = struct.unpack("<BHH", bytes.fromhex(gbt.group(3)))
        if pgi != 0x74 or soc_raw > 1000 or remaining > 65000:
            return None
        return {
            "kind": gbt.group(1).upper(), "sequence": int(gbt.group(2)),
            "soc": soc_raw / 10.0, "state": gbt.group(4).upper(),
            "sender_time_ms": int(gbt.group(5)) if gbt.group(5) else None,
            "protocol": "GBT74-demo", "pgi": "0x74",
            "payload_hex": gbt.group(3).upper(), "remaining_minutes": remaining,
        }
    match = CHARGE_PACKET_PATTERN.fullmatch(packet)
    if not match:
        return None
    return {
        "kind": match.group(1).upper(),
        "sequence": int(match.group(2)),
        "soc": float(match.group(3)),
        "state": match.group(4).upper(),
        "sender_time_ms": int(match.group(5)) if match.group(5) else None,
    }


def detect_wifi_intrusion(state, charge, detected_at_ms):
    result = {"verdict": "unclassified", "rule": None, "alert": None}
    if charge is None:
        return result

    sequence = charge["sequence"]
    soc = charge["soc"]
    result["verdict"] = "normal"

    if charge["kind"] == "START" and sequence == 0:
        state.wifi_rate_times = deque()
        state.wifi_rate_active = False
        state.wifi_receive_rate = 0
        state.wifi_ids_last_good_soc = soc
        state.wifi_ids_last_good_sequence = sequence
        state.wifi_ids_alert_count = 0
        state.wifi_ids_latest_alert = None
        return result

    if state.wifi_ids_last_good_soc is None or state.wifi_ids_last_good_sequence is None:
        state.wifi_ids_last_good_soc = soc
        state.wifi_ids_last_good_sequence = sequence
        return result

    sequence_gap = sequence - state.wifi_ids_last_good_sequence
    if sequence_gap <= 0:
        return result

    baseline_soc = state.wifi_ids_last_good_soc
    delta_soc = soc - baseline_soc
    allowed_increase = max(3.0, sequence_gap * 2.0)
    if not (delta_soc < -1.0 or delta_soc > allowed_increase):
        state.wifi_ids_last_good_soc = soc
        state.wifi_ids_last_good_sequence = sequence
        return result

    state.wifi_ids_alert_count += 1
    alert = {
        "rule": "SOC_JUMP",
        "severity": "high",
        "title": "SOC 数据突变攻击",
        "sequence": sequence,
        "detected_at_ms": detected_at_ms,
        "baseline_soc": round(baseline_soc, 1),
        "observed_soc": round(soc, 1),
        "delta_soc": round(delta_soc, 1),
        "allowed_increase": round(allowed_increase, 1),
    }
    state.wifi_ids_latest_alert = alert
    result.update({"verdict": "anomaly", "rule": "SOC_JUMP", "alert": alert})
    return result


def detect_wifi_rate(state, charge, edge_at_ms, detected_at_ms):
    """Only parsed charging DATA counts. Use edge reception time, not HTTP bursts."""
    if not charge or charge["kind"] != "DATA":
        return None
    times = getattr(state, "wifi_rate_times", None)
    if times is None:
        times = state.wifi_rate_times = deque()
    # Late/out-of-order uploads cannot manufacture a new sliding window.
    if times and edge_at_ms < times[-1]:
        return None
    times.append(edge_at_ms)
    while times and times[0] < edge_at_ms - 2000:
        times.popleft()
    rate = len(times) / 2.0
    state.wifi_receive_rate = rate
    if rate <= 3:
        state.wifi_rate_active = False
    if rate < 8:
        return None
    alert = {"rule": "WIFI_RATE", "severity": "high", "title": "DDoS 流量模拟告警",
             "sequence": charge["sequence"], "detected_at_ms": detected_at_ms,
             "rate_pps": rate, "threshold_pps": 8, "window_seconds": 2}
    if not getattr(state, "wifi_rate_active", False):
        state.wifi_ids_alert_count += 1
        state.wifi_ids_latest_alert = alert
        state.wifi_rate_active = True
    return {"verdict": "anomaly", "rule": "WIFI_RATE", "alert": alert}
