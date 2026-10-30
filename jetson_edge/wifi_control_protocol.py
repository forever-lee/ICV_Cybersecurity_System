"""Wire format supported by the tested S32K firmware and WiFi sender."""
import re

ACK_PATTERN = re.compile(r"ACK,ID=([A-Za-z0-9_-]{1,40}),ACTION=(PAUSE|RESUME),STATUS=OK")


def parse_ack(text):
    match = ACK_PATTERN.fullmatch(str(text).strip())
    return {"id": match[1], "command": match[2].lower()} if match else None


def encode_command(command):
    command_id = str(command["id"])
    action = str(command["command"]).upper()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", command_id) or action not in {"PAUSE", "RESUME"}:
        raise ValueError("invalid WiFi control command")
    return "CTRL,ID={},ACTION={}\n".format(command_id, action).encode("ascii")
