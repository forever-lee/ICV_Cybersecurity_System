"""Per-vehicle control state. Only a matching sender ACK confirms execution."""
import secrets
import time
from jetson_edge.wifi_control_protocol import parse_ack


class WifiControl:
    def __init__(self, clock=None):
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.pending = None
        self.last_ack = None
        self.last_seen_ms = 0
        self.confirmed_state = "unknown"

    def expire(self):
        if self.pending and self.clock() >= self.pending["expires_at_ms"]:
            self.last_ack = dict(self.pending, status="expired", message="命令超时，未收到发送脚本确认")
            self.pending = None

    def snapshot(self):
        self.expire()
        return {
            "agent_status": "online" if self.last_seen_ms and self.clock() - self.last_seen_ms < 10000 else "offline",
            "state": self.confirmed_state,
            "pending": dict(self.pending) if self.pending else None,
            "last_ack": dict(self.last_ack) if self.last_ack else None,
            "status": self.pending["status"] if self.pending else (self.last_ack or {}).get("status", "idle"),
        }

    def queue(self, action):
        self.expire()
        if action not in {"pause", "resume"}:
            raise ValueError("command must be pause or resume")
        if self.pending:
            raise RuntimeError("已有命令等待确认，请稍后重试")
        if self.snapshot()["agent_status"] != "online":
            raise ConnectionError("WiFi 接收程序未连接后端")
        now = self.clock()
        self.pending = {"id": secrets.token_hex(16), "command": action,
                        "created_at_ms": now, "expires_at_ms": now + 15000, "status": "queued"}
        return self.snapshot()

    def poll(self):
        self.last_seen_ms = self.clock()
        self.expire()
        return dict(self.pending) if self.pending else None

    def acknowledge(self, payload):
        self.expire()
        ack = parse_ack(payload.get("device_ack", ""))
        if not ack:
            raise ValueError("requires a valid sender ACK")
        if not self.pending:
            if self.last_ack and ack["id"] == self.last_ack["id"] and self.last_ack["status"] == "applied" and ack["command"] == self.last_ack["command"]:
                return self.snapshot()  # HTTP retry after a lost response
            raise RuntimeError("command is no longer pending")
        if ack["id"] != self.pending["id"] or ack["command"] != self.pending["command"]:
            raise RuntimeError("sender ACK does not match pending command")
        self.last_seen_ms = self.clock()
        self.confirmed_state = "paused" if ack["command"] == "pause" else "running"
        self.last_ack = dict(self.pending, status="applied", device_ack=payload["device_ack"],
                             acknowledged_at_ms=self.clock(), message="发送脚本已确认执行")
        self.pending = None
        return self.snapshot()
