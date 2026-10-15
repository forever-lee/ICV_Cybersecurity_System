#!/usr/bin/env python3
"""Send BLE telemetry and accept hardware-delivered control over BLE Notify."""

import asyncio
import base64
import binascii
import os
import random
import re
import sys
import struct
import threading
import time
from collections import OrderedDict

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
NOTIFY_UUID = os.getenv(
    "BLE_NOTIFY_UUID", "00001002-0000-1000-8000-00805f9b34fb"
).strip()
SEND_INTERVAL_SECONDS = max(0.1, float(os.getenv("BLUETOOTH_SEND_INTERVAL_SECONDS", "2")))
DATA_ACK_TIMEOUT_SECONDS = max(0.5, float(os.getenv("BLUETOOTH_DATA_ACK_TIMEOUT_SECONDS", "2.5")))
DATA_ACK_ATTEMPTS = max(1, int(os.getenv("BLUETOOTH_DATA_ACK_ATTEMPTS", "3")))
ASYNC_ACK_POLL_SECONDS = 0.1
REQUIRE_DATA_ACK = os.getenv("BLUETOOTH_REQUIRE_DATA_ACK", "0").strip().lower() in {
    "1", "true", "yes", "on",
}
ABNORMAL_EVERY = max(0, int(os.getenv("BLUETOOTH_ABNORMAL_EVERY", "5")))
ATTACK_MODE = os.getenv("BLUETOOTH_ATTACK_MODE", "replace").strip().lower()
if ATTACK_MODE not in {"replace", "extra"}:
    raise SystemExit("BLUETOOTH_ATTACK_MODE 只能是 replace 或 extra")
INITIAL_ENABLED = os.getenv("BLUETOOTH_INITIAL_ENABLED", "1").strip().lower() not in {
    "0", "false", "off",
}
MAX_CONTROL_FRAME_BYTES = 256
NONCE_PATTERN = re.compile(r"[0-9A-F]{16}")
MAX_SYNC_ID = (1 << 32) - 1


def monotonic_us():
    return time.monotonic_ns() // 1000


def parse_sync_request(frame):
    if not isinstance(frame, bytes) or not frame.endswith(b"#") or len(frame) > MAX_CONTROL_FRAME_BYTES:
        raise ValueError("同步请求不完整")
    if frame.startswith(b"BTSYNC|2|Q|"):
        try:
            packed = base64.b64decode(frame[len(b"BTSYNC|2|Q|"):-1],
                                      altchars=b"-_", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("同步请求编码错误") from exc
        if len(packed) != 14:
            raise ValueError("同步请求长度错误")
        payload, crc = packed[:-2], struct.unpack("!H", packed[-2:])[0]
        if binascii.crc_hqx(b"BTSYNC|2|Q|" + payload, 0xFFFF) != crc:
            raise ValueError("同步请求 CRC 错误")
        sync_id, t1_ms = struct.unpack("!IQ", payload)
        if not 0 < sync_id <= MAX_SYNC_ID or not 0 < t1_ms <= 9999999999999:
            raise ValueError("同步请求参数错误")
        return sync_id, t1_ms
    try:
        fields = frame[:-1].decode("ascii").split("|")
    except UnicodeDecodeError as exc:
        raise ValueError("同步请求不是 ASCII") from exc
    if (len(fields) != 6 or fields[:2] != ["BTSYNC", "1"] or fields[3] != "REQ" or
            not fields[2].isdigit() or not fields[4].isdigit() or
            not re.fullmatch(r"[0-9A-F]{4}", fields[5])):
        raise ValueError("同步请求格式错误")
    sync_id, t1_ms = int(fields[2]), int(fields[4])
    if not 0 < sync_id <= MAX_SYNC_ID or not 0 < t1_ms <= 9999999999999:
        raise ValueError("同步请求参数错误")
    body = b"|".join(frame[:-1].split(b"|")[:-1])
    if binascii.crc_hqx(body, 0xFFFF) != int(fields[5], 16):
        raise ValueError("同步请求 CRC 错误")
    return sync_id, t1_ms


def make_sync_response(sync_id, t1_ms, u2_us, u3_us, version=1):
    if not 0 < sync_id <= MAX_SYNC_ID or not 0 < u2_us <= u3_us:
        raise ValueError("同步响应参数错误")
    if version == 2:
        duration_us = u3_us - u2_us
        if u2_us > (1 << 64) - 1 or duration_us > MAX_SYNC_ID:
            raise ValueError("同步响应参数错误")
        prefix = b"BTSYNC|2|R|"
        payload = struct.pack("!IQI", sync_id, u2_us, duration_us)
        crc = binascii.crc_hqx(prefix + payload, 0xFFFF)
        return (prefix + base64.urlsafe_b64encode(payload + struct.pack("!H", crc)) + b"#").decode("ascii")
    if version != 1:
        raise ValueError("不支持的同步协议版本")
    body = "BTSYNC|1|{}|RESP|{}|{}|{}".format(sync_id, t1_ms, u2_us, u3_us)
    return "{}|{:04X}#".format(body, binascii.crc_hqx(body.encode("ascii"), 0xFFFF))


def timed_telemetry(text, sequence, tick_us, sync_id):
    if not 0 <= sequence <= MAX_SYNC_ID or tick_us <= 0 or not 0 <= sync_id <= MAX_SYNC_ID:
        raise ValueError("遥测时间字段错误")
    body = "{};SEQ={};TICK_US={};SYNC_ID={}".format(text, sequence, tick_us, sync_id)
    return "{};CRC={:04X}".format(body, binascii.crc_hqx(body.encode("ascii"), 0xFFFF))


def parse_data_ack(frame):
    if not isinstance(frame, bytes) or not frame.endswith(b"#") or len(frame) > MAX_CONTROL_FRAME_BYTES:
        raise ValueError("遥测 ACK 不完整")
    try:
        fields = frame[:-1].decode("ascii").split("|")
    except UnicodeDecodeError as exc:
        raise ValueError("遥测 ACK 不是 ASCII") from exc
    if (len(fields) != 6 or fields[:2] != ["BTDATA", "1"] or fields[3] != "ACK" or
            not fields[2].isdigit() or not 0 <= int(fields[2]) <= MAX_SYNC_ID or
            not re.fullmatch(r"[0-9A-F]{4}", fields[4]) or
            not re.fullmatch(r"[0-9A-F]{4}", fields[5])):
        raise ValueError("遥测 ACK 格式错误")
    body = b"|".join(frame[:-1].split(b"|")[:-1])
    if binascii.crc_hqx(body, 0xFFFF) != int(fields[5], 16):
        raise ValueError("遥测 ACK 校验错误")
    return int(fields[2]), fields[4]


def parse_control_frame(frame):
    """Validate a complete BTCTL frame before changing the send flag."""
    if not isinstance(frame, bytes) or not frame.endswith(b"#"):
        raise ValueError("控制帧不完整")
    if len(frame) > MAX_CONTROL_FRAME_BYTES:
        raise ValueError("控制帧过长")
    try:
        fields = frame[:-1].decode("ascii").split("|")
    except UnicodeDecodeError as exc:
        raise ValueError("控制帧不是 ASCII") from exc
    if (len(fields) != 8 or fields[0:2] != ["BTCTL", "1"] or
            fields[3] != "SET_TX" or fields[4] not in {"ON", "OFF"} or
            not fields[2].isdigit() or int(fields[2]) < 1 or
            not fields[5].isdigit() or not NONCE_PATTERN.fullmatch(fields[6]) or
            not re.fullmatch(r"[0-9A-F]{4}", fields[7])):
        raise ValueError("控制帧格式错误")
    body = b"|".join(frame[:-1].split(b"|")[:-1])
    if binascii.crc_hqx(body, 0xFFFF) != int(fields[7], 16):
        raise ValueError("控制帧 CRC 错误")
    return {"id": int(fields[2]), "action": fields[4].lower(),
            "nonce": fields[6]}


def make_ack_frame(command_id, state, nonce):
    if state not in {"on", "off"} or not NONCE_PATTERN.fullmatch(nonce):
        raise ValueError("ACK 参数错误")
    body = "BTACK|1|{}|APPLIED|{}|{}".format(command_id, state.upper(), nonce)
    return "{}|{:04X}#".format(body, binascii.crc_hqx(body.encode("ascii"), 0xFFFF))


class NotifyFramer:
    """Reassemble '#' terminated frames across BLE notification boundaries."""

    def __init__(self):
        self.buffer = bytearray()
        self.overflow = False

    def feed(self, data):
        frames = []
        for byte in data:
            if byte == ord("#"):
                if self.buffer and not self.overflow:
                    frames.append(bytes(self.buffer) + b"#")
                self.buffer.clear()
                self.overflow = False
            elif not self.overflow:
                if len(self.buffer) < MAX_CONTROL_FRAME_BYTES - 1:
                    self.buffer.append(byte)
                else:
                    self.buffer.clear()
                    self.overflow = True
        return frames


def normal_frame(counter):
    return "S32K344_HELLO_{:02d}".format(((counter - 1) % 99) + 1)


def abnormal_frame(counter):
    return "S32K344_ABNORMAL_{:04d};TYPE=BLE_ATTACK;LEVEL=HIGH".format(counter)


def next_telemetry_text(state, counter):
    """In replace mode, substitute one of slots 5/6 in each ten-frame block."""
    if ATTACK_MODE == "replace" and ABNORMAL_EVERY:
        block = (counter - 1) // 10
        if state.get("attack_block") != block:
            state["attack_block"] = block
            state["attack_slot"] = random.choice((5, 6))
        if (counter - 1) % 10 + 1 == state["attack_slot"]:
            abnormal_counter = state["abnormal_counter"]
            state["abnormal_counter"] += 1
            return abnormal_frame(abnormal_counter)
    return normal_frame(counter)


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


async def apply_command(state, command, command_key=None):
    async with state["lock"]:
        if command_key is not None and command_key in state["applied_commands"]:
            return state["applied_commands"][command_key]
        state["enabled"] = command == "on"
        state["revision"] += 1
        revision = state["revision"]
        if command_key is not None:
            state["applied_commands"][command_key] = revision
            if len(state["applied_commands"]) > 128:
                state["applied_commands"].popitem(last=False)
        if command == "off":
            pending_ack = state.get("pending_data_ack")
            if pending_ack is not None and not pending_ack[2].done():
                pending_ack[2].set_result("paused")
    print("[{}] 蓝牙业务发送已{}".format(
        "硬件命令" if command_key is not None else "CMD",
        "打开" if command == "on" else "关闭"), flush=True)
    return revision


def read_console(loop, commands):
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        loop.call_soon_threadsafe(commands.put_nowait, line.strip().lower())


async def console_loop(state, commands):
    while True:
        command = await commands.get()
        if command in {"on", "off"}:
            await apply_command(state, command)
            print("[CMD] 仅改变本机发送；未直接访问云端", flush=True)
        elif command == "status":
            print("当前：{}，BLE {}".format(
                "发送中" if state["enabled"] else "已暂停",
                "已连接" if state["ble_connected"] else "未连接"), flush=True)
        elif command:
            print("请输入 on、off 或 status；Ctrl+C 退出", flush=True)


async def notify_control_loop(state, client, write_characteristic, notifications):
    framer = NotifyFramer()
    frame_started_us = None
    while client.is_connected:
        notification = await notifications.get()
        data, received_us = notification if isinstance(notification, tuple) else (notification, monotonic_us())
        if not framer.buffer:
            frame_started_us = received_us
        for frame in framer.feed(data):
            u2_us = frame_started_us or received_us
            frame_started_us = received_us
            if frame.startswith(b"BTDATA|"):
                try:
                    sequence, frame_crc = parse_data_ack(frame)
                except ValueError as exc:
                    print("[遥测 ACK 已拒绝] {}".format(exc), flush=True)
                    continue
                pending = state.get("pending_data_ack")
                if (pending is not None and pending[0] == sequence and
                        pending[1] == frame_crc and not pending[2].done()):
                    pending[2].set_result(None)
                recent = state.get("recent_data_acks")
                if recent is not None:
                    sent = recent.pop((sequence, frame_crc), None)
                    if sent is not None:
                        print("[K3 异步确认] {}".format(sent["text"]), flush=True)
                continue
            if frame.startswith(b"BTSYNC|"):
                try:
                    sync_id, t1_ms = parse_sync_request(frame)
                except ValueError as exc:
                    print("[蓝牙对时请求已拒绝] {}".format(exc), flush=True)
                    continue
                async with state["write_lock"]:
                    u3_us = monotonic_us()
                    response = make_sync_response(
                        sync_id, t1_ms, u2_us, u3_us,
                        version=2 if frame.startswith(b"BTSYNC|2|Q|") else 1)
                    await write_frame(client, write_characteristic, response[:-1])
                    state["sync_id"] = sync_id
                print("[蓝牙对时响应已发出] sync_id={}".format(sync_id), flush=True)
                continue
            if not frame.startswith(b"BTCTL|"):
                continue
            try:
                command = parse_control_frame(frame)
            except ValueError as exc:
                print("[硬件命令已拒绝] {}".format(exc), flush=True)
                continue
            key = (command["id"], command["nonce"])
            async with state["lock"]:
                cached_ack = state["acknowledged_commands"].get(key)
            if cached_ack is None:
                await apply_command(state, command["action"], key)
                cached_ack = make_ack_frame(command["id"], command["action"], command["nonce"])
                async with state["lock"]:
                    state["acknowledged_commands"][key] = cached_ack
                    if len(state["acknowledged_commands"]) > 128:
                        state["acknowledged_commands"].popitem(last=False)
            async with state["write_lock"]:
                await write_frame(client, write_characteristic, cached_ack[:-1])
            print("[硬件 ACK 已经 BLE 发出] id={} 状态={}".format(
                command["id"], command["action"]), flush=True)


def pending_telemetry(state, text, normal_counter=None):
    state["sender_sequence"] = (state["sender_sequence"] + 1) & MAX_SYNC_ID
    sequence = state["sender_sequence"]
    frame = timed_telemetry(text, sequence, monotonic_us(), state["sync_id"])
    pending = {"text": frame, "sequence": sequence, "crc": frame.rsplit(";CRC=", 1)[1],
               "normal_counter": normal_counter}
    state["pending_frame"] = pending
    return pending


async def send_pending_telemetry(state, client, write_characteristic):
    pending = state["pending_frame"]
    if not REQUIRE_DATA_ACK:
        if not state["enabled"]:
            return None
        recent = state.setdefault("recent_data_acks", OrderedDict())
        key = (pending["sequence"], pending["crc"])
        recent[key] = {"text": pending["text"], "attempts": 1,
                       "last_sent": time.monotonic()}
        if len(recent) > 128:
            expired_key, _expired = recent.popitem(last=False)
            print("[K3 未确认] SEQ={} 超出待确认队列容量".format(
                expired_key[0]), flush=True)
        try:
            async with state["write_lock"]:
                await write_frame(client, write_characteristic, pending["text"])
        except Exception:
            recent.pop(key, None)
            raise
        if key in recent:
            recent[key]["last_sent"] = time.monotonic()
        state["pending_frame"] = None
        print("[BLE 已发送] {}（K3 ACK 后台确认及限次重发，不阻塞下一帧）".format(
            pending["text"]), flush=True)
        return pending
    for attempt in range(1, DATA_ACK_ATTEMPTS + 1):
        if not state["enabled"]:
            return None
        future = asyncio.get_running_loop().create_future()
        state["pending_data_ack"] = (pending["sequence"], pending["crc"], future)
        try:
            async with state["write_lock"]:
                await write_frame(client, write_characteristic, pending["text"])
            result = await asyncio.wait_for(future, DATA_ACK_TIMEOUT_SECONDS)
            if result == "paused":
                return None
            print("[K3 已确认] {}".format(pending["text"]), flush=True)
            state["pending_frame"] = None
            return pending
        except asyncio.TimeoutError:
            print("[K3 未确认] SEQ={}，重试 {}/{}".format(
                pending["sequence"], attempt, DATA_ACK_ATTEMPTS), flush=True)
        finally:
            state["pending_data_ack"] = None
    raise RuntimeError("SEQ={} 未收到 K3 确认；保留原报文，重连后继续重试".format(
        pending["sequence"]))


async def retry_unconfirmed_telemetry(state, client, write_characteristic):
    """Retry missing K3 receipts without stopping new telemetry or reconnecting BLE."""
    while client.is_connected:
        await asyncio.sleep(ASYNC_ACK_POLL_SECONDS)
        if not state["enabled"]:
            continue
        recent = state["recent_data_acks"]
        now = time.monotonic()
        for key, sent in list(recent.items()):
            if key not in recent or now - sent["last_sent"] < DATA_ACK_TIMEOUT_SECONDS:
                continue
            if sent["attempts"] >= DATA_ACK_ATTEMPTS:
                recent.pop(key, None)
                print("[K3 未确认] SEQ={} 已发送 {} 次；继续发送后续报文".format(
                    key[0], sent["attempts"]), flush=True)
                continue
            async with state["write_lock"]:
                if key not in recent or not state["enabled"]:
                    continue
                sent["attempts"] += 1
                sent["last_sent"] = time.monotonic()
                await write_frame(client, write_characteristic, sent["text"])
            if key in recent:
                recent[key]["last_sent"] = time.monotonic()
                print("[K3 后台重发] SEQ={} 第 {}/{} 次".format(
                    key[0], sent["attempts"], DATA_ACK_ATTEMPTS), flush=True)


async def send_session(state):
    print("正在连接 nRF52840：{}".format(BLE_ADDRESS), flush=True)
    async with BleakClient(BLE_ADDRESS, timeout=30.0, pair=False) as client:
        write_characteristic = client.services.get_characteristic(WRITE_UUID)
        notify_characteristic = client.services.get_characteristic(NOTIFY_UUID)
        if write_characteristic is None:
            raise RuntimeError("找不到 BLE 写特征：{}".format(WRITE_UUID))
        if notify_characteristic is None or "notify" not in notify_characteristic.properties:
            raise RuntimeError("找不到可订阅的 BLE Notify 特征：{}".format(NOTIFY_UUID))
        notifications = asyncio.Queue(maxsize=128)
        loop = asyncio.get_running_loop()

        def on_notify(_characteristic, data):
            received_us = monotonic_us()
            def enqueue():
                try:
                    notifications.put_nowait((bytes(data), received_us))
                except asyncio.QueueFull:
                    print("[硬件命令] Notify 队列已满；等待 Jetson 重试", flush=True)

            loop.call_soon_threadsafe(enqueue)

        await client.start_notify(notify_characteristic, on_notify)
        state["ble_connected"] = True
        state.setdefault("recent_data_acks", OrderedDict())
        print("BLE 已连接并订阅 Notify；CMD 输入 on/off 可控制本机发送", flush=True)
        control_task = asyncio.create_task(
            notify_control_loop(state, client, write_characteristic, notifications)
        )
        retry_task = None if REQUIRE_DATA_ACK else asyncio.create_task(
            retry_unconfirmed_telemetry(state, client, write_characteristic)
        )
        try:
            while client.is_connected:
                if control_task.done():
                    await control_task
                if retry_task is not None and retry_task.done():
                    await retry_task
                async with state["lock"]:
                    enabled = state["enabled"]
                    if enabled and state["pending_frame"] is None:
                        counter = state["normal_counter"]
                        state["normal_counter"] += 1
                    else:
                        counter = None
                if not enabled:
                    await asyncio.sleep(0.2)
                    continue
                if counter is not None:
                    text = next_telemetry_text(state, counter)
                    if text.startswith("S32K344_ABNORMAL_"):
                        print("[模拟攻击替换] {} -> {}".format(
                            normal_frame(counter), text), flush=True)
                    pending_telemetry(state, text, counter)
                completed = await send_pending_telemetry(state, client, write_characteristic)
                if completed is None:
                    continue
                counter = completed["normal_counter"]
                if (ATTACK_MODE == "extra" and counter is not None and
                        ABNORMAL_EVERY and counter % ABNORMAL_EVERY == 0):
                    await asyncio.sleep(0.25)
                    async with state["lock"]:
                        abnormal_counter = state["abnormal_counter"] if state["enabled"] else None
                        if abnormal_counter is not None:
                            state["abnormal_counter"] += 1
                    if abnormal_counter is not None:
                        pending_telemetry(state, abnormal_frame(abnormal_counter))
                        await send_pending_telemetry(state, client, write_characteristic)
                await asyncio.sleep(SEND_INTERVAL_SECONDS)
        finally:
            state["ble_connected"] = False
            control_task.cancel()
            tasks = [control_task]
            if retry_task is not None:
                retry_task.cancel()
                tasks.append(retry_task)
            await asyncio.gather(*tasks, return_exceptions=True)
            if client.is_connected:
                await client.stop_notify(notify_characteristic)


async def send_loop(state):
    while True:
        try:
            await send_session(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["ble_connected"] = False
            print("BLE 链路异常：{}".format(exc), flush=True)
        print("3 秒后重新连接……", flush=True)
        await asyncio.sleep(3)


async def run():
    state = {
        "normal_counter": 1, "abnormal_counter": 1,
        "sender_sequence": 0, "sync_id": 0,
        "enabled": INITIAL_ENABLED, "ble_connected": False,
        "revision": 0, "applied_commands": OrderedDict(),
        "acknowledged_commands": OrderedDict(), "lock": asyncio.Lock(),
        "write_lock": asyncio.Lock(),
        "pending_frame": None, "pending_data_ack": None,
        "recent_data_acks": OrderedDict(),
    }
    commands = asyncio.Queue()
    threading.Thread(
        target=read_console, args=(asyncio.get_running_loop(), commands), daemon=True
    ).start()
    print("发送端：CMD 输入 on / off / status；网页命令仅经 Jetson/K344/BLE Notify 接收。", flush=True)
    await asyncio.gather(send_loop(state), console_loop(state, commands))


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("发送已停止。")
