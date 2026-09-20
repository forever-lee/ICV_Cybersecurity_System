#!/usr/bin/env python3
"""车身域数据适配器。

本程序只负责接收、校验、合并和转发真实设备数据，不生成任何模拟状态，
也不依赖网页。当前支持直接读取 S32K344 经 LPUART6/CH340 发出的 16 字节
车身状态包，也保留原有 UDP JSON 接口。

串口示例：
    python Body_Module_Board.py --mode serial --serial-port COM4 --baudrate 115200

UDP 示例：
    {"status":"online","can_status":"normal"}
    {"Door_FL":false,"Door_FR":false,"Door_RL":true,"Door_RR":false}
    {"CentralLockState":"unlocked","TurnLeft":true}
"""

import argparse
import json
import math
import os
import socket
import time
from urllib import error, request


CLOUD_HTTP_URL = os.getenv("CLOUD_HTTP_URL", "http://127.0.0.1:8001").rstrip("/")
VEHICLE_ID = os.getenv("VEHICLE_ID", "VHC-001")
INGEST_TOKEN = os.getenv(
   "VEHICLE_INGEST_TOKEN", ""
)
BODY_UDP_HOST = os.getenv("BODY_UDP_HOST", "0.0.0.0")
BODY_UDP_PORT = int(os.getenv("BODY_UDP_PORT", "7100"))
BODY_INPUT_MODE = os.getenv("BODY_INPUT_MODE", "serial")
BODY_SERIAL_PORT = os.getenv("BODY_SERIAL_PORT", "COM4")
BODY_SERIAL_BAUDRATE = int(os.getenv("BODY_SERIAL_BAUDRATE", "115200"))

PACKET_SIZE = 16
PACKET_MAGIC = bytes((0xA5, 0x5A))

DOOR_FIELDS = ("Door_FL", "Door_FR", "Door_RL", "Door_RR")
LIGHT_FIELDS = ("LowBeam", "HighBeam", "TurnLeft", "TurnRight", "Hazard")
SWITCH_FIELDS = DOOR_FIELDS + LIGHT_FIELDS


def now_ms():
    return int(time.time() * 1000)


def normalize_switch(value, field):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "on", "open", "opened", "active"}:
        return True
    if normalized in {"0", "false", "off", "close", "closed", "inactive"}:
        return False
    raise ValueError("{} must be an ON/OFF or open/closed value".format(field))


def normalize_lock(value):
    if isinstance(value, bool):
        return "locked" if value else "unlocked"
    if isinstance(value, (int, float)) and value in (0, 1):
        return "locked" if value == 1 else "unlocked"
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "locked", "lock"}:
        return "locked"
    if normalized in {"0", "false", "unlocked", "unlock"}:
        return "unlocked"
    raise ValueError("CentralLockState must be locked or unlocked")


def normalize_can_status(value):
    if isinstance(value, bool):
        return "normal" if value else "error"
    if isinstance(value, (int, float)) and value in (0, 1):
        return "normal" if value == 1 else "error"
    normalized = str(value).strip().lower()
    if normalized in {"normal", "ok", "online", "active"}:
        return "normal"
    if normalized in {"error", "abnormal", "fault", "offline", "bus-off"}:
        return "error"
    raise ValueError("can_status must be normal or error")


def finite_number(value, field, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("{} must be numeric".format(field))
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError("{} must be between {} and {}".format(field, minimum, maximum))
    return number


def normalize_update(payload):
    if not isinstance(payload, dict):
        raise ValueError("body payload must be a JSON object")
    if isinstance(payload.get("body"), dict):
        payload = payload["body"]

    normalized = {}
    if "status" in payload:
        status = str(payload["status"]).strip().lower()
        if status not in {"active", "online", "offline"}:
            raise ValueError("status must be online or offline")
        normalized["status"] = "online" if status in {"active", "online"} else "offline"
    if "cpu_load_percent" in payload:
        normalized["cpu_load_percent"] = round(
            finite_number(payload["cpu_load_percent"], "cpu_load_percent", 0, 100), 1
        )
    if "supply_voltage_v" in payload:
        normalized["supply_voltage_v"] = round(
            finite_number(payload["supply_voltage_v"], "supply_voltage_v", 0, 36), 2
        )
    if "can_status" in payload:
        normalized["can_status"] = normalize_can_status(payload["can_status"])
    for field in SWITCH_FIELDS:
        if field in payload:
            normalized[field] = normalize_switch(payload[field], field)
    if "CentralLockState" in payload:
        normalized["CentralLockState"] = normalize_lock(payload["CentralLockState"])
    if "source" in payload:
        normalized["source"] = str(payload["source"])[:80]
    for field in (
        "device_id", "captured_at_ms", "valid", "invalid_reason",
        "can_id", "dlc", "data_hex", "frame_name", "cycle_ms",
        "send_type", "interface_path", "raw_frame", "signals", "link",
    ):
        if field in payload:
            normalized[field] = payload[field]
    return normalized


def forward_to_cloud(payload, cloud_url, vehicle_id, token):
    url = "{}/api/vehicles/{}/domains/body".format(cloud_url.rstrip("/"), vehicle_id)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "V-SHIELD-Body-Adapter/1.0",
        },
        method="POST",
    )
    with request.urlopen(http_request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def initial_state():
    return {
        "source": "S32K344-CAN",
    }


def decode_serial_packet(packet):
    """Decode one S32K344 body packet and verify its framing/checksum."""
    if len(packet) != PACKET_SIZE:
        raise ValueError("expected {} bytes, got {}".format(PACKET_SIZE, len(packet)))
    if packet[:2] != PACKET_MAGIC:
        raise ValueError("invalid packet magic")
    if packet[2] != 0x01:
        raise ValueError("unsupported protocol version: {}".format(packet[2]))
    if packet[3] != 0x01:
        raise ValueError("unsupported message type: {}".format(packet[3]))

    checksum = 0
    for value in packet[:15]:
        checksum ^= value
    if checksum != packet[15]:
        raise ValueError(
            "checksum mismatch: expected 0x{:02X}, got 0x{:02X}".format(
                checksum, packet[15]
            )
        )

    body_flags = packet[4]
    light_flags = packet[5]
    left_turn = bool(light_flags & 0x04)
    right_turn = bool(light_flags & 0x08)
    return {
        "Door_FL": bool(body_flags & 0x01),
        "Door_FR": bool(body_flags & 0x02),
        "Door_RL": bool(body_flags & 0x04),
        "Door_RR": bool(body_flags & 0x08),
        "trunkOpen": bool(body_flags & 0x10),
        "CentralLockState": "locked" if body_flags & 0x20 else "unlocked",
        "ignitionOn": bool(body_flags & 0x40),
        "LowBeam": bool(light_flags & 0x01),
        "HighBeam": bool(light_flags & 0x02),
        "TurnLeft": left_turn,
        "TurnRight": right_turn,
        "Hazard": left_turn and right_turn,
        "brakeLightOn": bool(light_flags & 0x10),
        "reverseLightOn": bool(light_flags & 0x20),
        "canAliveCounter": packet[6],
        "simulationStep": packet[7],
        "canRxCount": int.from_bytes(packet[8:12], byteorder="little"),
        "canProtocolValid": bool(packet[12]),
        "packetSequence": packet[13],
    }


def serial_payload(decoded, serial_port):
    """Map the board protocol names to the existing body-domain cloud schema."""
    payload = {
        "status": "online",
        "can_status": "normal" if decoded["canProtocolValid"] else "error",
        "captured_at_ms": now_ms(),
        "valid": decoded["canProtocolValid"],
        "invalid_reason": "" if decoded["canProtocolValid"] else "S32K344 CAN frame validation failed",
        "device_id": "S32K344-001",
        "source": "S32K344-LPUART6",
        "interface_path": "S32K344/CAN0 -> LPUART6 -> CH340/{} -> HTTP".format(serial_port),
        "link": {
            "mcu_status": "online",
            "edge_status": "online",
            "upload_status": "online",
            "edge_received_at_ms": now_ms(),
        },
    }
    for field in DOOR_FIELDS + LIGHT_FIELDS + ("CentralLockState",):
        payload[field] = decoded[field]
    return payload


def run_serial(args):
    try:
        import serial
    except ImportError as exc:
        raise SystemExit(
            "串口模式需要 pyserial，请执行：python -m pip install pyserial"
        ) from exc

    buffer = bytearray()
    while True:
        try:
            with serial.Serial(args.serial_port, baudrate=args.baudrate, timeout=1) as connection:
                print("车身域串口已连接：{} @ {} 8N1".format(args.serial_port, args.baudrate))
                print("云端接口：{}/api/vehicles/{}/domains/body".format(args.cloud, args.vehicle_id))
                print("按 Ctrl+C 停止")
                buffer.clear()
                while True:
                    chunk = connection.read(64)
                    if not chunk:
                        continue
                    buffer.extend(chunk)
                    while True:
                        magic_index = buffer.find(PACKET_MAGIC)
                        if magic_index < 0:
                            if buffer[-1:] != PACKET_MAGIC[:1]:
                                buffer.clear()
                            break
                        if magic_index > 0:
                            del buffer[:magic_index]
                        if len(buffer) < PACKET_SIZE:
                            break

                        packet = bytes(buffer[:PACKET_SIZE])
                        try:
                            decoded = decode_serial_packet(packet)
                        except ValueError:
                            del buffer[0]
                            continue
                        del buffer[:PACKET_SIZE]

                        payload = serial_payload(decoded, args.serial_port)
                        try:
                            result = forward_to_cloud(
                                payload, args.cloud, args.vehicle_id, args.token
                            )
                            print(
                                "[{}] SEQ={} CAN_RX={} 已上传 accepted={}".format(
                                    time.strftime("%H:%M:%S"),
                                    decoded["packetSequence"],
                                    decoded["canRxCount"],
                                    result.get("accepted"),
                                )
                            )
                        except (error.URLError, OSError, ValueError) as exc:
                            print("[{}] 云端上传失败：{}".format(time.strftime("%H:%M:%S"), exc))
        except (serial.SerialException, OSError) as exc:
            print(
                "[{}] 串口不可用：{}；{} 秒后重连".format(
                    time.strftime("%H:%M:%S"), exc, args.reconnect_delay
                )
            )
            time.sleep(args.reconnect_delay)


def run_udp(args):
    state = initial_state()
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_socket.bind((args.host, args.port))
    print("车身域 UDP 数据适配器已启动：{}:{}".format(args.host, args.port))
    print("云端接口：{}/api/vehicles/{}/domains/body".format(args.cloud, args.vehicle_id))
    print("按 Ctrl+C 停止")
    while True:
        data, address = udp_socket.recvfrom(8192)
        try:
            incoming = json.loads(data.decode("utf-8"))
            update = normalize_update(incoming)
            if not update:
                raise ValueError("payload does not contain supported body signals")
            state.update(update)
            state.setdefault("status", "online")
            state["captured_at_ms"] = now_ms()
            result = forward_to_cloud(state, args.cloud, args.vehicle_id, args.token)
            print(
                "[{}] {}:{} 已转发 {} 个信号 accepted={}".format(
                    time.strftime("%H:%M:%S"), address[0], address[1], len(update), result.get("accepted")
                )
            )
        except (UnicodeDecodeError, ValueError, error.URLError, OSError) as exc:
            print("[{}] 数据处理失败：{}".format(time.strftime("%H:%M:%S"), exc))


def parse_args():
    parser = argparse.ArgumentParser(description="V-SHIELD 车身域数据适配器")
    parser.add_argument("--mode", choices=("serial", "udp"), default=BODY_INPUT_MODE)
    parser.add_argument("--host", default=BODY_UDP_HOST)
    parser.add_argument("--port", type=int, default=BODY_UDP_PORT)
    parser.add_argument("--serial-port", default=BODY_SERIAL_PORT)
    parser.add_argument("--baudrate", type=int, default=BODY_SERIAL_BAUDRATE)
    parser.add_argument("--reconnect-delay", type=float, default=2.0)
    parser.add_argument("--cloud", default=CLOUD_HTTP_URL)
    parser.add_argument("--vehicle-id", default=VEHICLE_ID)
    parser.add_argument("--token", default=INGEST_TOKEN)
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        if args.mode == "serial":
            run_serial(args)
        else:
            run_udp(args)
    except KeyboardInterrupt:
        print("\n车身域数据适配器已停止")


if __name__ == "__main__":
    main()
