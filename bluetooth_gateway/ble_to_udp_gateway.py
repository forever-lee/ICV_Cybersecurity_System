#!/usr/bin/env python3
"""BLE adapter gateway: receive board notifications, timestamp them, and send UDP."""

import asyncio
import json
import os
import socket
import time
from urllib import error, request

from bleak import BleakClient


def required_environment(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit("缺少环境变量 {}".format(name))
    return value


BLE_ADDRESS = required_environment("BLE_ADDRESS")
NOTIFY_UUID = os.getenv("BLE_NOTIFY_UUID", "00001002-0000-1000-8000-00805f9b34fb").strip()
WRITE_UUID = os.getenv("BLE_WRITE_UUID", "00001001-0000-1000-8000-00805f9b34fb").strip()
UDP_HOST = required_environment("BLUETOOTH_UDP_TARGET_HOST")
UDP_PORT = int(os.getenv("BLUETOOTH_UDP_PORT", "5000"))
CLOUD_HTTP_URL = os.getenv("CLOUD_HTTP_URL", "").strip().rstrip("/")
VEHICLE_ID = os.getenv("VEHICLE_ID", "VHC-001").strip()
INGEST_TOKEN = os.getenv("VEHICLE_INGEST_TOKEN", "").strip()
CONTROL_POLL_SECONDS = max(0.2, float(os.getenv("CLOUD_CONTROL_POLL_SECONDS", "1")))

udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
receive_buffer = b""
last_ack_text = ""


def next_frame():
    global receive_buffer
    positions = [position for position in (receive_buffer.find(b"\r\n"), receive_buffer.find(b"#")) if position >= 0]
    if not positions:
        return None
    position = min(positions)
    separator_size = 2 if receive_buffer[position:position + 2] == b"\r\n" else 1
    frame = receive_buffer[:position]
    receive_buffer = receive_buffer[position + separator_size:]
    return frame


def on_notify(_sender, data):
    global receive_buffer, last_ack_text
    receive_buffer += bytes(data)
    while True:
        frame = next_frame()
        if frame is None:
            return
        text = frame.decode("utf-8", errors="ignore").replace("\x00", "").strip()
        if not text:
            continue
        if text.startswith("ACK:") or text.startswith("STATUS:"):
            last_ack_text = text
            print("[BLE ACK] {}".format(text), flush=True)
            continue
        payload = {
            "text": text,
            "source_time_ms": int(time.time() * 1000),
            "source": "BLE-GATEWAY@{}".format(BLE_ADDRESS),
            "byte_count": len(text.encode("utf-8")),
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        udp_socket.sendto(encoded, (UDP_HOST, UDP_PORT))
        print("[BLE -> UDP] {}".format(text), flush=True)


async def write_characters(client, payload):
    for character in payload:
        await client.write_gatt_char(WRITE_UUID, character.encode("utf-8"), response=True)
        await asyncio.sleep(0.03)


async def execute_command(client, command, lock):
    global last_ack_text
    settings = {"off": ("FFFFFF", "ACK:BLE_TX_OFF"), "on": ("OOOOOO", "ACK:BLE_TX_ON")}
    if command not in settings:
        return False
    payload, expected_ack = settings[command]
    async with lock:
        for attempt in range(1, 11):
            last_ack_text = ""
            try:
                await write_characters(client, payload)
            except Exception as exc:
                print("[BLE 控制写入失败] 第 {} 次：{}".format(attempt, exc), flush=True)
                continue
            for _ in range(60):
                if expected_ack in last_ack_text:
                    return True
                await asyncio.sleep(0.01)
    return False


def control_request(path, payload=None):
    url = "{}/api/vehicles/{}/bluetooth/control/{}".format(CLOUD_HTTP_URL, VEHICLE_ID, path)
    headers = {"Authorization": "Bearer {}".format(INGEST_TOKEN), "Accept": "application/json"}
    body = None
    method = "GET"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST"
    http_request = request.Request(url, data=body, headers=headers, method=method)
    with request.urlopen(http_request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


async def cloud_control_loop(client, lock):
    last_result = None
    while True:
        try:
            response = await asyncio.to_thread(control_request, "next")
            pending = response.get("command")
            if pending:
                command_id = int(pending["id"])
                command = str(pending["command"]).strip().lower()
                if not last_result or last_result["id"] != command_id:
                    success = await execute_command(client, command, lock)
                    last_result = {
                        "id": command_id,
                        "status": "applied" if success else "error",
                        "message": "BLE ACK received" if success else "BLE ACK timeout",
                    }
                await asyncio.to_thread(control_request, "ack", dict(last_result, serial_port="BLE:" + BLE_ADDRESS))
        except asyncio.CancelledError:
            raise
        except (error.HTTPError, error.URLError, OSError, ValueError) as exc:
            print("[云端控制暂不可用] {}".format(exc), flush=True)
        await asyncio.sleep(CONTROL_POLL_SECONDS)


async def connected_session():
    print("正在连接蓝牙设备 {}".format(BLE_ADDRESS), flush=True)
    async with BleakClient(BLE_ADDRESS, timeout=30.0, pair=False) as client:
        await client.start_notify(NOTIFY_UUID, on_notify)
        print("蓝牙网关已连接，UDP 目标 {}:{}".format(UDP_HOST, UDP_PORT), flush=True)
        control_task = None
        if CLOUD_HTTP_URL and INGEST_TOKEN:
            control_task = asyncio.create_task(cloud_control_loop(client, asyncio.Lock()))
            print("云端蓝牙开关控制已启用", flush=True)
        else:
            print("未配置云端地址或令牌，仅转发蓝牙数据", flush=True)
        try:
            while client.is_connected:
                await asyncio.sleep(1)
        finally:
            if control_task:
                control_task.cancel()
                await asyncio.gather(control_task, return_exceptions=True)


async def main():
    retry_seconds = 1
    while True:
        try:
            await connected_session()
            retry_seconds = 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[蓝牙连接断开] {}，{} 秒后重连".format(exc, retry_seconds), flush=True)
            await asyncio.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, 30)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
