"""Pure helpers for the S32K344 body-domain protocol.

This module deliberately has no environment lookups, sockets, HTTP clients, or
runtime loops so it can be imported safely by unit tests and other tools.
"""

import math
import time


PACKET_SIZE = 16
TIMESTAMPED_PACKET_SIZE = 25
CONTROL_ACK_PACKET_SIZE = 32
TRANSMISSION_ACK_PACKET_SIZE = 40
PACKET_MAGIC = bytes((0xA5, 0x5A))
COMMAND_PACKET_MAGIC = bytes((0xA5, 0x5A, 0x01, 0x02))
TIME_SYNC_PACKET_MAGIC = bytes((0xA5, 0x5A, 0x01, 0x03))
BODY_EVENT_PACKET_MAGIC = bytes((0xA5, 0x5A, 0x01, 0x04))
TRANSMISSION_PACKET_MAGIC = bytes((0xA5, 0x5A, 0x01, 0x05))
COMMAND_START = 0x01
COMMAND_STOP = 0x00

DOOR_FIELDS = ("Door_FL", "Door_FR", "Door_RL", "Door_RR")
LIGHT_FIELDS = ("LowBeam", "HighBeam", "TurnLeft", "TurnRight", "Hazard")
SWITCH_FIELDS = DOOR_FIELDS + LIGHT_FIELDS

BODY_EVENT_SIGNALS = {
    "door_fl": (1, "Door_FL"),
    "door_fr": (2, "Door_FR"),
    "door_rl": (3, "Door_RL"),
    "door_rr": (4, "Door_RR"),
    "central_lock": (5, "CentralLockState"),
    "low_beam": (6, "LowBeam"),
    "high_beam": (7, "HighBeam"),
    "turn_left": (8, "TurnLeft"),
    "turn_right": (9, "TurnRight"),
    "hazard": (10, "Hazard"),
}

CONTROL_ACK_RESULTS = {
    0: "none",
    1: "applied",
    2: "duplicate",
    3: "rejected",
    4: "restored",
}

TRANSMISSION_ACK_RESULTS = {
    0: "none",
    1: "applied",
    2: "duplicate",
    3: "error",
}

TRANSMISSION_ACK_ERRORS = {
    0: "none",
    1: "sequence_action_conflict",
    2: "invalid_command",
}


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


def build_serial_command(action):
    if action not in {"start", "stop"}:
        raise ValueError("action must be start or stop")
    packet = bytearray(COMMAND_PACKET_MAGIC)
    packet.append(COMMAND_START if action == "start" else COMMAND_STOP)
    checksum = 0
    for value in packet:
        checksum ^= value
    packet.append(checksum)
    return bytes(packet)


def build_transmission_command(sequence, action):
    if action not in {"start", "stop"}:
        raise ValueError("action must be start or stop")
    sequence = int(sequence)
    if not 0 < sequence <= 0xFFFFFFFF:
        raise ValueError("sequence must be between 1 and 4294967295")
    packet = bytearray(TRANSMISSION_PACKET_MAGIC)
    packet.extend(sequence.to_bytes(4, byteorder="little", signed=False))
    packet.append(COMMAND_START if action == "start" else COMMAND_STOP)
    packet.extend((0, 0))
    checksum = 0
    for value in packet:
        checksum ^= value
    packet.append(checksum)
    return bytes(packet)


def build_time_sync_packet(epoch_ms=None):
    if epoch_ms is None:
        epoch_ms = now_ms()
    packet = bytearray(TIME_SYNC_PACKET_MAGIC)
    packet.extend(int(epoch_ms).to_bytes(8, byteorder="little", signed=False))
    checksum = 0
    for value in packet:
        checksum ^= value
    packet.append(checksum)
    return bytes(packet)


def build_body_event_command(sequence, action, target, hold_seconds=5):
    if action not in BODY_EVENT_SIGNALS:
        raise ValueError("unsupported body event action")
    sequence = int(sequence)
    hold_seconds = int(hold_seconds)
    if not 0 < sequence <= 0xFFFFFFFF:
        raise ValueError("sequence must be between 1 and 4294967295")
    if not 1 <= hold_seconds <= 30:
        raise ValueError("hold_seconds must be between 1 and 30")
    signal_id, _ = BODY_EVENT_SIGNALS[action]
    packet = bytearray(BODY_EVENT_PACKET_MAGIC)
    packet.extend(sequence.to_bytes(4, byteorder="little", signed=False))
    packet.append(signal_id)
    packet.append(1 if normalize_switch(target, action) else 0)
    packet.append(hold_seconds)
    checksum = 0
    for value in packet:
        checksum ^= value
    packet.append(checksum)
    return bytes(packet)


def initial_state():
    return {"source": "S32K344-CAN"}


def decode_serial_packet(packet):
    supported_sizes = (
        PACKET_SIZE, TIMESTAMPED_PACKET_SIZE, CONTROL_ACK_PACKET_SIZE,
        TRANSMISSION_ACK_PACKET_SIZE,
    )
    if len(packet) not in supported_sizes:
        raise ValueError(
            "expected {}, {}, {} or {} bytes, got {}".format(
                PACKET_SIZE, TIMESTAMPED_PACKET_SIZE, CONTROL_ACK_PACKET_SIZE,
                TRANSMISSION_ACK_PACKET_SIZE, len(packet)
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
    if len(packet) in (
        TIMESTAMPED_PACKET_SIZE, CONTROL_ACK_PACKET_SIZE,
        TRANSMISSION_ACK_PACKET_SIZE,
    ):
        checksum_index = {
            TIMESTAMPED_PACKET_SIZE: 24,
            CONTROL_ACK_PACKET_SIZE: 31,
            TRANSMISSION_ACK_PACKET_SIZE: 39,
        }[len(packet)]
        extended_checksum = 0
        for value in packet[:checksum_index]:
            extended_checksum ^= value
        if extended_checksum != packet[checksum_index]:
            raise ValueError(
                "extended checksum mismatch: expected 0x{:02X}, got 0x{:02X}".format(
                    extended_checksum, packet[checksum_index]
                )
            )
        timestamp_value = int.from_bytes(packet[16:24], byteorder="little")
        if timestamp_value > 0:
            board_sent_at_ms = timestamp_value

    body_flags = packet[4]
    light_flags = packet[5]
    left_turn = bool(light_flags & 0x04)
    right_turn = bool(light_flags & 0x08)
    decoded = {
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
    if len(packet) in (CONTROL_ACK_PACKET_SIZE, TRANSMISSION_ACK_PACKET_SIZE):
        ack_sequence = int.from_bytes(packet[24:28], byteorder="little")
        ack_signal_id = packet[28]
        ack_target = bool(packet[29])
        ack_result_code = packet[30]
        action = next(
            (key for key, value in BODY_EVENT_SIGNALS.items() if value[0] == ack_signal_id),
            None,
        )
        decoded["controlAck"] = {
            "sequence": ack_sequence,
            "signal_id": ack_signal_id,
            "action": action,
            "target": ack_target,
            "result_code": ack_result_code,
            "result": CONTROL_ACK_RESULTS.get(ack_result_code, "unknown"),
        }
    if len(packet) == TRANSMISSION_ACK_PACKET_SIZE:
        tx_sequence = int.from_bytes(packet[32:36], byteorder="little")
        tx_action_code = packet[36]
        tx_result_code = packet[37]
        tx_error_code = packet[38]
        decoded["transmissionAck"] = {
            "sequence": tx_sequence,
            "action": "start" if tx_action_code == COMMAND_START else "stop",
            "action_code": tx_action_code,
            "result_code": tx_result_code,
            "result": TRANSMISSION_ACK_RESULTS.get(tx_result_code, "unknown"),
            "error_code": tx_error_code,
            "error": TRANSMISSION_ACK_ERRORS.get(tx_error_code, "unknown"),
        }
    return decoded


def serial_payload(decoded, serial_port, captured_at_ms=None):
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

    ack = decoded.get("controlAck") or {}
    ack_action = ack.get("action") if ack.get("result") in {"applied", "duplicate"} else None
    if ack_action in {"door_fl", "door_fr", "door_rl", "door_rr"}:
        can_id, frame_name, cycle_ms, send_type = "0x351", "BodyDoorStatus", 0, "Web事件触发/CAN回读"
        values = [door_bits, 0, 0, 0, 0, 0, sequence]
    elif ack_action == "central_lock":
        can_id, frame_name, cycle_ms, send_type = "0x352", "CentralLockStatus", 0, "Web事件触发/CAN回读"
        values = [0, lock_bit, 0, 0, 0, 0, sequence]
    elif ack_action in {"low_beam", "high_beam", "turn_left", "turn_right", "hazard"}:
        can_id, frame_name, cycle_ms, send_type = "0x353", "BodyLightStatus", 0, "Web事件触发/CAN回读"
        values = [0, 0, light_bits, 0, 0, 0, sequence]
    elif 1 <= step <= 4:
        can_id, frame_name, cycle_ms, send_type = "0x351", "BodyDoorStatus", 100, "周期+事件触发"
        values = [door_bits, 0, 0, 0, 0, 0, sequence]
    elif 5 <= step <= 9:
        can_id, frame_name, cycle_ms, send_type = "0x353", "BodyLightStatus", 50, "周期+事件触发"
        values = [0, 0, light_bits, 0, 0, 0, sequence]
    else:
        can_id, frame_name, cycle_ms, send_type = "0x352", "CentralLockStatus", 500, "周期"
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
    payload["source"] = "S32K344-Ethernet-UDP"
    payload["interface_path"] = (
        "S32K344/CAN0 -> GMAC/MII -> TJA1101B -> 100BASE-T1 -> "
        "{0}:{1} -> Jetson -> HTTP".format(board_address[0], board_address[1])
    )
    payload["link"].update({
        "transport": "Ethernet/IPv4/UDP",
        "board_ip": board_address[0],
        "edge_received_at_ms": udp_received_at_ms,
        "udp_received_at_ms": udp_received_at_ms,
        "board_sent_at_ms": board_sent_at_ms,
        "timestamp_synchronized": timestamp_synchronized,
        "latency_scope": (
            "board_udp_send_to_cloud_ingest"
            if timestamp_synchronized
            else "jetson_udp_receive_to_cloud_ingest"
        ),
    })
    payload["raw_frame"] = body_can_frame_metadata(decoded)
    if decoded.get("controlAck"):
        payload["control_ack"] = dict(decoded["controlAck"])
    if decoded.get("transmissionAck"):
        payload["transmission_ack"] = dict(decoded["transmissionAck"])
    return payload
