"""Independent cloud polling so RESUME works when telemetry has stopped."""
import json
import queue
import threading
import time
import uuid
from collections import deque
from urllib import request, error

try:  # supports both python jetson_edge/WiFi_Module.py and package imports
    from .wifi_control_protocol import parse_ack, encode_command
except ImportError:
    from wifi_control_protocol import parse_ack, encode_command


class WifiControlAgent(threading.Thread):
    def __init__(self, sock, board, cloud_url, vehicle_id, token, http=None):
        super().__init__(daemon=True)
        self.sock, self.board = sock, board
        self.url = "{}/api/vehicles/{}/wifi/control".format(cloud_url.rstrip("/"), vehicle_id)
        self.token = token
        self.http = http or self._http
        self.stopped = threading.Event()
        self.acks = queue.Queue(maxsize=64)
        self.receipts = queue.Queue(maxsize=64)
        self.last_id = None
        self.last_sent = 0
        self.last_error = None
        self.attempts = 0
        self.instance_id = uuid.uuid4().hex

    def _http(self, suffix, payload=None):
        req = request.Request(self.url + suffix,
                              data=json.dumps(payload).encode() if payload is not None else None,
                              headers={"Authorization": "Bearer " + self.token,
                                       "Content-Type": "application/json"},
                              method="POST" if payload is not None else "GET")
        with request.urlopen(req, timeout=2) as response:
            return json.loads(response.read())

    def receive_ack(self, data, addr):
        text = data.decode("ascii", errors="replace").strip()
        if not text.startswith("ACK,"):
            return False
        if addr[0] != self.board[0] or not parse_ack(text):
            print("[CONTROL] Rejected invalid ACK/source: {}".format(addr), flush=True)
            return True
        try:
            self.acks.put_nowait((time.monotonic(), {"device_ack": text, "event_id": uuid.uuid4().hex}))
        except queue.Full:
            print("[CONTROL] ACK queue full; waiting for cloud recovery", flush=True)
        return True

    def upload_reports(self):
        retries = deque(maxlen=self.acks.maxsize)
        while not self.stopped.is_set():
            try:
                report = self.acks.get_nowait()
                attempts = 0
            except queue.Empty:
                if retries:
                    report, attempts = retries.popleft()
                else:
                    try:
                        report = self.receipts.get_nowait()
                        attempts = 0
                    except queue.Empty:
                        self.stopped.wait(0.1)
                        continue
            received_at, payload = report
            age_ms = int((time.monotonic() - received_at) * 1000)
            if age_ms >= 15000:
                continue
            is_reply = "device_ack" in payload
            command_id = parse_ack(payload["device_ack"])["id"] if is_reply else payload["sent"]["id"]
            try:
                self.http("/ack", dict(payload, age_ms=age_ms))
                if is_reply:
                    print("[CONTROL] Cloud accepted ACK ID={}".format(command_id), flush=True)
            except error.HTTPError as exc:
                print("[CONTROL] Report ID={} HTTP {}".format(command_id, exc.code), flush=True)
                if is_reply and exc.code >= 500 and attempts < 2:
                    retries.append((report, attempts + 1))
                self.stopped.wait(0.3)
            except Exception as exc:
                print("[CONTROL] Report ID={} failed: {}".format(command_id, exc), flush=True)
                if is_reply and attempts < 2:
                    retries.append((report, attempts + 1))
                self.stopped.wait(0.3)

    def step(self):
        result = self.http("/next")
        command = result.get("command")
        if not command:
            return
        # Use server-relative time, since edge and server clocks can differ.
        if command["expires_at_ms"] <= result["server_time_ms"]:
            return
        now = time.monotonic()
        if command["id"] != self.last_id:
            self.attempts = 0
        if self.attempts >= 5:
            return
        if command["id"] != self.last_id or now - self.last_sent >= 1:
            self.sock.sendto(encode_command(command), self.board)
            self.attempts += 1
            try:
                self.receipts.put_nowait((now, {"sent": {"id": command["id"], "attempt": self.attempts,
                    "event_id": self.instance_id + ":" + command["id"] + ":" + str(self.attempts),
                    "at_ms": int(time.time() * 1000)}}))
            except queue.Full:
                print("[CONTROL] TX statistics queue full, dropped ID={}".format(command["id"]), flush=True)
            print("[CONTROL] {} ID={} -> {}:{}".format(
                command["command"], command["id"], *self.board), flush=True)
            self.last_id, self.last_sent = command["id"], now

    def run(self):
        uploader = threading.Thread(target=self.upload_reports, daemon=True)
        uploader.start()
        while not self.stopped.is_set():
            try:
                self.step()
                if self.last_error:
                    print("[CONTROL] Cloud connection recovered", flush=True)
                self.last_error = None
            except Exception as exc:
                message = str(exc)
                if message != self.last_error:
                    print("[CONTROL] Command poll failed: {}".format(message), flush=True)
                self.last_error = message
            self.stopped.wait(0.5)
        uploader.join(timeout=2.5)
