#!/usr/bin/env python3
"""Send S32K344 test frames through nRF52840 BLE and obey cloud on/off control.

Actual lab path: laptop BLE write -> nRF UART -> S32K344 Ethernet UDP -> Ubuntu.
The S32K344 firmware, not this laptop, sends the UDP datagram to Ubuntu.
"""

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
WRITE_UUID = os.getenv(
    "BLE_WRITE_UUID", "00001001-0000-1000-8000-00805f9b34fb"
).strip()
CLOUD_HTTP_URL = required_environment("CLOUD_HTTP_URL").rstrip("/")
VEHICLE_ID = os.getenv("VEHICLE_ID", "VHC-001").strip()
INGEST_TOKEN = required_environment("VEHICLE_INGEST_TOKEN")
SEND_INTERVAL_SECONDS = max(0.1, float(os.getenv("BLUETOOTH_SEND_INTERVAL_SECONDS", "2")))
ABNORMAL_EVERY = max(0, int(os.getenv("BLUETOOTH_ABNORMAL_EVERY", "5")))
CONTROL_POLL_SECONDS = max(0.2, float(os.getenv("CLOUD_CONTROL_POLL_SECONDS", "0.5")))


def normal_frame(counter):
    return "S32K344_HELLO_{:02d}".format(((counter - 1) % 99) + 1)


def abnormal_frame(counter):
    return "S32K344_ABNORMAL_{:04d};TYPE=BLE_ATTACK;LEVEL=HIGH".format(counter)


def cloud_request(path, payload, timeout=5):
    url = "{}/api/vehicles/{}/bluetooth/{}".format(CLOUD_HTTP_URL, VEHICLE_ID, path)
    headers = {
        "Authorization": "Bearer {}".format(INGEST_TOKEN),
        "Accept": "application/json",
    }
    body = None
    method = "GET"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST"
    http_request = request.Request(url, data=body, headers=headers, method=method)
    with request.urlopen(http_request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


async def register_send_time(state, text):
    sent_at_ms = int(time.time() * 1000)
    try:
        await asyncio.to_thread(
            cloud_request, "timing", {"text": text, "sent_at_ms": sent_at_ms}, 2
        )
        state["timing_error"] = None
    except (error.HTTPError, error.URLError, OSError, ValueError) as exc:
        message = "延迟登记暂不可用：{}".format(exc)
        if message != state.get("timing_error"):
            print(message, flush=True)
            state["timing_error"] = message


async def write_frame(client, characteristic, text):
    """The board firmware expects one '#' terminated frame in small BLE writes."""
    payload = (text + "#").encode("utf-8")
    chunk_size = getattr(characteristic, "max_write_without_response_size", 20)
    if not isinstance(chunk_size, int) or chunk_size <= 0:
        chunk_size = 20
    chunk_size = min(chunk_size, 20)
    use_response = "write" in set(getattr(characteristic, "properties", []) or [])
    for offset in range(0, len(payload), chunk_size):
        await client.write_gatt_char(
            characteristic, payload[offset:offset + chunk_size], response=use_response
        )
        if offset + chunk_size < len(payload):
            await asyncio.sleep(0.08)


async def cloud_control_loop(state):
    last_result = None
    last_error = None
    print("云端发送开关已接入：{}".format(CLOUD_HTTP_URL), flush=True)
    while True:
        try:
            response = await asyncio.to_thread(cloud_request, "control/next", None)
            command = response.get("command")
            if command:
                command_id = int(command["id"])
                name = str(command["command"]).strip().lower()
                if last_result is None or last_result["id"] != command_id:
                    if name in {"on", "off"}:
                        state["enabled"] = name == "on"
                        status, message = "applied", "Laptop BLE sender {}".format(
                            "resumed" if state["enabled"] else "paused"
                        )
                    else:
                        status, message = "error", "Unsupported command"
                    last_result = {
                        "id": command_id, "status": status, "message": message,
                    }
                    print("云端命令 #{}：{} -> {}".format(command_id, name, status), flush=True)
                await asyncio.to_thread(
                    cloud_request,
                    "control/ack",
                    dict(last_result, serial_port="BLE:" + BLE_ADDRESS),
                )
            last_error = None
        except asyncio.CancelledError:
            raise
        except (error.HTTPError, error.URLError, OSError, ValueError) as exc:
            message = "云端控制暂不可用：{}".format(exc)
            if message != last_error:
                print(message, flush=True)
                last_error = message
        await asyncio.sleep(CONTROL_POLL_SECONDS)


async def send_session(state):
    print("正在连接 nRF52840：{}".format(BLE_ADDRESS), flush=True)
    async with BleakClient(BLE_ADDRESS, timeout=30.0, pair=False) as client:
        characteristic = client.services.get_characteristic(WRITE_UUID)
        if characteristic is None:
            raise RuntimeError("找不到 BLE 写特征：{}".format(WRITE_UUID))
        print("BLE 已连接，开始向 S32K344 发送测试报文", flush=True)
        while client.is_connected:
            if not state["enabled"]:
                await asyncio.sleep(0.2)
                continue
            counter = state["normal_counter"]
            text = normal_frame(counter)
            await register_send_time(state, text)
            await write_frame(client, characteristic, text)
            state["normal_counter"] += 1
            print("BLE -> nRF -> S32K344：{}".format(text), flush=True)

            if state["enabled"] and ABNORMAL_EVERY and counter % ABNORMAL_EVERY == 0:
                await asyncio.sleep(0.25)
                abnormal = abnormal_frame(state["abnormal_counter"])
                await register_send_time(state, abnormal)
                await write_frame(client, characteristic, abnormal)
                state["abnormal_counter"] += 1
                print("BLE -> nRF -> S32K344：{}".format(abnormal), flush=True)
            await asyncio.sleep(SEND_INTERVAL_SECONDS)


async def run():
    state = {
        "enabled": True,
        "normal_counter": 1,
        "abnormal_counter": 1,
        "timing_error": None,
    }
    control_task = asyncio.create_task(cloud_control_loop(state))
    try:
        while True:
            try:
                await send_session(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("BLE 链路异常：{}".format(exc), flush=True)
            print("3 秒后重新连接……", flush=True)
            await asyncio.sleep(3)
    finally:
        control_task.cancel()
        await asyncio.gather(control_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("发送已停止。")
