"""接收 WiFi 转 UDP 数据，并转发到车云监控页面。"""

import argparse
import json
import os
import random
import re
import socket
import time
from datetime import datetime
from urllib import error, request


# 可直接修改，也可通过同名环境变量覆盖。
UDP_IP = os.getenv("WIFI_UDP_HOST", "0.0.0.0")
UDP_PORT = int(os.getenv("WIFI_UDP_PORT", "6001"))
ESP32_BRIDGE_HOST = os.getenv("ESP32_BRIDGE_HOST", "192.168.3.1")
ESP32_BRIDGE_PORT = int(os.getenv("ESP32_BRIDGE_PORT", "8080"))
TIME_SYNC_INTERVAL_SECONDS = 10.0
CLOUD_HTTP_URL = os.getenv("CLOUD_HTTP_URL", "http://127.0.0.1:8001").rstrip("/")
VEHICLE_ID = os.getenv("VEHICLE_ID", "VHC-001")
# 必须与 run_cloud.py 中的 INGEST_TOKEN 一致。
INGEST_TOKEN = os.getenv(
    "VEHICLE_INGEST_TOKEN",
    "vcl_687Nfse29GsoYlX0j8hPaK4ctMv_5g4nXBeYpy1Obu0",
)


def now_ms():
    return int(time.time() * 1000)


def bytes_to_hex(data):
    return " ".join("{:02x}".format(value) for value in data)


def make_test_text(sequence):
    return "SSID=VCL-TEST;RSSI={};CH={};TX={}Kbps;SEQ={}".format(
        random.randint(-86, -35),
        random.choice([1, 6, 11]),
        random.randint(120, 2400),
        sequence,
    )


def extract_rssi(text):
    match = re.search(r"(?:^|[;,\s])RSSI\s*[=:]\s*(-?\d+)", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def extract_sender_time_ms(text):
    """Read the optional A-computer timestamp carried through S32K unchanged."""
    match = re.search(r"(?:^|,)TS=(\d{10,16})(?:,|$)", text)
    return int(match.group(1)) if match else None


def send_test_packets(host, port, count, interval):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for sequence in range(1, count + 1):
            text = make_test_text(sequence)
            sock.sendto(text.encode("utf-8"), (host, port))
            print("Test UDP sent to {}:{}  Data: {}".format(host, port, text))
            if sequence < count:
                time.sleep(interval)
    finally:
        sock.close()


def send_time_sync(sock):
    """Give the S32K an epoch reference for board-to-server delay estimates."""
    text = "SYNC,TS={}\r\n".format(now_ms())
    try:
        sock.sendto(text.encode("ascii"), (ESP32_BRIDGE_HOST, ESP32_BRIDGE_PORT))
        print("Time sync sent to {}:{}".format(ESP32_BRIDGE_HOST, ESP32_BRIDGE_PORT))
        return True
    except OSError as exc:
        print("Time sync failed: {}".format(exc))
        return False


def forward_to_cloud(data, addr):
    text = data.decode("utf-8", errors="replace").strip()
    udp_received_at_ms = now_ms()
    payload = {
        "source_time_ms": udp_received_at_ms,
        "udp_received_at_ms": udp_received_at_ms,
        "source": "{}:{}".format(addr[0], addr[1]),
        "text": text,
        "hex": bytes_to_hex(data),
        "byte_count": len(data),
    }
    rssi = extract_rssi(text)
    if rssi is not None:
        payload["rssi"] = rssi
    sender_time_ms = extract_sender_time_ms(text)
    if sender_time_ms is not None:
        payload["sender_time_ms"] = sender_time_ms
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = "{}/api/vehicles/{}/wifi".format(CLOUD_HTTP_URL, VEHICLE_ID)
    http_request = request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer {}".format(INGEST_TOKEN),
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=5) as response:
            return 200 <= response.status < 300
    except error.HTTPError as exc:
        print("Cloud rejected WiFi data: HTTP {} {}".format(exc.code, exc.reason))
    except (error.URLError, socket.timeout) as exc:
        print("Cloud forwarding failed: {}".format(exc))
    return False


def run_receiver(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.settimeout(1.0)
    print("UDP receiver started, listening on {}:{}".format(host, port))
    print("Forwarding WiFi data to {} (vehicle {})".format(CLOUD_HTTP_URL, VEHICLE_ID))
    print("Waiting for data...")
    last_sync_at = 0.0

    try:
        while True:
            if (time.monotonic() - last_sync_at) >= TIME_SYNC_INTERVAL_SECONDS:
                send_time_sync(sock)
                last_sync_at = time.monotonic()

            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            text = data.decode("utf-8", errors="replace").strip()
            if text.startswith("SYNC,TS="):
                continue
            line = "[{}] From {}:{}  Data: {}".format(timestamp, addr[0], addr[1], text)
            print(line)
            print("HEX   : {}".format(bytes_to_hex(data)))
            with open("wifi_udp_log.txt", "a", encoding="utf-8") as log_file:
                log_file.write(line + "\n")
            print("CLOUD : {}".format("accepted" if forward_to_cloud(data, addr) else "failed"))
            print("-" * 60)
    except KeyboardInterrupt:
        print("\nWiFi receiver stopped.")
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description="WiFi UDP receiver and cloud forwarder")
    parser.add_argument("--host", default=UDP_IP, help="UDP listen address")
    parser.add_argument("--port", type=int, default=UDP_PORT, help="UDP listen port")
    parser.add_argument("--send-test", action="store_true", help="send random UDP test data and exit")
    parser.add_argument("--target-host", default="127.0.0.1", help="test packet target host")
    parser.add_argument("--count", type=int, default=1, help="number of test packets")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between test packets")
    args = parser.parse_args()
    if args.send_test:
        send_test_packets(args.target_host, args.port, max(1, args.count), max(0, args.interval))
    else:
        run_receiver(args.host, args.port)


if __name__ == "__main__":
    main()
