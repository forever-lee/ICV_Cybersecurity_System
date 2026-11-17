#!/usr/bin/env python3
"""Upload the latest edge GNSS sample independently of the video pipeline."""

import argparse
import json
import logging
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("navigation-uploader")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_NAVIGATION_FILE = os.path.join(BASE_DIR, "navigation_live.json")


def now_ms():
    return int(time.time() * 1000)


def environment_enabled(name, default="0"):
    return os.getenv(name, default).strip().lower() not in {
        "0", "false", "no", "off", ""
    }


def read_navigation(path):
    """Read one atomically-written navigation sample."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict) and isinstance(data.get("navigation"), dict):
        data = data["navigation"]
    if not isinstance(data, dict):
        raise ValueError("navigation payload must be an object")
    if data.get("latitude") is None or data.get("longitude") is None:
        raise ValueError("navigation payload has no position")
    return dict(data)


def sample_key(payload):
    """Return a stable key so an unchanged file is not uploaded repeatedly."""
    return (
        payload.get("captured_at_ms"),
        payload.get("longitude"),
        payload.get("latitude"),
        payload.get("speed_kph"),
        payload.get("heading_deg"),
    )


def navigation_is_fresh(payload, maximum_age_seconds, current_time_ms=None):
    try:
        captured_at_ms = int(payload.get("captured_at_ms") or 0)
    except (TypeError, ValueError):
        return False
    current_time_ms = now_ms() if current_time_ms is None else int(current_time_ms)
    age_ms = current_time_ms - captured_at_ms
    return captured_at_ms > 0 and 0 <= age_ms <= maximum_age_seconds * 1000


def tokenized_url(url, token):
    if not token:
        return url
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("token", token)
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(query),
        parsed.fragment,
    ))


def fetch_ip_geolocation(url, token, timeout, opener=urlopen):
    """Resolve the Jetson public egress IP to a coarse WGS-84 position."""
    request = Request(
        tokenized_url(url, token),
        headers={
            "Accept": "application/json",
            "User-Agent": "vshield-jetson/1.0",
        },
    )
    with opener(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("IP geolocation response must be an object")

    latitude = data.get("latitude")
    longitude = data.get("longitude")
    if (latitude is None or longitude is None) and data.get("loc"):
        parts = str(data["loc"]).split(",", 1)
        if len(parts) == 2:
            latitude, longitude = parts
    if (latitude is None or longitude is None) and isinstance(data.get("geo"), dict):
        latitude = data["geo"].get("latitude")
        longitude = data["geo"].get("longitude")

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        raise ValueError("IP geolocation response has no coordinates")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("IP geolocation coordinates are out of range")
    return {
        "latitude": latitude,
        "longitude": longitude,
        "public_ip": str(data.get("ip") or ""),
        "city": str(data.get("city") or ""),
        "region": str(data.get("region") or ""),
    }


def build_ip_navigation(geolocation, accuracy_m, captured_at_ms=None):
    return {
        "latitude": geolocation["latitude"],
        "longitude": geolocation["longitude"],
        "coordinate_system": "WGS84",
        "source": "EDGE-IP-GEO",
        "accuracy_m": float(accuracy_m),
        "captured_at_ms": now_ms() if captured_at_ms is None else int(captured_at_ms),
    }


def build_payload(navigation, edge_session_id="", edge_session_started_at_ms=0):
    payload = dict(navigation)
    if edge_session_id:
        payload["edge_session_id"] = edge_session_id
    if edge_session_started_at_ms:
        payload["edge_session_started_at_ms"] = int(edge_session_started_at_ms)
    return payload


def upload_navigation(cloud_url, vehicle_id, token, payload, timeout, opener=urlopen):
    endpoint = "{}/api/vehicles/{}/navigation".format(
        cloud_url.rstrip("/"), quote(vehicle_id, safe="")
    )
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        endpoint,
        data=encoded,
        headers={
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json",
            "User-Agent": "vshield-navigation-uploader/1",
        },
        method="POST",
    )
    with opener(request, timeout=timeout) as response:
        status = response.getcode()
        body = response.read()
    if status < 200 or status >= 300:
        raise RuntimeError("navigation upload returned HTTP {}".format(status))
    return json.loads(body.decode("utf-8")) if body else {"accepted": True}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Upload navigation_live.json without depending on a camera uploader"
    )
    parser.add_argument(
        "--cloud",
        default=os.getenv("CLOUD_HTTP_URL", ""),
        help="Cloud HTTP base URL",
    )
    parser.add_argument(
        "--vehicle-id",
        default=os.getenv("VEHICLE_ID", os.getenv("EDGE_VEHICLE_ID", "")),
    )
    parser.add_argument(
        "--navigation-file",
        default=os.getenv("VEHICLE_NAVIGATION_FILE", DEFAULT_NAVIGATION_FILE),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("NAVIGATION_UPLOAD_INTERVAL_SECONDS", "1")),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("NAVIGATION_HTTP_TIMEOUT_SECONDS", "5")),
    )
    return parser.parse_args(argv)


def main(argv=None):
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = parse_args(argv)
    token = os.getenv("VEHICLE_INGEST_TOKEN", "").strip()
    ip_fallback_enabled = environment_enabled(
        "NAVIGATION_IP_FALLBACK_ENABLED", "0"
    )
    ip_geolocation_url = os.getenv(
        "NAVIGATION_IP_GEO_URL", "https://ipinfo.io/json"
    ).strip()
    ip_geolocation_token = os.getenv("IPINFO_TOKEN", "").strip()
    ip_fallback_after_seconds = float(os.getenv(
        "NAVIGATION_IP_FALLBACK_AFTER_SECONDS", "15"
    ))
    ip_geolocation_refresh_seconds = float(os.getenv(
        "NAVIGATION_IP_GEO_REFRESH_SECONDS", "600"
    ))
    ip_fallback_upload_seconds = float(os.getenv(
        "NAVIGATION_IP_FALLBACK_UPLOAD_SECONDS", "5"
    ))
    ip_geolocation_accuracy_m = float(os.getenv(
        "NAVIGATION_IP_GEO_ACCURACY_M", "50000"
    ))
    edge_session_id = os.getenv("EDGE_SESSION_ID", "").strip()
    try:
        edge_session_started_at_ms = int(
            os.getenv("EDGE_SESSION_STARTED_AT_MS", "0") or 0
        )
    except ValueError:
        raise SystemExit("EDGE_SESSION_STARTED_AT_MS must be an integer")

    if not args.cloud.strip():
        raise SystemExit("CLOUD_HTTP_URL is required")
    if not args.vehicle_id.strip():
        raise SystemExit("VEHICLE_ID is required")
    if not token or token == "change-me-in-production":
        raise SystemExit("VEHICLE_INGEST_TOKEN is required")
    if args.interval <= 0 or args.timeout <= 0:
        raise SystemExit("navigation interval and timeout must be positive")
    if ip_fallback_enabled and (
        not ip_geolocation_url
        or ip_fallback_after_seconds <= 0
        or ip_geolocation_refresh_seconds <= 0
        or ip_fallback_upload_seconds <= 0
        or ip_geolocation_accuracy_m <= 0
    ):
        raise SystemExit("IP navigation fallback settings must be positive")

    last_uploaded_key = None
    last_wait_message_at = 0.0
    retry_delay = args.interval
    cached_ip_geolocation = None
    next_ip_geolocation_refresh_at = 0.0
    next_ip_fallback_upload_at = 0.0
    LOGGER.info(
        "navigation_uploader_started vehicle_id=%s file=%s cloud=%s",
        args.vehicle_id,
        args.navigation_file,
        args.cloud.rstrip("/"),
    )

    while True:
        try:
            try:
                navigation = read_navigation(args.navigation_file)
            except (FileNotFoundError, OSError, ValueError):
                navigation = None

            navigation_source = "GNSS"
            if not navigation or not navigation_is_fresh(
                navigation, ip_fallback_after_seconds
            ):
                if not ip_fallback_enabled:
                    now = time.monotonic()
                    if now - last_wait_message_at >= 10:
                        LOGGER.info(
                            "waiting_for_fresh_navigation path=%s",
                            args.navigation_file,
                        )
                        last_wait_message_at = now
                    time.sleep(args.interval)
                    continue

                monotonic_now = time.monotonic()
                if (
                    cached_ip_geolocation is None
                    or monotonic_now >= next_ip_geolocation_refresh_at
                ):
                    cached_ip_geolocation = fetch_ip_geolocation(
                        ip_geolocation_url,
                        ip_geolocation_token,
                        args.timeout,
                    )
                    next_ip_geolocation_refresh_at = (
                        monotonic_now + ip_geolocation_refresh_seconds
                    )
                    LOGGER.info(
                        "ip_geolocation_updated public_ip=%s city=%s region=%s longitude=%s latitude=%s",
                        cached_ip_geolocation.get("public_ip"),
                        cached_ip_geolocation.get("city"),
                        cached_ip_geolocation.get("region"),
                        cached_ip_geolocation.get("longitude"),
                        cached_ip_geolocation.get("latitude"),
                    )
                if monotonic_now < next_ip_fallback_upload_at:
                    time.sleep(min(args.interval, next_ip_fallback_upload_at - monotonic_now))
                    continue
                navigation = build_ip_navigation(
                    cached_ip_geolocation, ip_geolocation_accuracy_m
                )
                navigation_source = "EDGE-IP-GEO"
                next_ip_fallback_upload_at = (
                    monotonic_now + ip_fallback_upload_seconds
                )

            current_key = sample_key(navigation)
            if current_key == last_uploaded_key:
                time.sleep(args.interval)
                continue
            payload = build_payload(
                navigation, edge_session_id, edge_session_started_at_ms
            )
            upload_navigation(
                args.cloud, args.vehicle_id, token, payload, args.timeout
            )
            last_uploaded_key = current_key
            retry_delay = args.interval
            LOGGER.info(
                "navigation_uploaded vehicle_id=%s source=%s captured_at_ms=%s longitude=%s latitude=%s",
                args.vehicle_id,
                navigation_source,
                navigation.get("captured_at_ms"),
                navigation.get("longitude"),
                navigation.get("latitude"),
            )
            time.sleep(args.interval)
        except KeyboardInterrupt:
            break
        except (HTTPError, URLError, OSError, ValueError, RuntimeError) as exc:
            LOGGER.warning(
                "navigation_upload_failed retry_in=%.1fs error=%s",
                retry_delay,
                exc,
            )
            time.sleep(retry_delay)
            retry_delay = min(15.0, max(args.interval, retry_delay * 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
