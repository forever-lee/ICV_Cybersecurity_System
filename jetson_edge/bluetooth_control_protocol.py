"""Phase-one Bluetooth control frames used by Jetson and the cloud.

The K344/nRF firmware forwards these ASCII frames unchanged. The trailing '#'
delimits a frame. CRC-16/CCITT-FALSE covers all bytes before the final '|CRC#'.
"""

import binascii
import re


MAX_FRAME_BYTES = 256
NONCE_PATTERN = re.compile(r"[0-9A-F]{16}")


def _pack(fields):
    body = "|".join(str(field) for field in fields)
    encoded = body.encode("ascii")
    if len(encoded) + 6 > MAX_FRAME_BYTES:
        raise ValueError("control frame too large")
    return "{}|{:04X}#".format(body, binascii.crc_hqx(encoded, 0xFFFF))


def _unpack(frame, kind, count):
    if isinstance(frame, bytes):
        frame = frame.decode("ascii")
    if not isinstance(frame, str) or not frame.endswith("#"):
        raise ValueError("incomplete control frame")
    if len(frame.encode("ascii")) > MAX_FRAME_BYTES:
        raise ValueError("control frame too large")
    fields = frame[:-1].split("|")
    if len(fields) != count or fields[0] != kind or fields[1] != "1":
        raise ValueError("unsupported control frame")
    if not re.fullmatch(r"[0-9A-F]{4}", fields[-1]):
        raise ValueError("invalid CRC field")
    body = "|".join(fields[:-1]).encode("ascii")
    if binascii.crc_hqx(body, 0xFFFF) != int(fields[-1], 16):
        raise ValueError("control CRC mismatch")
    return fields


def make_command(command_id, action, issued_at_ms, nonce):
    action = str(action).upper()
    nonce = str(nonce).upper()
    if not isinstance(command_id, int) or command_id < 1:
        raise ValueError("invalid command id")
    if action not in {"ON", "OFF"} or not NONCE_PATTERN.fullmatch(nonce):
        raise ValueError("invalid command")
    if not isinstance(issued_at_ms, int) or issued_at_ms < 1:
        raise ValueError("invalid issue time")
    return _pack(("BTCTL", 1, command_id, "SET_TX", action, issued_at_ms, nonce))


def parse_command(frame):
    fields = _unpack(frame, "BTCTL", 8)
    if not fields[2].isdigit() or int(fields[2]) < 1 or fields[3] != "SET_TX":
        raise ValueError("invalid command")
    if fields[4] not in {"ON", "OFF"} or not fields[5].isdigit():
        raise ValueError("invalid command")
    if not NONCE_PATTERN.fullmatch(fields[6]):
        raise ValueError("invalid command nonce")
    return {"id": int(fields[2]), "action": fields[4].lower(),
            "issued_at_ms": int(fields[5]), "nonce": fields[6]}


def make_ack(command_id, status, state, nonce):
    status = str(status).upper()
    state = str(state).upper()
    nonce = str(nonce).upper()
    if not isinstance(command_id, int) or command_id < 1:
        raise ValueError("invalid command id")
    if status not in {"APPLIED", "ERROR"} or state not in {"ON", "OFF"}:
        raise ValueError("invalid ACK")
    if not NONCE_PATTERN.fullmatch(nonce):
        raise ValueError("invalid ACK nonce")
    return _pack(("BTACK", 1, command_id, status, state, nonce))


def parse_ack(frame):
    fields = _unpack(frame, "BTACK", 7)
    if not fields[2].isdigit() or int(fields[2]) < 1:
        raise ValueError("invalid ACK id")
    if fields[3] not in {"APPLIED", "ERROR"} or fields[4] not in {"ON", "OFF"}:
        raise ValueError("invalid ACK status")
    if not NONCE_PATTERN.fullmatch(fields[5]):
        raise ValueError("invalid ACK nonce")
    return {"id": int(fields[2]), "status": fields[3].lower(),
            "state": fields[4].lower(), "nonce": fields[5]}


MAX_MESSAGE_BYTES = 64


def make_message(command_id, message, issued_at_ms, nonce):
    if not isinstance(message, str):
        raise ValueError("message must be text")
    encoded = message.encode("utf-8")
    if not encoded or len(encoded) > MAX_MESSAGE_BYTES or any(
            character in "\r\n\x00" for character in message):
        raise ValueError("message must be 1-64 UTF-8 bytes on one line")
    if not isinstance(command_id, int) or command_id < 1 or not isinstance(issued_at_ms, int) or issued_at_ms < 1:
        raise ValueError("invalid message id or time")
    nonce = str(nonce).upper()
    if not NONCE_PATTERN.fullmatch(nonce):
        raise ValueError("invalid message nonce")
    return _pack(("BTMSG", 1, command_id, encoded.hex().upper(), issued_at_ms, nonce))


def parse_message(frame):
    fields = _unpack(frame, "BTMSG", 7)
    if not fields[2].isdigit() or int(fields[2]) < 1 or not fields[4].isdigit():
        raise ValueError("invalid message id or time")
    if not NONCE_PATTERN.fullmatch(fields[5]) or not re.fullmatch(r"(?:[0-9A-F]{2}){1,64}", fields[3]):
        raise ValueError("invalid message")
    try:
        message = bytes.fromhex(fields[3]).decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("message is not UTF-8") from exc
    if any(character in "\r\n\x00" for character in message):
        raise ValueError("message contains a control character")
    return {"id": int(fields[2]), "message": message, "nonce": fields[5]}


def make_message_ack(command_id, nonce):
    nonce = str(nonce).upper()
    if not isinstance(command_id, int) or command_id < 1 or not NONCE_PATTERN.fullmatch(nonce):
        raise ValueError("invalid message ACK")
    return _pack(("BTMSGACK", 1, command_id, "RECEIVED", nonce))


def parse_message_ack(frame):
    fields = _unpack(frame, "BTMSGACK", 6)
    if not fields[2].isdigit() or int(fields[2]) < 1 or fields[3] != "RECEIVED":
        raise ValueError("invalid message ACK")
    if not NONCE_PATTERN.fullmatch(fields[4]):
        raise ValueError("invalid message ACK nonce")
    return {"id": int(fields[2]), "status": "applied", "state": "message", "nonce": fields[4]}
