#!/usr/bin/env python3
"""Send S32K344 test frames to the vehicle board through nRF52840 BLE.

This computer has no direct cloud connection. The complete data path is:
laptop BLE write -> nRF UART -> S32K344 Ethernet UDP -> Jetson -> cloud.

Web control and end-to-end latency are intentionally reserved for a later
hardware-closed-loop implementation.
"""

import asyncio
import os

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
SEND_INTERVAL_SECONDS = max(0.1, float(os.getenv("BLUETOOTH_SEND_INTERVAL_SECONDS", "2")))
ABNORMAL_EVERY = max(0, int(os.getenv("BLUETOOTH_ABNORMAL_EVERY", "5")))


def normal_frame(counter):
    return "S32K344_HELLO_{:02d}".format(((counter - 1) % 99) + 1)


def abnormal_frame(counter):
    return "S32K344_ABNORMAL_{:04d};TYPE=BLE_ATTACK;LEVEL=HIGH".format(counter)


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


async def send_session(state):
    print("正在连接 nRF52840：{}".format(BLE_ADDRESS), flush=True)
    async with BleakClient(BLE_ADDRESS, timeout=30.0, pair=False) as client:
        characteristic = client.services.get_characteristic(WRITE_UUID)
        if characteristic is None:
            raise RuntimeError("找不到 BLE 写特征：{}".format(WRITE_UUID))
        print("BLE 已连接，开始向 S32K344 发送测试报文", flush=True)
        while client.is_connected:
            counter = state["normal_counter"]
            text = normal_frame(counter)
            await write_frame(client, characteristic, text)
            state["normal_counter"] += 1
            print("BLE -> nRF -> S32K344：{}".format(text), flush=True)

            if ABNORMAL_EVERY and counter % ABNORMAL_EVERY == 0:
                await asyncio.sleep(0.25)
                abnormal = abnormal_frame(state["abnormal_counter"])
                await write_frame(client, characteristic, abnormal)
                state["abnormal_counter"] += 1
                print("BLE -> nRF -> S32K344：{}".format(abnormal), flush=True)
            await asyncio.sleep(SEND_INTERVAL_SECONDS)


async def run():
    state = {
        "normal_counter": 1,
        "abnormal_counter": 1,
    }
    print("发送端仅使用 BLE，不连接云端；网页启停和端到端延迟功能暂未接入。", flush=True)
    while True:
        try:
            await send_session(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("BLE 链路异常：{}".format(exc), flush=True)
        print("3 秒后重新连接……", flush=True)
        await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("发送已停止。")
