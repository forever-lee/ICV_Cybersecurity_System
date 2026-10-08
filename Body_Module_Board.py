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

S32K344 以太网示例：
    python Body_Module_Board.py --mode udp --host 0.0.0.0 --port 7100 \
        --board-host 192.168.4.31 --control-port 7101
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
BODY_BOARD_HOST = os.getenv("BODY_BOARD_HOST", "192.168.4.31")
BODY_CONTROL_PORT = int(os.getenv("BODY_CONTROL_PORT", "7101"))
BODY_INPUT_MODE = os.getenv("BODY_INPUT_MODE", "serial")
BODY_SERIAL_PORT = os.getenv("BODY_SERIAL_PORT", "COM4")
BODY_SERIAL_BAUDRATE = int(os.getenv("BODY_SERIAL_BAUDRATE", "115200"))

PACKET_SIZE = 16
TIMESTAMPED_PACKET_SIZE = 25
PACKET_MAGIC = bytes((0xA5, 0x5A))
COMMAND_PACKET_MAGIC = bytes((0xA5, 0x5A, 0x01, 0x02))
TIME_SYNC_PACKET_MAGIC = bytes((0xA5, 0x5A, 0x01, 0x03))
COMMAND_START = 0x01
COMMAND_STOP = 0x00
COMMAND_POLL_INTERVAL_SECONDS = 0.5
TIME_SYNC_INTERVAL_SECONDS = 5.0

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


def poll_transmission_command(
    cloud_url, vehicle_id, token, last_sequence, acknowledged_sequence=0
):
    url = "{}/api/vehicles/{}/domains/body/control".format(
        cloud_url.rstrip("/"), vehicle_id
    )
    body = json.dumps(
        {
            "last_sequence": int(last_sequence),
            "acknowledged_sequence": int(acknowledged_sequence),
        },
        separators=(",", ":"),
    ).encode("utf-8")
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
    with request.urlopen(http_request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def build_serial_command(action):
    command = COMMAND_START if action == "start" else COMMAND_STOP
    packet = bytearray(COMMAND_PACKET_MAGIC)
    packet.append(command)
    checksum = 0
    for value in packet:
        checksum ^= value
    packet.append(checksum)
    return bytes(packet)


def build_time_sync_packet(epoch_ms=None):
    """Build a board-clock synchronization packet using Jetson epoch ms."""
    if epoch_ms is None:
        epoch_ms = now_ms()
    packet = bytearray(TIME_SYNC_PACKET_MAGIC)
    packet.extend(int(epoch_ms).to_bytes(8, byteorder="little", signed=False))
    checksum = 0
    for value in packet:
        checksum ^= value
    packet.append(checksum)
    return bytes(packet)


def initial_state():
    return {
        "source": "S32K344-CAN",
    }


def decode_serial_packet(packet):
    """Decode one S32K344 body packet and verify its framing/checksum."""
    if len(packet) not in (PACKET_SIZE, TIMESTAMPED_PACKET_SIZE):
        raise ValueError(
            "expected {} or {} bytes, got {}".format(
                PACKET_SIZE, TIMESTAMPED_PACKET_SIZE, len(packet)
            )
        )
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

    board_sent_at_ms = None
    if len(packet) == TIMESTAMPED_PACKET_SIZE:
        extended_checksum = 0
        for value in packet[:24]:
            extended_checksum ^= value
        if extended_checksum != packet[24]:
            raise ValueError(
                "extended checksum mismatch: expected 0x{:02X}, got 0x{:02X}".format(
                    extended_checksum, packet[24]
                )
            )
        timestamp_value = int.from_bytes(packet[16:24], byteorder="little")
        if timestamp_value > 0:
            board_sent_at_ms = timestamp_value

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
        "transmissionEnabled": bool(packet[14] & 0x01),
        "boardSentAtMs": board_sent_at_ms,
    }


def serial_payload(decoded, serial_port, captured_at_ms=None):
    """Map the board protocol names to the existing body-domain cloud schema."""
    if captured_at_ms is None:
        captured_at_ms = now_ms()
    captured_at_ms = int(captured_at_ms)
    payload = {
        "status": "online",
        "can_status": "normal" if decoded["canProtocolValid"] else "error",
        "captured_at_ms": captured_at_ms,
        "valid": decoded["canProtocolValid"],
        "invalid_reason": "" if decoded["canProtocolValid"] else "S32K344 CAN frame validation failed",
        "device_id": "S32K344-001",
        "source": "S32K344-LPUART6",
        "interface_path": "S32K344/CAN0 -> LPUART6 -> CH340/{} -> HTTP".format(serial_port),
        "link": {
            "mcu_status": "online",
            "edge_status": "online",
            "upload_status": "online",
            "edge_received_at_ms": captured_at_ms,
        },
        "transmission_enabled": decoded["transmissionEnabled"],
    }
    for field in DOOR_FIELDS + LIGHT_FIELDS + ("CentralLockState",):
        payload[field] = decoded[field]
    return payload


def body_can_frame_metadata(decoded):
    """Describe the active body-domain CAN/PDU group for the demo step."""
    step = int(decoded.get("simulationStep") or 0)
    sequence = int(decoded.get("packetSequence") or 0) & 0x0F
    door_bits = sum(
        (1 << index) if decoded.get(field) else 0
        for index, field in enumerate(DOOR_FIELDS)
    )
    light_bits = sum(
        (1 << index) if decoded.get(field) else 0
        for index, field in enumerate(LIGHT_FIELDS)
    )
    lock_bit = 1 if decoded.get("CentralLockState") == "locked" else 0

    if 1 <= step <= 4:
        can_id = "0x351"
        frame_name = "BodyDoorStatus"
        cycle_ms = 100
        send_type = "周期+事件触发"
        values = [door_bits, 0, 0, 0, 0, 0, sequence]
    elif 5 <= step <= 9:
        can_id = "0x353"
        frame_name = "BodyLightStatus"
        cycle_ms = 50
        send_type = "周期+事件触发"
        values = [0, 0, light_bits, 0, 0, 0, sequence]
    else:
        can_id = "0x352"
        frame_name = "CentralLockStatus"
        cycle_ms = 500
        send_type = "周期"
        values = [0, lock_bit, 0, 0, 0, 0, sequence]

    values.append(sum(values) & 0xFF)
    return {
        "can_id": can_id,
        "frame_name": frame_name,
        "cycle_ms": cycle_ms,
        "send_type": send_type,
        "dlc": len(values),
        "data_hex": " ".join("{:02X}".format(value) for value in values),
    }


def ethernet_payload(decoded, board_address, udp_received_at_ms=None):
    """Map the S32K344 UDP binary packet to the existing cloud schema."""
    if udp_received_at_ms is None:
        udp_received_at_ms = now_ms()
    udp_received_at_ms = int(udp_received_at_ms)
    board_sent_at_ms = decoded.get("boardSentAtMs")
    timestamp_synchronized = isinstance(board_sent_at_ms, int) and board_sent_at_ms > 0
    payload = serial_payload(
        decoded,
        "UDP",
        board_sent_at_ms if timestamp_synchronized else udp_received_at_ms,
    )
    payload["source"] = "S32K344-100BASE-T1"
    payload["interface_path"] = (
        "S32K344/CAN0 -> GMAC/MII -> TJA1101B -> 100BASE-T1 -> "
        "{0}:{1} -> HTTP".format(board_address[0], board_address[1])
    )
    payload["link"]["edge_received_at_ms"] = udp_received_at_ms
    payload["link"]["udp_received_at_ms"] = udp_received_at_ms
    payload["link"]["board_sent_at_ms"] = board_sent_at_ms
    payload["link"]["timestamp_synchronized"] = timestamp_synchronized
    payload["link"]["latency_scope"] = (
        "board_udp_send_to_cloud_ingest"
        if timestamp_synchronized
        else "jetson_udp_receive_to_cloud_ingest"
    )
    payload["raw_frame"] = body_can_frame_metadata(decoded)
    return payload


def run_serial(args):
    try:
        import serial
    except ImportError as exc:
        raise SystemExit(
            "串口模式需要 pyserial，请执行：python -m pip install pyserial"
        ) from exc

    buffer = bytearray()
    last_command_sequence = 0
    acknowledged_sequence = 0
    next_command_poll = 0.0
    while True:
        try:
            with serial.Serial(args.serial_port, baudrate=args.baudrate, timeout=1) as connection:
                print("车身域串口已连接：{} @ {} 8N1".format(args.serial_port, args.baudrate))
                print("云端接口：{}/api/vehicles/{}/domains/body".format(args.cloud, args.vehicle_id))
                print("按 Ctrl+C 停止")
                buffer.clear()
                while True:
                    current_time = time.monotonic()
                    if current_time >= next_command_poll:
                        try:
                            command_result = poll_transmission_command(
                                args.cloud,
                                args.vehicle_id,
                                args.token,
                                last_command_sequence,
                                acknowledged_sequence,
                            )
                            acknowledged_sequence = 0
                            if command_result.get("pending"):
                                command = command_result.get("command") or {}
                                sequence = int(command.get("sequence") or 0)
                                action = str(command.get("action") or "")
                                if sequence > last_command_sequence and action in {"start", "stop"}:
                                    connection.write(build_serial_command(action))
                                    connection.flush()
                                    last_command_sequence = sequence
                                    acknowledged_sequence = sequence
                                    print(
                                        "[{}] 已向开发板下发{}命令 SEQ={}".format(
                                            time.strftime("%H:%M:%S"),
                                            "启动发送" if action == "start" else "停止发送",
                                            sequence,
                                        )
                                    )
                        except (error.URLError, OSError, ValueError) as exc:
                            print("[{}] 控制命令轮询失败：{}".format(time.strftime("%H:%M:%S"), exc))
                        next_command_poll = current_time + COMMAND_POLL_INTERVAL_SECONDS

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
    udp_socket.settimeout(0.2)
    last_command_sequence = 0
    acknowledged_sequence = 0
    next_command_poll = 0.0
    next_time_sync = 0.0
    print("车身域 UDP 数据适配器已启动：{}:{}".format(args.host, args.port))
    print("开发板控制接口：{}:{}".format(args.board_host, args.control_port))
    print("云端接口：{}/api/vehicles/{}/domains/body".format(args.cloud, args.vehicle_id))
    print("按 Ctrl+C 停止")
    while True:
        current_time = time.monotonic()
        if current_time >= next_time_sync:
            try:
                udp_socket.sendto(
                    build_time_sync_packet(),
                    (args.board_host, args.control_port),
                )
            except OSError as exc:
                print("[{}] 板端时钟校准失败：{}".format(time.strftime("%H:%M:%S"), exc))
            next_time_sync = current_time + TIME_SYNC_INTERVAL_SECONDS

        if current_time >= next_command_poll:
            try:
                command_result = poll_transmission_command(
                    args.cloud,
                    args.vehicle_id,
                    args.token,
                    last_command_sequence,
                    acknowledged_sequence,
                )
                acknowledged_sequence = 0
                if command_result.get("pending"):
                    command = command_result.get("command") or {}
                    sequence = int(command.get("sequence") or 0)
                    action = str(command.get("action") or "")
                    if sequence > last_command_sequence and action in {"start", "stop"}:
                        udp_socket.sendto(
                            build_serial_command(action),
                            (args.board_host, args.control_port),
                        )
                        last_command_sequence = sequence
                        acknowledged_sequence = sequence
                        print(
                            "[{}] 已通过 UDP 向开发板下发{}命令 SEQ={}".format(
                                time.strftime("%H:%M:%S"),
                                "启动发送" if action == "start" else "停止发送",
                                sequence,
                            )
                        )
            except (error.URLError, OSError, ValueError) as exc:
                print("[{}] 控制命令轮询失败：{}".format(time.strftime("%H:%M:%S"), exc))
            next_command_poll = current_time + COMMAND_POLL_INTERVAL_SECONDS

        try:
            data, address = udp_socket.recvfrom(8192)
            udp_received_at_ms = now_ms()
        except socket.timeout:
            continue

        try:
            if len(data) in (PACKET_SIZE, TIMESTAMPED_PACKET_SIZE) and data[:2] == PACKET_MAGIC:
                decoded = decode_serial_packet(data)
                payload = ethernet_payload(decoded, address, udp_received_at_ms)
                result = forward_to_cloud(
                    payload, args.cloud, args.vehicle_id, args.token
                )
                print(
                    "[{}] UDP SEQ={} CAN_RX={} 已上传 accepted={}".format(
                        time.strftime("%H:%M:%S"),
                        decoded["packetSequence"],
                        decoded["canRxCount"],
                        result.get("accepted"),
                    )
                )
            else:
                incoming = json.loads(data.decode("utf-8"))
                update = normalize_update(incoming)
                if not update:
                    raise ValueError("payload does not contain supported body signals")
                state.update(update)
                state.setdefault("status", "online")
                state["captured_at_ms"] = udp_received_at_ms
                result = forward_to_cloud(state, args.cloud, args.vehicle_id, args.token)
                print(
                    "[{}] {}:{} 已转发 {} 个信号 accepted={}".format(
                        time.strftime("%H:%M:%S"),
                        address[0],
                        address[1],
                        len(update),
                        result.get("accepted"),
                    )
                )
        except (UnicodeDecodeError, ValueError, error.URLError, OSError) as exc:
            print("[{}] 数据处理失败：{}".format(time.strftime("%H:%M:%S"), exc))


def parse_args():
    parser = argparse.ArgumentParser(description="V-SHIELD 车身域数据适配器")
    parser.add_argument("--mode", choices=("serial", "udp"), default=BODY_INPUT_MODE)
    parser.add_argument("--host", default=BODY_UDP_HOST)
    parser.add_argument("--port", type=int, default=BODY_UDP_PORT)
    parser.add_argument("--board-host", default=BODY_BOARD_HOST)
    parser.add_argument("--control-port", type=int, default=BODY_CONTROL_PORT)
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
