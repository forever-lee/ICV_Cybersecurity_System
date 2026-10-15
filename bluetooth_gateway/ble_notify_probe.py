#!/usr/bin/env python3
"""Check whether bytes sent into the module UART reach this laptop via BLE Notify.

This is a diagnostic listener only. It does not send business packets, change
the module mode, or connect to the cloud. Stop the normal BLE sender first.
"""

import asyncio
import os

from bleak import BleakClient


BLE_ADDRESS = os.getenv("BLE_ADDRESS", "").strip()
NOTIFY_UUID = os.getenv(
    "BLE_NOTIFY_UUID", "00001002-0000-1000-8000-00805f9b34fb"
).strip()


def show_notification(_characteristic, data):
    raw = bytes(data)
    print(
        "[BLE Notify] HEX={}  文本={}".format(
            raw.hex(" "), raw.decode("utf-8", errors="replace")
        ),
        flush=True,
    )


async def main():
    if not BLE_ADDRESS:
        raise SystemExit("请先设置 BLE_ADDRESS 为当前模块的蓝牙地址")
    print("正在连接 {}；只监听 Notify，不发送数据……".format(BLE_ADDRESS), flush=True)
    async with BleakClient(BLE_ADDRESS, timeout=30.0, pair=False) as client:
        characteristic = client.services.get_characteristic(NOTIFY_UUID)
        if characteristic is None:
            raise SystemExit("找不到 Notify 特征 {}".format(NOTIFY_UUID))
        if "notify" not in characteristic.properties:
            raise SystemExit("特征 {} 不支持 Notify".format(NOTIFY_UUID))
        await client.start_notify(characteristic, show_notification)
        print("已订阅 Notify。现在向模块 UART 发送 TEST#；收到后按 Ctrl+C 退出。", flush=True)
        try:
            while client.is_connected:
                await asyncio.sleep(0.5)
            print("BLE 连接已断开，测试结束。", flush=True)
        finally:
            if client.is_connected:
                await client.stop_notify(characteristic)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("监听已停止。")
