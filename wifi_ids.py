"""Rule-based IDS for simulated WiFi charging telemetry."""

import re


CHARGE_PACKET_PATTERN = re.compile(
    r"^CHG,(START|DATA),SEQ=(\d+),SOC=(\d+(?:\.\d+)?),STATE=([A-Z_]+)"
    r"(?:,TS=(\d{10,16}))?$",
    re.IGNORECASE,
)


def parse_charging_packet(text):
    match = CHARGE_PACKET_PATTERN.fullmatch(str(text).strip())
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
