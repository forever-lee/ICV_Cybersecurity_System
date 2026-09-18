"""Cloud relay and V-SHIELD connected-vehicle cybersecurity console."""

import argparse
import asyncio
import json
import logging
import math
import os
import random
import secrets
import sqlite3
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from stream_protocol import (
    FMP4_HEADER_SIZE,
    FMP4_KIND_INIT,
    FMP4_KIND_MEDIA,
    HEADER_SIZE,
    now_ms,
    unpack_fmp4,
    unpack_frame,
)


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INGEST_TOKEN = os.getenv("VEHICLE_INGEST_TOKEN", "change-me-in-production")
DASHBOARD_ACCESS_TOKEN = os.getenv("DASHBOARD_ACCESS_TOKEN", "")
OFFLINE_AFTER_SECONDS = float(os.getenv("OFFLINE_AFTER_SECONDS", "4"))
MAX_FRAME_BYTES = int(os.getenv("MAX_FRAME_BYTES", str(4 * 1024 * 1024)))
BLUETOOTH_OFFLINE_AFTER_SECONDS = float(os.getenv("BLUETOOTH_OFFLINE_AFTER_SECONDS", "10"))
MAX_BLUETOOTH_TEXT_BYTES = int(os.getenv("MAX_BLUETOOTH_TEXT_BYTES", "2048"))
WIFI_OFFLINE_AFTER_SECONDS = float(os.getenv("WIFI_OFFLINE_AFTER_SECONDS", "10"))
MAX_WIFI_TEXT_BYTES = int(os.getenv("MAX_WIFI_TEXT_BYTES", "4096"))
NAVIGATION_OFFLINE_AFTER_SECONDS = float(os.getenv("NAVIGATION_OFFLINE_AFTER_SECONDS", "10"))
AMAP_JS_KEY = os.getenv("AMAP_JS_KEY", "").strip()
AMAP_SECURITY_JS_CODE = os.getenv("AMAP_SECURITY_JS_CODE", "").strip()
AMAP_SERVICE_HOST = os.getenv("AMAP_SERVICE_HOST", "").strip()
CLOUD_FRAME_BUFFER_FRAMES = max(30, int(os.getenv("CLOUD_FRAME_BUFFER_FRAMES", "300")))
FMP4_BUFFER_SEGMENTS = max(30, int(os.getenv("FMP4_BUFFER_SEGMENTS", "300")))
BODY_DATA_TIMEOUT_SECONDS = float(os.getenv("BODY_DATA_TIMEOUT_SECONDS", "10"))
BODY_DATABASE_PATH = Path(os.getenv("BODY_DATABASE_PATH", str(BASE_DIR / "data" / "body_telemetry.db")))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("vehicle-cloud")


def init_body_database():
    BODY_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(BODY_DATABASE_PATH)) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS body_frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                can_id TEXT NOT NULL,
                dlc INTEGER NOT NULL,
                data_hex TEXT NOT NULL,
                signals_json TEXT NOT NULL,
                captured_at_ms INTEGER NOT NULL,
                received_at_ms INTEGER NOT NULL,
                valid INTEGER NOT NULL,
                invalid_reason TEXT NOT NULL,
                device_id TEXT NOT NULL,
                interface_path TEXT NOT NULL,
                source TEXT NOT NULL,
                is_test INTEGER NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS body_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_id TEXT NOT NULL,
                event_key TEXT NOT NULL,
                level TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_body_frames_vehicle_time ON body_frames(vehicle_id, received_at_ms DESC)"
        )


def persist_body_frame(vehicle_id, frame):
    with sqlite3.connect(str(BODY_DATABASE_PATH)) as connection:
        connection.execute(
            """INSERT INTO body_frames (
                vehicle_id, sequence, can_id, dlc, data_hex, signals_json,
                captured_at_ms, received_at_ms, valid, invalid_reason,
                device_id, interface_path, source, is_test
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vehicle_id, frame["sequence"], frame["can_id"], frame["dlc"], frame["data_hex"],
                json.dumps(frame.get("signal_changes", {}), ensure_ascii=False, separators=(",", ":")),
                frame["vehicle_time_ms"], frame["cloud_receive_time_ms"], int(frame.get("valid", True)),
                frame.get("invalid_reason", ""), frame.get("device_id", ""),
                frame.get("interface_path", ""), frame.get("source", ""), int(frame.get("test", False)),
            ),
        )


init_body_database()


class VehicleState:
    def __init__(self, vehicle_id):
        self.vehicle_id = vehicle_id
        self.frame = None
        self.frame_buffer = deque(maxlen=CLOUD_FRAME_BUFFER_FRAMES)
        self.sequence = -1
        self.last_seen_monotonic = 0.0
        self.last_seen_ms = 0
        self.connected = False
        self.source_address = None
        self.metrics = {
            "vehicle_id": vehicle_id,
            "status": "offline",
            "transport": "WebSocket / JPEG",
            "schema_version": 1,
        }
        self.domains = {
            "video": {"status": "active"},
            "chassis": {"status": "reserved"},
            "powertrain": {"status": "reserved"},
            "body": {"status": "reserved"},
            "cockpit": {"status": "reserved"},
            "adas": {"status": "reserved"},
        }
        self.history = deque(maxlen=180)
        self.frame_arrivals = deque(maxlen=300)
        self.condition = asyncio.Condition()
        self.ingest_lock = asyncio.Lock()
        self.ingest_websocket = None
        self.fmp4_init = None
        self.fmp4_fragments = deque(maxlen=FMP4_BUFFER_SEGMENTS)
        self.fmp4_condition = asyncio.Condition()
        self.fmp4_ingest_lock = asyncio.Lock()
        self.fmp4_ingest_websocket = None
        self.fmp4_connected = False
        self.viewer_count = 0
        self.received_frames = 0
        self.invalid_frames = 0
        self.last_sequence = None
        self.cloud_dropped_frames = 0
        self.bluetooth_sequence = 0
        self.bluetooth_last_seen_ms = 0
        self.bluetooth_latest = None
        self.bluetooth_history = deque(maxlen=60)
        self.wifi_sequence = 0
        self.wifi_last_seen_ms = 0
        self.wifi_latest = None
        self.wifi_history = deque(maxlen=60)
        self.body_sequence = 0
        self.body_frame_history = deque(maxlen=80)
        self.body_alerts = deque(maxlen=40)
        self.body_last_seen_ms = 0
        self.navigation = None

    def online(self):
        return (self.connected or self.fmp4_connected) and (time.monotonic() - self.last_seen_monotonic) < OFFLINE_AFTER_SECONDS

    def snapshot(self):
        data = dict(self.metrics)
        domains = dict(self.domains)
        domains["body"] = self.body_snapshot()
        data.update(
            {
                "vehicle_id": self.vehicle_id,
                "status": "online" if self.online() else "offline",
                "last_seen_ms": self.last_seen_ms,
                "viewer_count": self.viewer_count,
                "received_frames": self.received_frames,
                "cloud_dropped_frames": self.cloud_dropped_frames,
                "invalid_frames": self.invalid_frames,
                "domains": domains,
                "bluetooth": self.bluetooth_snapshot(),
                "wifi": self.wifi_snapshot(),
                "navigation": self.navigation_snapshot(),
            }
        )
        return data

    def navigation_snapshot(self):
        if not self.navigation:
            return None
        data = dict(self.navigation)
        captured_at_ms = data.get("captured_at_ms", 0)
        data["status"] = (
            "online"
            if captured_at_ms and now_ms() - captured_at_ms < NAVIGATION_OFFLINE_AFTER_SECONDS * 1000
            else "stale"
        )
        return data

    def bluetooth_snapshot(self):
        online = bool(
            self.bluetooth_last_seen_ms
            and now_ms() - self.bluetooth_last_seen_ms < BLUETOOTH_OFFLINE_AFTER_SECONDS * 1000
        )
        return {
            "status": "online" if online else "offline",
            "last_seen_ms": self.bluetooth_last_seen_ms,
            "packet_count": self.bluetooth_sequence,
            "latest": self.bluetooth_latest,
            "history": list(self.bluetooth_history)[:12],
        }

    def wifi_snapshot(self):
        online = bool(
            self.wifi_last_seen_ms
            and now_ms() - self.wifi_last_seen_ms < WIFI_OFFLINE_AFTER_SECONDS * 1000
        )
        return {
            "status": "online" if online else "offline",
            "last_seen_ms": self.wifi_last_seen_ms,
            "packet_count": self.wifi_sequence,
            "latest": self.wifi_latest,
            "history": list(self.wifi_history)[:12],
        }

    def body_snapshot(self):
        body = dict(self.domains.get("body") or {})
        age_ms = now_ms() - self.body_last_seen_ms if self.body_last_seen_ms else None
        online = age_ms is not None and age_ms <= BODY_DATA_TIMEOUT_SECONDS * 1000
        link = dict(body.get("link") or {})
        if not online:
            link.update({"mcu_status": "timeout", "edge_status": "timeout", "upload_status": "timeout"})
        link.update({
            "cloud_status": "online" if online else "timeout",
            "last_upload_at_ms": self.body_last_seen_ms or None,
            "age_ms": age_ms,
        })
        body["status"] = "online" if online else "offline"
        body["connected"] = online
        body["link"] = link
        alerts = list(self.body_alerts)
        if self.body_last_seen_ms and not online:
            alerts.insert(0, {
                "key": "link:timeout", "level": "critical", "title": "车身数据超时",
                "detail": "S32K344或Jetson上传链路已超过{}秒无数据".format(BODY_DATA_TIMEOUT_SECONDS),
                "created_at_ms": self.body_last_seen_ms + int(BODY_DATA_TIMEOUT_SECONDS * 1000),
            })
        body["alerts"] = alerts[:12]
        body["frame_count"] = self.body_sequence
        body["latest_frame"] = self.body_frame_history[0] if self.body_frame_history else None
        body["frame_history"] = list(self.body_frame_history)[:8]
        return body


class Registry:
    def __init__(self):
        self.vehicles = {}

    def get(self, vehicle_id):
        vehicle_id = normalize_vehicle_id(vehicle_id)
        if vehicle_id not in self.vehicles:
            self.vehicles[vehicle_id] = VehicleState(vehicle_id)
        return self.vehicles[vehicle_id]


def normalize_vehicle_id(value):
    cleaned = "".join(ch for ch in value.upper() if ch.isalnum() or ch in "-_")[:40]
    if not cleaned:
        raise HTTPException(status_code=400, detail="invalid vehicle id")
    return cleaned


def bearer_token(headers):
    value = headers.get("authorization", "")
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def token_matches(actual, expected):
    return bool(actual and expected and secrets.compare_digest(str(actual), str(expected)))


def finite_number(payload, keys, minimum, maximum, required=False):
    for key in keys:
        if key not in payload or payload[key] is None:
            continue
        try:
            value = float(payload[key])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="{} must be numeric".format(keys[0]))
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise HTTPException(
                status_code=400,
                detail="{} must be between {} and {}".format(keys[0], minimum, maximum),
            )
        return value
    if required:
        raise HTTPException(status_code=400, detail="{} is required".format(keys[0]))
    return None


def normalize_navigation(payload):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="navigation payload must be an object")
    latitude = finite_number(payload, ("latitude", "lat"), -90, 90, required=True)
    longitude = finite_number(payload, ("longitude", "lng", "lon"), -180, 180, required=True)
    speed_kph = finite_number(payload, ("speed_kph", "speed"), 0, 500)
    heading_deg = finite_number(payload, ("heading_deg", "heading"), 0, 360)
    accuracy_m = finite_number(payload, ("accuracy_m", "accuracy"), 0, 100000)
    try:
        captured_at_ms = int(payload.get("captured_at_ms") or now_ms())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="captured_at_ms must be an integer")
    if captured_at_ms <= 0 or captured_at_ms > now_ms() + 60000:
        raise HTTPException(status_code=400, detail="captured_at_ms is outside the accepted range")
    coordinate_system = str(payload.get("coordinate_system", "WGS84")).upper().replace("-", "")
    if coordinate_system not in {"WGS84", "GCJ02"}:
        raise HTTPException(status_code=400, detail="coordinate_system must be WGS84 or GCJ02")
    return {
        "latitude": round(latitude, 7),
        "longitude": round(longitude, 7),
        "speed_kph": round(speed_kph, 1) if speed_kph is not None else None,
        "heading_deg": round(heading_deg % 360, 1) if heading_deg is not None else None,
        "accuracy_m": round(accuracy_m, 1) if accuracy_m is not None else None,
        "coordinate_system": coordinate_system,
        "source": str(payload.get("source", "GNSS"))[:32],
        "captured_at_ms": captured_at_ms,
        "received_at_ms": now_ms(),
    }


BODY_SIGNAL_GROUPS = {
    "door": {
        "fields": ("Door_FL", "Door_FR", "Door_RL", "Door_RR"),
        "can_id": "0x351", "frame_name": "BodyDoorStatus",
        "pdu_name": "BodyDoorStatusPdu", "cycle_ms": 100,
    },
    "lock": {
        "fields": ("CentralLockState",),
        "can_id": "0x352", "frame_name": "CentralLockStatus",
        "pdu_name": "CentralLockStatusPdu", "cycle_ms": 500,
    },
    "light": {
        "fields": ("LowBeam", "HighBeam", "TurnLeft", "TurnRight", "Hazard"),
        "can_id": "0x353", "frame_name": "BodyLightStatus",
        "pdu_name": "BodyLightStatusPdu", "cycle_ms": 50,
    },
}

BODY_TEST_ACTIONS = {
    "door_fl": ("Door_FL", "左前门"),
    "door_fr": ("Door_FR", "右前门"),
    "door_rl": ("Door_RL", "左后门"),
    "door_rr": ("Door_RR", "右后门"),
    "central_lock": ("CentralLockState", "中央门锁"),
    "low_beam": ("LowBeam", "近光灯"),
    "high_beam": ("HighBeam", "远光灯"),
    "turn_left": ("TurnLeft", "左转向灯"),
    "turn_right": ("TurnRight", "右转向灯"),
    "hazard": ("Hazard", "双闪"),
}


def body_switch(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value).strip().lower() in {"1", "true", "on", "open", "opened", "active"}


def body_payload_bytes(body, sequence, group_name):
    doors = sum(
        (1 << index) if body_switch(body.get(field, False)) else 0
        for index, field in enumerate(BODY_SIGNAL_GROUPS["door"]["fields"])
    ) if group_name == "door" else 0
    lock = 1 if str(body.get("CentralLockState", "unlocked")).lower() == "locked" else 0
    if group_name != "lock":
        lock = 0
    lights = sum(
        (1 << index) if body_switch(body.get(field, False)) else 0
        for index, field in enumerate(BODY_SIGNAL_GROUPS["light"]["fields"])
    ) if group_name == "light" else 0
    values = [doors, lock, lights, 0, 0, 0, sequence & 0x0F]
    values.append(sum(values) & 0xFF)
    return " ".join("{:02X}".format(value) for value in values)


def extract_body_signals(payload):
    signal_payload = payload.get("signals") if isinstance(payload.get("signals"), dict) else {}
    values, validity, reasons = {}, {}, {}
    for group in BODY_SIGNAL_GROUPS.values():
        for field in group["fields"]:
            if field in signal_payload:
                raw = signal_payload[field]
            elif field in payload:
                raw = payload[field]
            else:
                continue
            if isinstance(raw, dict):
                values[field] = raw.get("value")
                validity[field] = bool(raw.get("valid", True))
                reasons[field] = str(raw.get("reason", ""))[:120]
            else:
                values[field] = raw
                validity[field] = bool(payload.get("valid", True))
                reasons[field] = str(payload.get("invalid_reason", ""))[:120]
    return values, validity, reasons


def normalize_raw_can_frame(payload, fallback_can_id, fallback_data_hex):
    raw = payload.get("raw_frame") if isinstance(payload.get("raw_frame"), dict) else payload
    value = raw.get("can_id", fallback_can_id)
    try:
        can_id_number = int(str(value), 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="can_id must be a decimal or hexadecimal integer")
    if not 0 <= can_id_number <= 0x1FFFFFFF:
        raise HTTPException(status_code=400, detail="can_id is outside the CAN/CAN-FD range")
    data_hex = str(raw.get("data_hex", fallback_data_hex)).replace("0x", "").replace(",", " ").strip()
    try:
        data = bytes.fromhex(data_hex)
    except ValueError:
        raise HTTPException(status_code=400, detail="data_hex must contain hexadecimal bytes")
    try:
        dlc = int(raw.get("dlc", len(data)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="dlc must be an integer")
    if not 0 <= dlc <= 64 or len(data) != dlc:
        raise HTTPException(status_code=400, detail="dlc must match the number of data_hex bytes")
    return "0x{:X}".format(can_id_number), dlc, " ".join("{:02X}".format(item) for item in data)


def body_alerts(body, validity, reasons, timestamp):
    alerts = []
    for field, valid in validity.items():
        if not valid:
            alerts.append({
                "key": "invalid:{}".format(field), "level": "warning", "title": "信号无效",
                "detail": "{} {}".format(field, reasons.get(field) or "未通过有效性校验"),
                "created_at_ms": timestamp,
            })
    if str(body.get("can_status", "normal")).lower() not in {"normal", "ok", "online", "active"}:
        alerts.append({
            "key": "can:abnormal", "level": "critical", "title": "CAN通信异常",
            "detail": "当前CAN状态：{}".format(body.get("can_status")), "created_at_ms": timestamp,
        })
    door_open = any(body_switch(body.get(field, False)) for field in BODY_SIGNAL_GROUPS["door"]["fields"])
    if str(body.get("CentralLockState", "")).lower() == "locked" and door_open:
        alerts.append({
            "key": "conflict:locked-door-open", "level": "warning", "title": "车门状态冲突",
            "detail": "中央门锁已锁止，但仍检测到开启车门", "created_at_ms": timestamp,
        })
    if body_switch(body.get("Hazard", False)) and not (
        body_switch(body.get("TurnLeft", False)) and body_switch(body.get("TurnRight", False))
    ):
        alerts.append({
            "key": "conflict:hazard", "level": "warning", "title": "灯光状态冲突",
            "detail": "双闪开启时左右转向灯状态不一致", "created_at_ms": timestamp,
        })
    return alerts


def record_body_update(state, payload, is_test=False):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body payload must be an object")
    previous = dict(state.domains.get("body") or {})
    received_at_ms = now_ms()
    try:
        captured_at_ms = int(payload.get("captured_at_ms") or received_at_ms)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="captured_at_ms must be an integer")
    if captured_at_ms <= 0 or captured_at_ms > received_at_ms + 60000:
        raise HTTPException(status_code=400, detail="captured_at_ms is outside the accepted range")

    reported_fields, validity, invalid_reasons = extract_body_signals(payload)
    body = dict(previous)
    changed_at = dict(previous.get("signal_changed_at_ms") or {})
    for field, value in reported_fields.items():
        if previous.get(field) != value:
            changed_at[field] = captured_at_ms
        body[field] = value
    link_input = payload.get("link") if isinstance(payload.get("link"), dict) else {}
    source = str(payload.get("source", previous.get("source", "S32K344-CAN")))[:80]
    interface_path = str(payload.get("interface_path", link_input.get(
        "interface_path", "S32K344/CAN → Ethernet → Jetson → HTTPS"
    )))[:160]
    body.update({
        "status": "online",
        "can_status": payload.get("can_status", previous.get("can_status", "normal")),
        "captured_at_ms": captured_at_ms,
        "updated_at_ms": received_at_ms,
        "source": source,
        "device_id": str(payload.get("device_id", previous.get("device_id", "S32K344-001")))[:64],
        "signal_validity": dict(previous.get("signal_validity") or {}, **validity),
        "signal_invalid_reasons": dict(previous.get("signal_invalid_reasons") or {}, **invalid_reasons),
        "signal_changed_at_ms": changed_at,
        "latest_change_at_ms": max(changed_at.values()) if changed_at else captured_at_ms,
        "link": {
            "mcu_status": str(link_input.get("mcu_status", "online"))[:24],
            "edge_status": str(link_input.get("edge_status", "online"))[:24],
            "upload_status": str(link_input.get("upload_status", "online"))[:24],
            "edge_received_at_ms": int(link_input.get("edge_received_at_ms", received_at_ms)),
            "interface_path": interface_path,
        },
        "data_mode": "test" if is_test else "real",
    })
    for transient in ("frame_history", "latest_frame", "frame_count", "alerts"):
        body.pop(transient, None)
    state.domains["body"] = body
    state.body_last_seen_ms = received_at_ms

    alerts = body_alerts(body, body["signal_validity"], body["signal_invalid_reasons"], received_at_ms)
    state.body_alerts.clear()
    state.body_alerts.extend(alerts)
    with sqlite3.connect(str(BODY_DATABASE_PATH)) as connection:
        for alert in alerts:
            connection.execute(
                "INSERT INTO body_events (vehicle_id,event_key,level,title,detail,created_at_ms) VALUES (?,?,?,?,?,?)",
                (state.vehicle_id, alert["key"], alert["level"], alert["title"], alert["detail"], alert["created_at_ms"]),
            )

    created_frames = []
    raw_supplied = isinstance(payload.get("raw_frame"), dict) or "can_id" in payload or "data_hex" in payload
    groups = []
    if raw_supplied:
        profile = next((item for item in BODY_SIGNAL_GROUPS.values() if item["can_id"].lower() == str(
            (payload.get("raw_frame") or payload).get("can_id", "")
        ).lower()), None)
        groups = [("raw", profile or {"can_id": "0x0", "frame_name": "UnknownBodyFrame", "cycle_ms": 0}, reported_fields)]
    else:
        for group_name, profile in BODY_SIGNAL_GROUPS.items():
            values = {key: value for key, value in reported_fields.items() if key in profile["fields"]}
            if values:
                groups.append((group_name, profile, values))

    for group_name, profile, group_values in groups:
        state.body_sequence += 1
        fallback_hex = body_payload_bytes(body, state.body_sequence, group_name) if group_name != "raw" else ""
        can_id, dlc, data_hex = normalize_raw_can_frame(payload, profile["can_id"], fallback_hex)
        frame_valid = bool(payload.get("valid", True)) and all(validity.get(key, True) for key in group_values)
        frame = {
            "sequence": state.body_sequence,
            "event": " / ".join(group_values.keys()) or "RAW_FRAME",
            "can_id": can_id,
            "frame_name": str((payload.get("raw_frame") or payload).get("frame_name", profile["frame_name"]))[:64],
            "cycle_ms": int((payload.get("raw_frame") or payload).get("cycle_ms", profile.get("cycle_ms", 0)) or 0),
            "send_type": str((payload.get("raw_frame") or payload).get("send_type", "周期+事件触发"))[:32],
            "dlc": dlc, "data_hex": data_hex, "signal_changes": group_values,
            "vehicle_time_ms": captured_at_ms, "cloud_receive_time_ms": received_at_ms,
            "latency_ms": max(0, received_at_ms - captured_at_ms),
            "valid": frame_valid,
            "invalid_reason": "" if frame_valid else str(payload.get("invalid_reason", "信号或报文有效性校验失败"))[:160],
            "device_id": body["device_id"], "interface_path": interface_path,
            "source": source, "test": bool(is_test),
        }
        state.body_frame_history.appendleft(frame)
        persist_body_frame(state.vehicle_id, frame)
        created_frames.append(frame)
    return state.body_snapshot(), created_frames


def login_page():
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>V-SHIELD · 访问验证</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#060d18;color:#e8f3fa;font-family:Segoe UI,Microsoft YaHei,sans-serif;background-image:linear-gradient(#20d6e705 1px,transparent 1px),linear-gradient(90deg,#20d6e705 1px,transparent 1px),radial-gradient(circle at 50% 30%,#17649626,transparent 40%);background-size:36px 36px,36px 36px,auto}form{width:min(370px,calc(100% - 40px));padding:36px;background:#0b1829ee;border:1px solid #20d6e72b;border-radius:10px;box-shadow:0 25px 80px #0008}small{color:#20d6e7;letter-spacing:.18em}h2{margin:8px 0;font-size:24px;letter-spacing:.12em}p{color:#71879a;font-size:12px;line-height:1.7}input,button{width:100%;box-sizing:border-box;border-radius:5px;padding:13px;margin-top:10px;font:inherit}input{color:#fff;background:#071321;border:1px solid #6fabdb24;outline:none}input:focus{border-color:#20d6e766}button{border:1px solid #20d6e766;background:#20d6e71a;color:#aef8fc;font-weight:600;cursor:pointer}</style></head>
<body><form action="/" method="get"><small>VEHICLE CYBER RANGE</small><h2>V-SHIELD</h2><p>智能网联汽车攻防平台已启用访问保护，请输入云端启动窗口显示的访问口令。</p><input name="access_token" type="password" autocomplete="current-password" placeholder="访问口令" required autofocus><button type="submit">进入安全运营中心</button></form></body></html>"""


registry = Registry()
app = FastAPI(title="V-SHIELD Vehicle Cybersecurity Platform", version="2.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    public_path = request.url.path in {"/healthz", "/readyz"}
    domain_ingest = request.method == "POST" and "/domains/" in request.url.path
    bluetooth_ingest = request.method == "POST" and request.url.path.endswith("/bluetooth")
    wifi_ingest = request.method == "POST" and request.url.path.endswith("/wifi")
    navigation_ingest = request.method == "POST" and request.url.path.endswith("/navigation")
    if (
        DASHBOARD_ACCESS_TOKEN
        and not public_path
        and not domain_ingest
        and not bluetooth_ingest
        and not wifi_ingest
        and not navigation_ingest
    ):
        query_token = request.query_params.get("access_token", "")
        if token_matches(query_token, DASHBOARD_ACCESS_TOKEN):
            response = RedirectResponse(url="/", status_code=303)
            forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            response.set_cookie(
                "vcl_access",
                DASHBOARD_ACCESS_TOKEN,
                max_age=43200,
                httponly=True,
                secure=forwarded_proto == "https",
                samesite="strict",
            )
            return response
        if not token_matches(request.cookies.get("vcl_access", ""), DASHBOARD_ACCESS_TOKEN):
            return HTMLResponse(login_page(), status_code=401)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    # 高德 JS API 的域名白名单校验需要跨域请求携带当前站点来源。
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Camera and microphone remain disabled. Geolocation is allowed only for
    # this origin as an explicitly-labelled fallback when edge GNSS is absent.
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(self)"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def dashboard():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "time_ms": now_ms(), "vehicles": len(registry.vehicles)}


@app.get("/readyz")
async def readyz():
    if not STATIC_DIR.exists():
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return {"status": "ready"}


@app.get("/api/config/map")
async def map_config():
    return {
        "provider": "amap",
        "enabled": bool(AMAP_JS_KEY),
        "key": AMAP_JS_KEY,
        "security_js_code": AMAP_SECURITY_JS_CODE if not AMAP_SERVICE_HOST else "",
        "service_host": AMAP_SERVICE_HOST,
    }


@app.get("/api/vehicles")
async def vehicles():
    values = [state.snapshot() for state in registry.vehicles.values()]
    if not values:
        values = [registry.get("VHC-001").snapshot()]
    return {"vehicles": values, "server_time_ms": now_ms()}


@app.get("/api/vehicles/{vehicle_id}/metrics")
async def vehicle_metrics(vehicle_id: str):
    return registry.get(vehicle_id).snapshot()


@app.get("/api/vehicles/{vehicle_id}/history")
async def vehicle_history(vehicle_id: str):
    return {"vehicle_id": vehicle_id, "samples": list(registry.get(vehicle_id).history)}


@app.get("/api/vehicles/{vehicle_id}/body/history")
async def body_history(vehicle_id: str, limit: int = 100):
    vehicle_id = normalize_vehicle_id(vehicle_id)
    limit = max(1, min(1000, int(limit)))
    with sqlite3.connect(str(BODY_DATABASE_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT sequence,can_id,dlc,data_hex,signals_json,captured_at_ms,
                      received_at_ms,valid,invalid_reason,device_id,interface_path,source,is_test
               FROM body_frames WHERE vehicle_id=? ORDER BY received_at_ms DESC LIMIT ?""",
            (vehicle_id, limit),
        ).fetchall()
    records = []
    for row in rows:
        record = dict(row)
        record["signals"] = json.loads(record.pop("signals_json") or "{}")
        record["valid"] = bool(record["valid"])
        record["is_test"] = bool(record["is_test"])
        records.append(record)
    return {"vehicle_id": vehicle_id, "count": len(records), "frames": records}


@app.post("/api/vehicles/{vehicle_id}/navigation")
async def update_navigation(vehicle_id: str, request: Request):
    if bearer_token(request.headers) != INGEST_TOKEN:
        raise HTTPException(status_code=401, detail="invalid ingest token")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON payload")
    state = registry.get(vehicle_id)
    state.navigation = normalize_navigation(payload)
    return {"accepted": True, "vehicle_id": state.vehicle_id, "navigation": state.navigation}


@app.post("/api/vehicles/{vehicle_id}/domains/{domain}")
async def update_domain(vehicle_id: str, domain: str, request: Request):
    if bearer_token(request.headers) != INGEST_TOKEN:
        raise HTTPException(status_code=401, detail="invalid ingest token")
    state = registry.get(vehicle_id)
    domain = domain.lower()[:32]
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="domain payload must be an object")
    if domain == "body":
        body, frames = record_body_update(state, payload)
        return {
            "accepted": True, "vehicle_id": state.vehicle_id, "domain": domain,
            "body": body, "frames": frames,
        }
    state.domains[domain] = dict(payload, updated_at_ms=now_ms(), status=payload.get("status", "active"))
    return {"accepted": True, "vehicle_id": state.vehicle_id, "domain": domain}


@app.post("/api/vehicles/{vehicle_id}/body/test")
async def test_body_event(vehicle_id: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = str(payload.get("action", "door_fl"))
    if action not in BODY_TEST_ACTIONS:
        raise HTTPException(status_code=400, detail="unsupported body test action")
    state = registry.get(vehicle_id)
    if (
        (state.domains.get("body") or {}).get("data_mode") == "real"
        and state.body_last_seen_ms
        and now_ms() - state.body_last_seen_ms <= BODY_DATA_TIMEOUT_SECONDS * 1000
    ):
        raise HTTPException(status_code=409, detail="real body data is active; test controls are locked")
    current = state.domains.get("body") or {}
    field, label = BODY_TEST_ACTIONS[action]
    if field == "CentralLockState":
        value = "unlocked" if str(current.get(field, "unlocked")).lower() == "locked" else "locked"
    else:
        value = not body_switch(current.get(field, False))
    update = {
        field: value,
        "status": "online",
        "can_status": "normal",
        "source": "页面联调模拟器",
        "captured_at_ms": now_ms() - random.randint(12, 32),
        "transport": {"protocol": "HTTP/JSON", "fragment_count": random.choice([1, 1, 1, 2])},
    }
    if action == "hazard":
        update["TurnLeft"] = value
        update["TurnRight"] = value
    body, frames = record_body_update(state, update, is_test=True)
    return {
        "accepted": True, "vehicle_id": state.vehicle_id, "action": action,
        "label": label, "value": value, "body": body, "frames": frames,
    }


def record_bluetooth_packet(state, payload, is_test=False):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="bluetooth payload must be an object")

    text = payload.get("text", payload.get("data", ""))
    if isinstance(text, (dict, list)):
        parsed = text
        text = json.dumps(text, ensure_ascii=False, separators=(",", ":"))
    else:
        parsed = payload.get("parsed") if isinstance(payload.get("parsed"), dict) else None
        text = str(text)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_BLUETOOTH_TEXT_BYTES:
        raise HTTPException(status_code=413, detail="bluetooth payload is too large")

    hex_text = str(payload.get("hex", "")).strip().lower()
    if not hex_text:
        hex_text = " ".join("{:02x}".format(value) for value in encoded)
    hex_text = " ".join(hex_text.split())[: MAX_BLUETOOTH_TEXT_BYTES * 3]

    state.bluetooth_sequence += 1
    state.bluetooth_last_seen_ms = now_ms()
    record = {
        "sequence": state.bluetooth_sequence,
        "received_at_ms": state.bluetooth_last_seen_ms,
        "source_time_ms": payload.get("source_time_ms"),
        "source": str(payload.get("source", "BLE-UDP"))[:80],
        "text": text,
        "hex": hex_text,
        "byte_count": int(payload.get("byte_count", len(encoded))),
        "rssi": payload.get("rssi"),
        "parsed": parsed,
        "test": bool(is_test or payload.get("test")),
    }
    state.bluetooth_latest = record
    state.bluetooth_history.appendleft(record)
    return record


@app.post("/api/vehicles/{vehicle_id}/bluetooth")
async def ingest_bluetooth(vehicle_id: str, request: Request):
    if not token_matches(bearer_token(request.headers), INGEST_TOKEN):
        raise HTTPException(status_code=401, detail="invalid ingest token")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON payload")
    state = registry.get(vehicle_id)
    record = record_bluetooth_packet(state, payload)
    return {"accepted": True, "vehicle_id": state.vehicle_id, "bluetooth": record}


@app.post("/api/vehicles/{vehicle_id}/bluetooth/test")
async def test_bluetooth(vehicle_id: str):
    state = registry.get(vehicle_id)
    temperature = round(random.uniform(18.0, 36.0), 1)
    humidity = random.randint(35, 85)
    battery = random.randint(45, 100)
    rssi = random.randint(-88, -38)
    text = "TEMP={:.1f};HUM={};BAT={};SEQ={}".format(
        temperature, humidity, battery, state.bluetooth_sequence + 1
    )
    record = record_bluetooth_packet(
        state,
        {
            "text": text,
            "source": "云端随机测试",
            "rssi": rssi,
            "parsed": {
                "temperature_c": temperature,
                "humidity_percent": humidity,
                "battery_percent": battery,
            },
            "test": True,
        },
        is_test=True,
    )
    return {"accepted": True, "vehicle_id": state.vehicle_id, "bluetooth": record}


def record_wifi_packet(state, payload, is_test=False):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="wifi payload must be an object")

    text = payload.get("text", payload.get("data", ""))
    if isinstance(text, (dict, list)):
        parsed = text
        text = json.dumps(text, ensure_ascii=False, separators=(",", ":"))
    else:
        parsed = payload.get("parsed") if isinstance(payload.get("parsed"), dict) else None
        text = str(text)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_WIFI_TEXT_BYTES:
        raise HTTPException(status_code=413, detail="wifi payload is too large")

    hex_text = str(payload.get("hex", "")).strip().lower()
    if not hex_text:
        hex_text = " ".join("{:02x}".format(value) for value in encoded)
    hex_text = " ".join(hex_text.split())[: MAX_WIFI_TEXT_BYTES * 3]

    state.wifi_sequence += 1
    state.wifi_last_seen_ms = now_ms()
    record = {
        "sequence": state.wifi_sequence,
        "received_at_ms": state.wifi_last_seen_ms,
        "source_time_ms": payload.get("source_time_ms"),
        "source": str(payload.get("source", "WIFI-UDP"))[:80],
        "text": text,
        "hex": hex_text,
        "byte_count": int(payload.get("byte_count", len(encoded))),
        "rssi": payload.get("rssi"),
        "parsed": parsed,
        "test": bool(is_test or payload.get("test")),
    }
    state.wifi_latest = record
    state.wifi_history.appendleft(record)
    return record


@app.post("/api/vehicles/{vehicle_id}/wifi")
async def ingest_wifi(vehicle_id: str, request: Request):
    if not token_matches(bearer_token(request.headers), INGEST_TOKEN):
        raise HTTPException(status_code=401, detail="invalid ingest token")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON payload")
    state = registry.get(vehicle_id)
    record = record_wifi_packet(state, payload)
    return {"accepted": True, "vehicle_id": state.vehicle_id, "wifi": record}


@app.post("/api/vehicles/{vehicle_id}/wifi/test")
async def test_wifi(vehicle_id: str):
    state = registry.get(vehicle_id)
    rssi = random.randint(-86, -35)
    channel = random.choice([1, 6, 11])
    tx_kbps = random.randint(120, 2400)
    text = "SSID=VCL-TEST;RSSI={};CH={};TX={}Kbps;SEQ={}".format(
        rssi, channel, tx_kbps, state.wifi_sequence + 1
    )
    record = record_wifi_packet(
        state,
        {
            "text": text,
            "source": "云端随机测试",
            "rssi": rssi,
            "parsed": {
                "ssid": "VCL-TEST",
                "channel": channel,
                "tx_kbps": tx_kbps,
            },
            "test": True,
        },
        is_test=True,
    )
    return {"accepted": True, "vehicle_id": state.vehicle_id, "wifi": record}


@app.websocket("/ws/ingest-fmp4/{vehicle_id}")
async def ingest_fmp4(websocket: WebSocket, vehicle_id: str):
    """Receive already-compressed H.264 fragmented MP4 from the vehicle."""
    token = (
        websocket.headers.get("x-vehicle-token", "")
        or bearer_token(websocket.headers)
        or websocket.query_params.get("token", "")
    )
    if token != INGEST_TOKEN:
        await websocket.close(code=4401, reason="invalid ingest token")
        return

    state = registry.get(vehicle_id)
    await websocket.accept()
    previous = state.fmp4_ingest_websocket
    if previous is not None and previous.client_state == WebSocketState.CONNECTED:
        try:
            await previous.close(code=4001, reason="superseded by a newer H.264 connection")
        except RuntimeError:
            pass
    try:
        await asyncio.wait_for(state.fmp4_ingest_lock.acquire(), timeout=8.0)
    except asyncio.TimeoutError:
        await websocket.close(code=4409, reason="previous H.264 connection did not close")
        return

    state.fmp4_ingest_websocket = websocket
    state.fmp4_connected = True
    state.fmp4_init = None
    state.fmp4_fragments.clear()
    state.received_frames = 0
    state.cloud_dropped_frames = 0
    state.last_sequence = None
    state.metrics.update({
        "status": "online", "transport": "WebSocket / H.264 fMP4",
        "codec": "H.264", "stream_mode": "h264-fmp4",
    })
    LOGGER.info("h264_vehicle_connected vehicle_id=%s", state.vehicle_id)
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            packet = message.get("bytes")
            text = message.get("text")
            if packet is not None:
                await handle_fmp4(state, packet)
            elif text is not None:
                handle_fmp4_telemetry(state, text)
    except WebSocketDisconnect:
        pass
    except Exception:
        LOGGER.exception("h264_ingest_failure vehicle_id=%s", state.vehicle_id)
    finally:
        if state.fmp4_ingest_websocket is websocket:
            state.fmp4_ingest_websocket = None
            state.fmp4_connected = False
            if not state.connected:
                state.metrics["status"] = "offline"
            async with state.fmp4_condition:
                state.fmp4_condition.notify_all()
            LOGGER.warning("h264_vehicle_disconnected vehicle_id=%s", state.vehicle_id)
        state.fmp4_ingest_lock.release()


async def handle_fmp4(state, packet):
    if len(packet) > MAX_FRAME_BYTES or len(packet) <= FMP4_HEADER_SIZE:
        state.invalid_frames += 1
        return
    try:
        metadata = unpack_fmp4(packet)
    except ValueError:
        state.invalid_frames += 1
        return
    if metadata["kind"] == FMP4_KIND_INIT:
        state.fmp4_init = bytes(packet)
        state.fmp4_fragments.clear()
        async with state.fmp4_condition:
            state.fmp4_condition.notify_all()
        return

    sequence = metadata["sequence"]
    frames_per_segment = max(1, int(round(
        float(state.metrics.get("encoded_fps") or 15)
        * float(state.metrics.get("segment_seconds") or 0.5)
    )))
    if state.last_sequence is not None:
        delta = (sequence - state.last_sequence) & 0xFFFFFFFF
        if 1 < delta < 0x80000000:
            state.cloud_dropped_frames += (delta - 1) * frames_per_segment
    state.last_sequence = sequence
    state.sequence = sequence
    state.fmp4_fragments.append((sequence, bytes(packet)))
    state.last_seen_monotonic = time.monotonic()
    state.last_seen_ms = now_ms()
    state.received_frames += frames_per_segment
    state.metrics.update({
        "status": "online", "sequence": sequence,
        "segment_bytes": len(metadata["payload"]),
        "frame_bytes": round(len(metadata["payload"]) / frames_per_segment),
        "ingest_latency_ms": max(0, state.last_seen_ms - metadata["created_at_ms"]),
        "transport": "WebSocket / H.264 fMP4", "codec": "H.264",
    })
    async with state.fmp4_condition:
        state.fmp4_condition.notify_all()


def handle_fmp4_telemetry(state, text):
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        state.invalid_frames += 1
        return
    if not isinstance(payload, dict) or payload.get("type") != "fmp4_telemetry":
        return
    safe_keys = {
        "transport", "codec", "encoder", "pipeline", "width", "height", "encoded_fps", "fps",
        "bitrate_kbps", "upload_kbps", "sent_fragments", "segment_seconds",
        "queue_dropped_fragments", "queue_seconds", "source_status",
        "capture_reconnects", "agent_reconnects",
    }
    state.metrics.update({key: payload[key] for key in safe_keys if key in payload})
    dropped_fragments = int(state.metrics.get("queue_dropped_fragments") or 0)
    frames_per_segment = max(1, int(round(
        float(state.metrics.get("encoded_fps") or 15)
        * float(state.metrics.get("segment_seconds") or 0.5)
    )))
    state.metrics["queue_dropped_frames"] = dropped_fragments * frames_per_segment
    state.metrics["stream_mode"] = "h264-fmp4"
    navigation = payload.get("navigation")
    if isinstance(navigation, dict):
        try:
            state.navigation = normalize_navigation(navigation)
        except HTTPException:
            state.invalid_frames += 1
    state.history.append({
        "time_ms": now_ms(), "fps": state.metrics.get("fps", 0),
        "upload_kbps": state.metrics.get("upload_kbps", 0),
        "ingest_latency_ms": state.metrics.get("ingest_latency_ms", 0),
    })


@app.websocket("/ws/ingest/{vehicle_id}")
async def ingest(websocket: WebSocket, vehicle_id: str):
    # 部分公网隧道会占用或移除 Authorization，因此车端优先使用专用请求头。
    token = (
        websocket.headers.get("x-vehicle-token", "")
        or bearer_token(websocket.headers)
        or websocket.query_params.get("token", "")
    )
    if token != INGEST_TOKEN:
        await websocket.close(code=4401, reason="invalid ingest token")
        return

    state = registry.get(vehicle_id)
    await websocket.accept()
    previous = state.ingest_websocket
    if previous is not None and previous.client_state == WebSocketState.CONNECTED:
        LOGGER.warning("vehicle_connection_takeover vehicle_id=%s", state.vehicle_id)
        try:
            await previous.close(code=4001, reason="superseded by a newer vehicle connection")
        except RuntimeError:
            pass
    try:
        await asyncio.wait_for(state.ingest_lock.acquire(), timeout=8.0)
    except asyncio.TimeoutError:
        await websocket.close(code=4409, reason="previous vehicle connection did not close")
        return
    state.ingest_websocket = websocket
    try:
        # 新连接代表一个新的车端上传会话，清理上一会话的序号与丢帧统计。
        # 否则进程重启后的序号归零和旧累计值会让前端持续显示历史故障。
        state.last_sequence = None
        state.frame_buffer.clear()
        state.cloud_dropped_frames = 0
        state.invalid_frames = 0
        state.received_frames = 0
        state.frame_arrivals.clear()
        state.connected = True
        state.source_address = websocket.client.host if websocket.client else None
        LOGGER.info("vehicle_connected vehicle_id=%s source=%s", state.vehicle_id, state.source_address)
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            text = message.get("text")
            if data is not None:
                await handle_frame(state, data)
            elif text is not None:
                handle_telemetry(state, text)
    except WebSocketDisconnect:
        pass
    except Exception:
        LOGGER.exception("ingest_failure vehicle_id=%s", state.vehicle_id)
    finally:
        if state.ingest_websocket is websocket:
            state.ingest_websocket = None
            state.connected = False
            state.metrics["status"] = "offline"
            async with state.condition:
                state.condition.notify_all()
            LOGGER.warning("vehicle_disconnected vehicle_id=%s", state.vehicle_id)
        state.ingest_lock.release()


async def handle_frame(state, packet):
    if len(packet) > MAX_FRAME_BYTES or len(packet) <= HEADER_SIZE:
        state.invalid_frames += 1
        return
    try:
        metadata = unpack_frame(packet)
    except ValueError:
        state.invalid_frames += 1
        return

    sequence = metadata["sequence"]
    if state.last_sequence is not None:
        delta = (sequence - state.last_sequence) & 0xFFFFFFFF
        if 1 < delta < 0x80000000:
            state.cloud_dropped_frames += delta - 1
    state.last_sequence = sequence
    state.sequence = sequence
    state.frame = bytes(packet)
    state.frame_buffer.append((sequence, state.frame))
    state.received_frames += 1
    state.last_seen_monotonic = time.monotonic()
    state.frame_arrivals.append(state.last_seen_monotonic)
    while state.frame_arrivals and state.last_seen_monotonic - state.frame_arrivals[0] > 2.0:
        state.frame_arrivals.popleft()
    if len(state.frame_arrivals) >= 2:
        arrival_span = state.frame_arrivals[-1] - state.frame_arrivals[0]
        cloud_fps = (len(state.frame_arrivals) - 1) / max(0.001, arrival_span)
    else:
        cloud_fps = 0.0
    state.last_seen_ms = now_ms()
    state.metrics.update(
        {
            "status": "online",
            "sequence": sequence,
            "width": metadata["width"],
            "height": metadata["height"],
            "jpeg_quality": metadata["quality"],
            "frame_bytes": len(metadata["jpeg"]),
            "fps": round(cloud_fps, 1),
            "ingest_latency_ms": max(0, state.last_seen_ms - metadata["captured_at_ms"]),
        }
    )
    async with state.condition:
        state.condition.notify_all()


def handle_telemetry(state, text):
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        state.invalid_frames += 1
        return
    if not isinstance(payload, dict) or payload.get("type") != "telemetry":
        return
    safe = {
        key: value
        for key, value in payload.items()
        if key not in {"domains", "navigation", "type", "fps"}
    }
    if "fps" in payload:
        safe["agent_fps"] = payload["fps"]
    state.metrics.update(safe)
    navigation = payload.get("navigation")
    if isinstance(navigation, dict):
        try:
            state.navigation = normalize_navigation(navigation)
        except HTTPException:
            state.invalid_frames += 1
    domains = payload.get("domains")
    if isinstance(domains, dict):
        for name, values in domains.items():
            if isinstance(values, dict):
                state.domains[str(name)[:32]] = values
    sample = {
        "time_ms": now_ms(),
        "fps": state.metrics.get("fps", 0),
        "upload_kbps": state.metrics.get("upload_kbps", 0),
        "ingest_latency_ms": state.metrics.get("ingest_latency_ms", 0),
    }
    state.history.append(sample)


@app.websocket("/ws/live-fmp4/{vehicle_id}")
async def live_fmp4(websocket: WebSocket, vehicle_id: str):
    """Relay compressed fMP4 without decoding or re-encoding it in the cloud."""
    if DASHBOARD_ACCESS_TOKEN and not token_matches(
        websocket.cookies.get("vcl_access", ""), DASHBOARD_ACCESS_TOKEN
    ):
        await websocket.close(code=4401, reason="dashboard authentication required")
        return
    state = registry.get(vehicle_id)
    await websocket.accept()
    state.viewer_count += 1
    last_init = None
    last_sequence = None
    try:
        while True:
            init_packet = state.fmp4_init
            fragments = list(state.fmp4_fragments)
            if init_packet is None or not fragments:
                async with state.fmp4_condition:
                    try:
                        await asyncio.wait_for(state.fmp4_condition.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                continue
            if init_packet is not last_init:
                await websocket.send_bytes(init_packet)
                last_init = init_packet
                last_sequence = None
            if last_sequence is None:
                # Do not replay video captured before this viewer connected.
                # Starting from the newest independently-decodable fragment
                # still lets the browser build its deep playback buffer.
                sequence, packet = fragments[-1]
            else:
                cursor = next((index for index, item in enumerate(fragments) if item[0] == last_sequence), None)
                if cursor is None:
                    # The viewer fell farther behind than the bounded cloud FIFO.
                    # Resume at the oldest still-retained independently decodable
                    # fragment rather than jumping all the way to live.
                    sequence, packet = fragments[0]
                elif cursor + 1 < len(fragments):
                    sequence, packet = fragments[cursor + 1]
                else:
                    async with state.fmp4_condition:
                        try:
                            await asyncio.wait_for(state.fmp4_condition.wait(), timeout=1.0)
                        except asyncio.TimeoutError:
                            pass
                    continue
            await websocket.send_bytes(packet)
            last_sequence = sequence
    except Exception:
        pass
    finally:
        state.viewer_count = max(0, state.viewer_count - 1)


@app.websocket("/ws/live/{vehicle_id}")
async def live(websocket: WebSocket, vehicle_id: str):
    if DASHBOARD_ACCESS_TOKEN and not token_matches(
        websocket.cookies.get("vcl_access", ""), DASHBOARD_ACCESS_TOKEN
    ):
        await websocket.close(code=4401, reason="dashboard authentication required")
        return
    state = registry.get(vehicle_id)
    await websocket.accept()
    state.viewer_count += 1
    last_sequence = None
    playback_started = False
    next_send_at = asyncio.get_event_loop().time()
    try:
        while True:
            frames = list(state.frame_buffer)
            if not frames:
                await asyncio.sleep(0.03)
                continue
            encoded_fps = float(state.metrics.get("encoded_fps") or state.metrics.get("agent_fps") or 15)
            playback_fps = max(10.0, min(20.0, encoded_fps))
            prebuffer_frames = max(5, int(round(playback_fps)))
            if not playback_started:
                if len(frames) < prebuffer_frames and state.connected:
                    await asyncio.sleep(0.03)
                    continue
                sequence, frame = frames[max(0, len(frames) - prebuffer_frames)]
                playback_started = True
            else:
                cursor = next(
                    (index for index, (sequence, _) in enumerate(frames) if sequence == last_sequence),
                    None,
                )
                if cursor is None:
                    sequence, frame = frames[max(0, len(frames) - prebuffer_frames)]
                elif cursor + 1 >= len(frames):
                    await asyncio.sleep(0.01)
                    continue
                else:
                    sequence, frame = frames[cursor + 1]
                    latest_age = unpack_frame(frames[-1][1])["captured_at_ms"] - unpack_frame(frame)["captured_at_ms"]
                    if latest_age > 18000:
                        sequence, frame = frames[max(0, len(frames) - prebuffer_frames)]
            await websocket.send_bytes(frame)
            last_sequence = sequence
            next_send_at = max(next_send_at + 1.0 / playback_fps, asyncio.get_event_loop().time())
            await asyncio.sleep(max(0.0, next_send_at - asyncio.get_event_loop().time()))
    except Exception:
        pass
    finally:
        state.viewer_count = max(0, state.viewer_count - 1)


@app.websocket("/ws/metrics/{vehicle_id}")
async def metrics_socket(websocket: WebSocket, vehicle_id: str):
    if DASHBOARD_ACCESS_TOKEN and not token_matches(
        websocket.cookies.get("vcl_access", ""), DASHBOARD_ACCESS_TOKEN
    ):
        await websocket.close(code=4401, reason="dashboard authentication required")
        return
    state = registry.get(vehicle_id)
    await websocket.accept()
    try:
        while websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json(state.snapshot())
            await asyncio.sleep(1)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Vehicle video cloud relay")
    parser.add_argument("--host", default=os.getenv("CLOUD_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CLOUD_PORT", "8000")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    if INGEST_TOKEN == "change-me-in-production":
        LOGGER.warning("using_default_ingest_token set VEHICLE_INGEST_TOKEN before deployment")
    import uvicorn

    uvicorn.run("server:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
