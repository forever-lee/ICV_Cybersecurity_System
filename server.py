"""Cloud relay and V-SHIELD connected-vehicle cybersecurity console."""

import argparse
import asyncio
import json
import logging
import math
import os
import random
import re
import secrets
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
BLUETOOTH_CONTROL_OFFLINE_AFTER_SECONDS = float(
    os.getenv("BLUETOOTH_CONTROL_OFFLINE_AFTER_SECONDS", "5")
)
MAX_BLUETOOTH_TEXT_BYTES = int(os.getenv("MAX_BLUETOOTH_TEXT_BYTES", "2048"))
WIFI_OFFLINE_AFTER_SECONDS = float(os.getenv("WIFI_OFFLINE_AFTER_SECONDS", "10"))
MAX_WIFI_TEXT_BYTES = int(os.getenv("MAX_WIFI_TEXT_BYTES", "4096"))
NAVIGATION_OFFLINE_AFTER_SECONDS = float(os.getenv("NAVIGATION_OFFLINE_AFTER_SECONDS", "10"))
AMAP_JS_KEY = os.getenv("AMAP_JS_KEY", "").strip()
AMAP_SECURITY_JS_CODE = os.getenv("AMAP_SECURITY_JS_CODE", "").strip()
AMAP_SERVICE_HOST = os.getenv("AMAP_SERVICE_HOST", "").strip()
CLOUD_FRAME_BUFFER_FRAMES = max(30, int(os.getenv("CLOUD_FRAME_BUFFER_FRAMES", "300")))
FMP4_BUFFER_SEGMENTS = max(3, int(os.getenv("FMP4_BUFFER_SEGMENTS", "4")))
S32_COMPACT_TIME_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
S32_COMPACT_TIME_PATTERN = re.compile(r"^(S32K344_HELLO_\d{2})([0-9A-Za-z_-]{3})$")
S32_SOURCE_TIME_PATTERN = re.compile(r"(?:^|;)TS=(\d{13})(?=;|$)")
S32_SHORT_ABNORMAL_PATTERN = re.compile(r"^S32K344_ABNORMAL_(\d{2})$")
S32_NORMAL_PATTERN = re.compile(r"^S32K344_HELLO_\d{2,4}$")
S32_ABNORMAL_PATTERN = re.compile(
    r"^S32K344_ABNORMAL_\d{4};TYPE=BLE_ATTACK;LEVEL=HIGH$"
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("vehicle-cloud")


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
        self.bluetooth_abnormal_count = 0
        self.bluetooth_latest_abnormal = None
        self.bluetooth_abnormal_history = deque(maxlen=60)
        self.bluetooth_abnormal_fingerprints = set()
        self.bluetooth_abnormal_fingerprint_order = deque()
        self.bluetooth_send_times = {}
        self.bluetooth_control_sequence = 0
        self.bluetooth_control_last_seen_ms = 0
        self.bluetooth_control_state = "unknown"
        self.bluetooth_control_pending = None
        self.bluetooth_control_last_ack = None
        self.wifi_sequence = 0
        self.wifi_last_seen_ms = 0
        self.wifi_latest = None
        self.wifi_history = deque(maxlen=60)
        self.navigation = None

    def online(self):
        return (self.connected or self.fmp4_connected) and (time.monotonic() - self.last_seen_monotonic) < OFFLINE_AFTER_SECONDS

    def snapshot(self):
        data = dict(self.metrics)
        data.update(
            {
                "vehicle_id": self.vehicle_id,
                "status": "online" if self.online() else "offline",
                "last_seen_ms": self.last_seen_ms,
                "viewer_count": self.viewer_count,
                "received_frames": self.received_frames,
                "cloud_dropped_frames": self.cloud_dropped_frames,
                "invalid_frames": self.invalid_frames,
                "domains": self.domains,
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
            "abnormal_count": self.bluetooth_abnormal_count,
            "latest_abnormal": self.bluetooth_latest_abnormal,
            "abnormal_history": list(self.bluetooth_abnormal_history)[:12],
            "control": self.bluetooth_control_snapshot(),
        }

    def bluetooth_control_snapshot(self):
        agent_online = bool(
            self.bluetooth_control_last_seen_ms
            and now_ms() - self.bluetooth_control_last_seen_ms
            < BLUETOOTH_CONTROL_OFFLINE_AFTER_SECONDS * 1000
        )
        pending = self.bluetooth_control_pending
        return {
            "agent_status": "online" if agent_online else "offline",
            "last_seen_ms": self.bluetooth_control_last_seen_ms,
            "state": self.bluetooth_control_state,
            "pending": bool(pending),
            "pending_command": pending.get("command") if pending else None,
            "pending_id": pending.get("id") if pending else None,
            "last_ack": self.bluetooth_control_last_ack,
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
    public_path = request.url.path in {
        "/healthz", "/readyz", "/public/live/VHC-001",
    }
    domain_ingest = request.method == "POST" and "/domains/" in request.url.path
    bluetooth_ingest = request.method == "POST" and (
        request.url.path.endswith("/bluetooth")
        or request.url.path.endswith("/bluetooth/timing")
        or request.url.path.endswith("/bluetooth/control/ack")
    )
    bluetooth_control_poll = (
        request.method == "GET" and request.url.path.endswith("/bluetooth/control/next")
    )
    wifi_ingest = request.method == "POST" and request.url.path.endswith("/wifi")
    navigation_ingest = request.method == "POST" and request.url.path.endswith("/navigation")
    if (
        DASHBOARD_ACCESS_TOKEN
        and not public_path
        and not domain_ingest
        and not bluetooth_ingest
        and not bluetooth_control_poll
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
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def dashboard():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/public/live/VHC-001")
async def public_vhc001_live():
    """Token-free, video-only viewer for the explicitly published vehicle."""
    return FileResponse(str(STATIC_DIR / "public_vhc001.html"))


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
    state.domains[domain] = dict(payload, updated_at_ms=now_ms(), status=payload.get("status", "active"))
    return {"accepted": True, "vehicle_id": state.vehicle_id, "domain": domain}


def bluetooth_abnormal_reason(payload, text, parsed=None):
    """Return a displayable reason when a Bluetooth packet is marked abnormal."""
    truthy = {"1", "true", "yes", "on", "abnormal", "anomaly", "alert", "error", "fault"}
    for container in (payload, payload.get("parsed"), parsed):
        if not isinstance(container, dict):
            continue
        for key in ("abnormal", "is_abnormal", "anomaly", "alarm", "alert"):
            value = container.get(key)
            if value is True or (value is not None and str(value).strip().lower() in truthy):
                return "设备显式异常标记"
        for key in ("type", "status", "state", "level", "event"):
            value = str(container.get(key, "")).strip().lower()
            if value in {"abnormal", "anomaly", "alert", "error", "fault", "attack", "invalid", "warning", "critical"}:
                return "设备状态：{}".format(value.upper())

    message = str(text).strip()
    if any(keyword in message for keyword in ("异常", "告警", "故障", "攻击")):
        return "报文包含中文异常标识"
    marker = re.search(
        r"(?:^|[^A-Z0-9])"
        r"(?<!NOT_)(?<!NO_)"
        r"(ABNORMAL(?:_FRAME)?|ANOMALY|ALERT|FAULT|ATTACK|INVALID|WARN(?:ING)?|ERR(?:OR)?(?:_FRAME)?)"
        r"(?!\s*[:=]\s*(?:0|FALSE|NO|OFF)(?:$|[^A-Z0-9]))"
        r"(?:$|[^A-Z0-9])",
        message.upper(),
    )
    if marker:
        return "报文异常标识：{}".format(marker.group(1))
    return None


def bluetooth_number(value, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < minimum or number > maximum:
        return None
    return int(number) if number.is_integer() else round(number, 2)


def bluetooth_text_number(text, field_names, minimum, maximum):
    pattern = r"(?:^|[;,\s])(?:{})\s*[=:]\s*(-?\d+(?:\.\d+)?)".format("|".join(field_names))
    match = re.search(pattern, str(text), re.IGNORECASE)
    return bluetooth_number(match.group(1), minimum, maximum) if match else None


def normalize_s32_bluetooth_text(text):
    """Strip transport-only timing metadata from an S32 telemetry frame."""
    compact_match = S32_COMPACT_TIME_PATTERN.fullmatch(text)
    if compact_match:
        tick = 0
        for character in compact_match.group(2):
            tick = (tick << 6) | S32_COMPACT_TIME_ALPHABET.index(character)
        return compact_match.group(1), None, tick, True

    timestamp_match = S32_SOURCE_TIME_PATTERN.search(text)
    if timestamp_match:
        clean_text = S32_SOURCE_TIME_PATTERN.sub("", text).strip(";")
        return clean_text, int(timestamp_match.group(1)), None, True

    short_abnormal_match = S32_SHORT_ABNORMAL_PATTERN.fullmatch(text)
    if short_abnormal_match:
        clean_text = "S32K344_ABNORMAL_{:04d};TYPE=BLE_ATTACK;LEVEL=HIGH".format(
            int(short_abnormal_match.group(1))
        )
        return clean_text, None, None, True

    return text, None, None, False


def is_valid_s32_bluetooth_text(text):
    return bool(
        S32_NORMAL_PATTERN.fullmatch(text)
        or S32_ABNORMAL_PATTERN.fullmatch(text)
    )


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
    source = str(payload.get("source", "BLE-UDP"))[:80]
    text, embedded_source_time_ms, embedded_source_tick_ms, timing_removed = (
        normalize_s32_bluetooth_text(text)
    )
    if source.startswith(("S32K344-ETH@", "BLE-GATEWAY@")) and not is_valid_s32_bluetooth_text(text):
        raise HTTPException(status_code=422, detail="incomplete S32K344 bluetooth frame")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_BLUETOOTH_TEXT_BYTES:
        raise HTTPException(status_code=413, detail="bluetooth payload is too large")

    hex_text = "" if timing_removed else str(payload.get("hex", "")).strip().lower()
    if not hex_text:
        hex_text = " ".join("{:02x}".format(value) for value in encoded)
    hex_text = " ".join(hex_text.split())[: MAX_BLUETOOTH_TEXT_BYTES * 3]

    state.bluetooth_sequence += 1
    state.bluetooth_last_seen_ms = now_ms()
    abnormal_reason = bluetooth_abnormal_reason(payload, text, parsed)
    rssi = bluetooth_number(payload.get("rssi"), -127, 20)
    if rssi is None:
        rssi = bluetooth_text_number(text, ("RSSI",), -127, 20)
    latency_ms = bluetooth_number(payload.get("latency_ms", payload.get("delay_ms")), 0, 600000)
    if latency_ms is None:
        latency_ms = bluetooth_text_number(text, ("LATENCY", "DELAY"), 0, 600000)
    source_time_ms = bluetooth_number(
        payload.get("source_time_ms", embedded_source_time_ms), 1, 9999999999999
    )
    source_tick_ms = bluetooth_number(
        payload.get("source_tick_ms", embedded_source_tick_ms), 0, (1 << 18) - 1
    )
    clock_unsynced = False
    registered_send_time_ms = state.bluetooth_send_times.pop(text, None)
    if latency_ms is None and registered_send_time_ms is not None:
        registered_delta_ms = state.bluetooth_last_seen_ms - registered_send_time_ms
        if registered_delta_ms >= 0:
            latency_ms = registered_delta_ms
    if latency_ms is None and source_time_ms is not None:
        clock_delta_ms = state.bluetooth_last_seen_ms - source_time_ms
        if clock_delta_ms >= -100:
            # The sender and this service normally share the laptop clock.
            # Tolerate a very small scheduling/clock adjustment around zero.
            latency_ms = max(0, clock_delta_ms)
        else:
            clock_unsynced = True
    if latency_ms is None and source_tick_ms is not None:
        latency_ms = (
            (state.bluetooth_last_seen_ms & ((1 << 18) - 1))
            - int(source_tick_ms)
        ) & ((1 << 18) - 1)
    record = {
        "sequence": state.bluetooth_sequence,
        "received_at_ms": state.bluetooth_last_seen_ms,
        "source_time_ms": source_time_ms,
        "source_tick_ms": source_tick_ms,
        "latency_ms": latency_ms,
        "clock_unsynced": clock_unsynced,
        "source": source,
        "text": text,
        "hex": hex_text,
        "byte_count": (
            len(encoded)
            if timing_removed
            else int(payload.get("byte_count", len(encoded)))
        ),
        "rssi": rssi,
        "parsed": parsed,
        "test": bool(is_test or payload.get("test")),
        "abnormal": bool(abnormal_reason),
        "abnormal_reason": abnormal_reason,
        "abnormal_duplicate": False,
    }
    if abnormal_reason:
        fingerprint = hex_text or text.strip().lower()
        if fingerprint in state.bluetooth_abnormal_fingerprints:
            record["abnormal_duplicate"] = True
        else:
            if len(state.bluetooth_abnormal_fingerprint_order) >= 512:
                oldest = state.bluetooth_abnormal_fingerprint_order.popleft()
                state.bluetooth_abnormal_fingerprints.discard(oldest)
            state.bluetooth_abnormal_fingerprint_order.append(fingerprint)
            state.bluetooth_abnormal_fingerprints.add(fingerprint)
            state.bluetooth_abnormal_count += 1
            state.bluetooth_latest_abnormal = record
            state.bluetooth_abnormal_history.appendleft(record)
    else:
        state.bluetooth_latest = record
        state.bluetooth_history.appendleft(record)
    return record


@app.post("/api/vehicles/{vehicle_id}/bluetooth/timing")
async def register_bluetooth_send_time(vehicle_id: str, request: Request):
    """Register the laptop send time without adding metadata to the BLE frame."""
    if not token_matches(bearer_token(request.headers), INGEST_TOKEN):
        raise HTTPException(status_code=401, detail="invalid ingest token")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="timing payload must be an object")

    text = str(payload.get("text", "")).strip().rstrip("#")
    text, _, _, _ = normalize_s32_bluetooth_text(text)
    if not is_valid_s32_bluetooth_text(text):
        raise HTTPException(status_code=400, detail="invalid S32K344 frame text")
    sent_at_ms = bluetooth_number(payload.get("sent_at_ms"), 1, 9999999999999)
    if sent_at_ms is None or abs(now_ms() - sent_at_ms) > 600000:
        raise HTTPException(status_code=400, detail="invalid send timestamp")

    state = registry.get(vehicle_id)
    state.bluetooth_send_times[text] = int(sent_at_ms)
    cutoff = now_ms() - 30000
    stale = [key for key, value in state.bluetooth_send_times.items() if value < cutoff]
    for key in stale:
        state.bluetooth_send_times.pop(key, None)
    return {"accepted": True, "vehicle_id": state.vehicle_id, "text": text}


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
async def test_bluetooth(vehicle_id: str, abnormal: bool = False):
    state = registry.get(vehicle_id)
    temperature = round(random.uniform(18.0, 36.0), 1)
    humidity = random.randint(35, 85)
    battery = random.randint(45, 100)
    rssi = random.randint(-88, -38)
    if abnormal:
        text = "TYPE=ABNORMAL;CODE=0xE1;TEMP={:.1f};SEQ={}".format(
            temperature, state.bluetooth_sequence + 1
        )
    else:
        text = "TEMP={:.1f};HUM={};BAT={};SEQ={}".format(
            temperature, humidity, battery, state.bluetooth_sequence + 1
        )
    record = record_bluetooth_packet(
        state,
        {
            "text": text,
            "source_time_ms": now_ms() - random.randint(8, 80),
            "source": "云端随机测试",
            "rssi": rssi,
            "parsed": {
                "temperature_c": temperature,
                "humidity_percent": humidity,
                "battery_percent": battery,
                "status": "abnormal" if abnormal else "normal",
            },
            "test": True,
        },
        is_test=True,
    )
    return {"accepted": True, "vehicle_id": state.vehicle_id, "bluetooth": record}


@app.post("/api/vehicles/{vehicle_id}/bluetooth/control")
async def queue_bluetooth_control(vehicle_id: str, request: Request):
    """Queue an idempotent on/off command from the authenticated dashboard."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="control payload must be an object")
    command = str(payload.get("command", "")).strip().lower()
    if command not in {"on", "off"}:
        raise HTTPException(status_code=400, detail="command must be on or off")

    state = registry.get(vehicle_id)
    state.bluetooth_control_sequence += 1
    state.bluetooth_control_pending = {
        "id": state.bluetooth_control_sequence,
        "command": command,
        "created_at_ms": now_ms(),
    }
    return {
        "accepted": True,
        "vehicle_id": state.vehicle_id,
        "control": state.bluetooth_control_snapshot(),
    }


@app.get("/api/vehicles/{vehicle_id}/bluetooth/control/next")
async def next_bluetooth_control(vehicle_id: str, request: Request):
    """Return the pending command to the authenticated PC-side BLE gateway."""
    if not token_matches(bearer_token(request.headers), INGEST_TOKEN):
        raise HTTPException(status_code=401, detail="invalid ingest token")
    state = registry.get(vehicle_id)
    state.bluetooth_control_last_seen_ms = now_ms()
    return {
        "vehicle_id": state.vehicle_id,
        "server_time_ms": now_ms(),
        "command": state.bluetooth_control_pending,
    }


@app.post("/api/vehicles/{vehicle_id}/bluetooth/control/ack")
async def acknowledge_bluetooth_control(vehicle_id: str, request: Request):
    """Record the gateway result after the board ACK has been received or timed out."""
    if not token_matches(bearer_token(request.headers), INGEST_TOKEN):
        raise HTTPException(status_code=401, detail="invalid ingest token")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="ack payload must be an object")

    state = registry.get(vehicle_id)
    state.bluetooth_control_last_seen_ms = now_ms()
    pending = state.bluetooth_control_pending
    try:
        command_id = int(payload.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid command id")
    if not pending or command_id != pending.get("id"):
        raise HTTPException(status_code=409, detail="command is no longer pending")

    status = str(payload.get("status", "error")).strip().lower()
    if status not in {"applied", "error"}:
        raise HTTPException(status_code=400, detail="status must be applied or error")
    ack = {
        "id": command_id,
        "command": pending["command"],
        "status": status,
        "message": str(payload.get("message", ""))[:200],
        "serial_port": str(payload.get("serial_port", ""))[:40],
        "acknowledged_at_ms": now_ms(),
    }
    state.bluetooth_control_last_ack = ack
    state.bluetooth_control_pending = None
    if status == "applied":
        state.bluetooth_control_state = pending["command"]
    return {
        "accepted": True,
        "vehicle_id": state.vehicle_id,
        "control": state.bluetooth_control_snapshot(),
    }


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
    """Receive already-compressed H.264/H.265 fragmented MP4 from a vehicle."""
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
            await previous.close(code=4001, reason="superseded by a newer fMP4 connection")
        except RuntimeError:
            pass
    try:
        await asyncio.wait_for(state.fmp4_ingest_lock.acquire(), timeout=8.0)
    except asyncio.TimeoutError:
        await websocket.close(code=4409, reason="previous fMP4 connection did not close")
        return

    state.fmp4_ingest_websocket = websocket
    state.fmp4_connected = True
    state.fmp4_init = None
    state.fmp4_fragments.clear()
    state.received_frames = 0
    state.cloud_dropped_frames = 0
    state.last_sequence = None
    state.metrics.update({
        "status": "online", "transport": "WebSocket / fMP4",
        "codec": "H.264/H.265", "stream_mode": "fmp4",
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
        "transport": state.metrics.get("transport") or "WebSocket / fMP4",
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
    state.metrics["stream_mode"] = "fmp4"
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


async def relay_fmp4(websocket: WebSocket, vehicle_id: str):
    """Relay compressed fMP4 without decoding or re-encoding it in the cloud."""
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
                # A passthrough fragment isn't guaranteed to begin with an IDR
                # unless the camera GOP exactly matches the mux fragment size.
                # Replay only the short cloud window so MSE sees the preceding
                # keyframe; the browser then seeks near the buffered live edge.
                sequence, packet = fragments[0]
            else:
                cursor = next((index for index, item in enumerate(fragments) if item[0] == last_sequence), None)
                if cursor is None:
                    # The viewer fell farther behind than the bounded cloud FIFO.
                    # Restart from the short window's oldest fragment so the
                    # decoder receives a keyframe before seeking to the live edge.
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


@app.websocket("/ws/live-fmp4/{vehicle_id}")
async def live_fmp4(websocket: WebSocket, vehicle_id: str):
    if DASHBOARD_ACCESS_TOKEN and not token_matches(
        websocket.cookies.get("vcl_access", ""), DASHBOARD_ACCESS_TOKEN
    ):
        await websocket.close(code=4401, reason="dashboard authentication required")
        return
    await relay_fmp4(websocket, vehicle_id)


@app.websocket("/ws/public/live-fmp4/VHC-001")
async def public_vhc001_fmp4(websocket: WebSocket):
    """Unauthenticated video-only relay; does not expose metrics or controls."""
    await relay_fmp4(websocket, "VHC-001")


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
