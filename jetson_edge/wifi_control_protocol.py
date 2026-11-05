"""B wire protocol. Commands fit the board's 127-byte line limit."""
import re

ACTIONS = {"PAUSE", "RESUME", "STATUS", "SOC", "FLOOD", "STOP"}
ID = r"[A-Za-z0-9_-]{1,40}"
COMMAND = re.compile(r"CTRL,ID=(" + ID + r"),ACTION=([A-Z]+),SEQ=(\d{1,16}),EXP=(\d{1,16})")
ACK = re.compile(r"ACK,ID=(" + ID + r"),ACTION=([A-Z]+),STATUS=(OK|STALE|EXPIRED),STATE=(RUNNING|PAUSED),MODE=(NONE|SOC|FLOOD),LAST=(\d{1,16})")


def parse_command(text):
    m = COMMAND.fullmatch(str(text).strip())
    if not m or m[2] not in ACTIONS:
        return None
    return {"id": m[1], "command": m[2].lower(), "order": int(m[3]), "expires_at_ms": int(m[4])}


def parse_ack(text):
    m = ACK.fullmatch(str(text).strip())
    if not m or m[2] not in ACTIONS:
        return None
    return {"id": m[1], "command": m[2].lower(), "status": m[3].lower(),
            "state": m[4].lower(), "mode": m[5].lower(), "last_order": int(m[6])}


def encode_command(command):
    text = "CTRL,ID={},ACTION={},SEQ={},EXP={}".format(
        command["id"], command["command"].upper(), command["order"], command["expires_at_ms"])
    if not parse_command(text) or len(text) + 1 >= 128:
        raise ValueError("invalid/oversized B command")
    return (text + "\n").encode("ascii")
