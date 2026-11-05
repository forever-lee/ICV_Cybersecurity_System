"""B: priority control, periodic probes, confirmed state and actual TX/RX log."""
import secrets
import threading
import time
from collections import OrderedDict, deque
from jetson_edge.wifi_control_protocol import parse_ack, encode_command


class WifiControl:
    def __init__(self, clock=None):
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.lock = threading.RLock()
        self.pending = self.probe = self.last_ack = None
        self.last_seen_ms = self.sender_seen_ms = self.last_probe_ms = 0
        self.confirmed_state = "unknown"
        self.mode = "none"
        self.last_order = self.last_confirmed_order = 0
        self.last_observation_order = 0
        self.jobs = OrderedDict()
        self.events = deque(maxlen=80)
        self.event_ids = OrderedDict()
        self.tx_count = self.rx_count = 0

    def expire(self):
        now = self.clock()
        if self.pending and now >= self.pending["expires_at_ms"]:
            self.last_ack = dict(self.pending, status="expired")
            self.pending = None
        if self.probe and now >= self.probe["expires_at_ms"]:
            self.probe = None

    def snapshot(self):
        with self.lock:
            self.expire()
            fresh = bool(self.sender_seen_ms and self.clock() - self.sender_seen_ms < 15000)
            return {"agent_status": "online" if self.last_seen_ms and self.clock() - self.last_seen_ms < 10000 else "offline",
                    "state": self.confirmed_state if fresh else "unknown", "mode": self.mode if fresh else "unknown",
                    "sender_seen_ms": self.sender_seen_ms, "pending": dict(self.pending) if self.pending else None,
                    "last_ack": dict(self.last_ack) if self.last_ack else None,
                    "status": self.pending["status"] if self.pending else (self.last_ack or {}).get("status", "idle"),
                    "tx_count": self.tx_count, "rx_count": self.rx_count, "history": list(self.events)}

    def new_command(self, action, lifetime):
        now = self.clock()
        self.last_order = max(now, self.last_order + 1)
        cmd = {"id": secrets.token_hex(8), "command": action, "order": self.last_order,
               "created_at_ms": now, "expires_at_ms": now + lifetime, "status": "queued"}
        self.jobs[cmd["id"]] = cmd
        while len(self.jobs) > 256:
            self.jobs.popitem(last=False)
        return cmd

    def queue(self, action):
        with self.lock:
            self.expire()
            if action not in {"pause", "resume", "soc", "flood", "stop"}:
                raise ValueError("unknown WiFi command")
            if self.pending:
                raise RuntimeError("已有操作等待确认")
            if self.snapshot()["agent_status"] != "online":
                raise ConnectionError("WiFi 接收端离线")
            if action in {"soc", "flood"} and (self.snapshot()["state"] != "running" or self.mode != "none"):
                raise RuntimeError("请先确认正常发送且没有其他攻击")
            self.probe = None
            self.pending = self.new_command(action, 12000)
            return self.snapshot()

    def poll(self):
        with self.lock:
            self.last_seen_ms = self.clock()
            self.expire()
            if self.pending:
                return dict(self.pending)
            if not self.probe and self.clock() - self.last_probe_ms >= 5000:
                self.probe = self.new_command("status", 5000)
                self.last_probe_ms = self.clock()
            return dict(self.probe) if self.probe else None

    def record(self, direction, text, event_id, at=None):
        if event_id in self.event_ids:
            return
        self.event_ids[event_id] = True
        while len(self.event_ids) > 2048:
            self.event_ids.popitem(last=False)
        if direction == "TX":
            self.tx_count += 1
        else:
            self.rx_count += 1
        raw = (text + "\n").encode("ascii")
        self.events.appendleft({"direction": direction, "text": text, "received_at_ms": self.clock(),
                                "edge_time_ms": at, "byte_count": len(raw), "hex": raw.hex(" "),
                                "source": "云端 → K3" if direction == "TX" else "K3 → 云端"})

    def acknowledge(self, payload):
        with self.lock:
            self.last_seen_ms = self.clock()
            if payload.get("sent"):
                sent = payload["sent"]
                job = self.jobs.get(sent.get("id"))
                attempt = int(sent.get("attempt", 0))
                if not job or not 1 <= attempt <= 5:
                    raise ValueError("invalid TX report")
                event_id = str(sent.get("event_id", "{}:{}".format(job["id"], attempt)))[:160]
                self.record("TX", encode_command(job).decode().strip(), "tx:" + event_id, sent.get("at_ms"))
                return self.snapshot()
            text = payload.get("device_ack", "")
            ack = parse_ack(text)
            if not ack:
                raise ValueError("requires a valid B device reply")
            job = self.jobs.get(ack["id"])
            if not job or job["command"] != ack["command"]:
                raise RuntimeError("unknown/mismatching reply")
            self.record("RX", text, "rx:" + str(payload.get("event_id", ack["id"])))
            observed_at = min(self.clock(), int(payload.get("received_at_ms", self.clock())))
            fresh = self.clock() - observed_at < 15000
            if (ack["status"] == "ok" and fresh and ack["last_order"] >= self.last_confirmed_order
                    and job["order"] >= self.last_observation_order and observed_at >= self.sender_seen_ms):
                self.sender_seen_ms = observed_at
                self.last_observation_order = job["order"]
                self.last_confirmed_order = ack["last_order"]
                self.confirmed_state = ack["state"]
                self.mode = ack["mode"]
            if self.pending and (ack["id"] == self.pending["id"] or
                                 (ack["status"] == "ok" and ack["last_order"] == self.pending["order"])):
                self.last_ack = dict(self.pending, status="applied" if ack["status"] == "ok" else ack["status"],
                                     acknowledged_at_ms=self.clock())
                self.pending = None
            if self.probe and self.probe["id"] == ack["id"]:
                self.probe = None
            if (self.last_ack and self.last_ack["status"] == "expired" and
                    ack["status"] == "ok" and ack["last_order"] == self.last_ack["order"]):
                self.last_ack["status"] = "applied"
            return self.snapshot()
