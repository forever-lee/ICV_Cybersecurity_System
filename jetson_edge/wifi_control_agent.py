"""Independent cloud polling so RESUME works when telemetry has stopped."""
import json
import queue
import threading
import time
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
        self.last_id = None
        self.last_sent = 0
        self.pending_ack = None
        self.last_error = None

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
            self.acks.put_nowait({"device_ack": text})
        except queue.Full:
            print("[CONTROL] ACK queue full; waiting for cloud recovery", flush=True)
        return True

    def step(self):
        if self.pending_ack is None:
            try:
                self.pending_ack = self.acks.get_nowait()
            except queue.Empty:
                pass
        if self.pending_ack is not None:
            try:
                self.http("/ack", self.pending_ack)
                print("[CONTROL] Cloud confirmed {}".format(self.pending_ack["device_ack"]), flush=True)
            except error.HTTPError as exc:
                if exc.code not in (400, 409):
                    raise
                print("[CONTROL] Stale/rejected ACK: HTTP {}".format(exc.code), flush=True)
            self.pending_ack = None
        result = self.http("/next")
        command = result.get("command")
        if not command:
            self.last_id = None
            return
        # Use server-relative time, since edge and server clocks can differ.
        if command["expires_at_ms"] <= result["server_time_ms"]:
            return
        now = time.monotonic()
        if command["id"] != self.last_id or now - self.last_sent >= 1:
            self.sock.sendto(encode_command(command), self.board)
            print("[CONTROL] {} ID={} -> {}:{}".format(
                command["command"], command["id"], *self.board), flush=True)
            self.last_id, self.last_sent = command["id"], now

    def run(self):
        while not self.stopped.is_set():
            try:
                self.step()
                if self.last_error:
                    print("[CONTROL] Cloud connection recovered", flush=True)
                self.last_error = None
            except Exception as exc:
                message = str(exc)
                if message != self.last_error:
                    print("[CONTROL] Poll/ACK failed: {}".format(message), flush=True)
                self.last_error = message
            self.stopped.wait(0.5)
