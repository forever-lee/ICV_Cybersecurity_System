"""Small, bounded CAN frame summaries for BLF uploads and local Vector capture."""

from collections import Counter, deque
from statistics import median
from threading import Event, Lock, Thread

TARGET_IDS = (0x290, 0x1D1)
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


class FrameSummary:
    def __init__(self):
        self.count = 0
        self.last = None
        self.first = None
        self.previous = None
        self.intervals = deque(maxlen=1000)
        self.dlcs = Counter()
        self.can_types = Counter()
        self.directions = Counter()
        self.payloads = Counter()

    def add(self, msg):
        timestamp = float(msg.timestamp)
        if self.first is None:
            self.first = timestamp
        if self.previous is not None and timestamp >= self.previous:
            self.intervals.append(round((timestamp - self.previous) * 1000, 3))
        self.previous = timestamp
        self.count += 1
        self.last = timestamp
        self.dlcs[int(msg.dlc)] += 1
        self.can_types["CAN FD" if msg.is_fd else "Classical CAN"] += 1
        self.directions["Rx" if msg.is_rx else "Tx"] += 1
        data = bytes(msg.data).hex(" ").upper()
        self.payloads[data] += 1
        if len(self.payloads) > 32:
            self.payloads = Counter(dict(self.payloads.most_common(16)))
        self.latest_data = data

    def json(self, can_id):
        return {
            "id": "0x{:X}".format(can_id),
            "count": self.count,
            "cycle_ms": round(median(self.intervals), 3) if self.intervals else None,
            "dlc": self.dlcs.most_common(1)[0][0] if self.dlcs else None,
            "can_type": self.can_types.most_common(1)[0][0] if self.can_types else None,
            "direction": self.directions.most_common(1)[0][0] if self.directions else None,
            "data": getattr(self, "latest_data", ""),
            "unique_data_count": len(self.payloads),
            "last_timestamp": self.last,
        }


def summarize_blf(stream):
    import can

    summaries = {can_id: FrameSummary() for can_id in TARGET_IDS}
    total = 0
    with can.BLFReader(stream) as reader:
        for msg in reader:
            total += 1
            if not msg.is_error_frame and msg.arbitration_id in summaries:
                summaries[msg.arbitration_id].add(msg)
    return {"total_frames": total, "frames": [summaries[can_id].json(can_id) for can_id in TARGET_IDS]}


class VectorMonitor:
    def __init__(self):
        self.lock = Lock()
        self.stop_event = Event()
        self.thread = None
        self.channel = 0
        self.error = ""
        self.summaries = {can_id: FrameSummary() for can_id in TARGET_IDS}

    def start(self, channel):
        with self.lock:
            if self.thread and self.thread.is_alive():
                pass
            else:
                self.channel = channel
                self.error = ""
                self.summaries = {can_id: FrameSummary() for can_id in TARGET_IDS}
                self.stop_event.clear()
                self.thread = Thread(target=self._run, daemon=True, name="body-vector-monitor")
                self.thread.start()
        return self.snapshot()

    def stop(self):
        self.stop_event.set()
        thread = self.thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            return {
                "running": bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set()),
                "channel": self.channel,
                "error": self.error,
                "frames": [self.summaries[can_id].json(can_id) for can_id in TARGET_IDS],
            }

    def _run(self):
        try:
            import can

            with can.Bus(interface="vector", channel=self.channel, receive_own_messages=False) as bus:
                while not self.stop_event.is_set():
                    msg = bus.recv(timeout=0.25)
                    if msg and not msg.is_error_frame and msg.arbitration_id in self.summaries:
                        with self.lock:
                            self.summaries[msg.arbitration_id].add(msg)
        except Exception as exc:
            with self.lock:
                self.error = str(exc)[:300]


vector_monitor = VectorMonitor()
