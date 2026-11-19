"""Small, bounded CAN frame summaries for BLF uploads and local Vector capture."""

from collections import Counter, deque
from statistics import median
from threading import Event, Lock, Thread
from types import SimpleNamespace
import time

TARGET_IDS = (0x1D1, 0x228)
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

# VCU-ESP sheet in 2028Pch05_整合通信矩阵(1).xlsx. Starts use Intel/LSB0
# numbering; the workbook itself does not specify byte order explicitly.
SIGNALS = {
    0x1D1: [
        ("MbRegenWhl", "制动能量回收轮端实际扭矩", 8, 16, 1, -32768, "Nm", None, None),
        ("MbRegenMaxWhl", "制动能量回收轮端允许最大扭矩", 24, 16, 1, -32768, "Nm", None, None),
        ("MbRegenWhlQlfr", "制动能量回收轮端实际扭矩有效状态", 38, 2, 1, 0, "", {0:"未初始化",1:"正常",2:"故障"}, None),
        ("MbRegenMaxWhlQlfr", "制动能量回收轮端允许最大扭矩有效状态", 36, 2, 1, 0, "", {0:"未初始化",1:"正常",2:"故障"}, None),
        ("VcuMotSts", "电机运行状态", 34, 2, 1, 0, "", {0:"停止",1:"保留",2:"运行",3:"无效"}, "0x00：未点火发送停止；0x02：点火成功发送运行"),
        ("VcuAccrPedlVld", "加速踏板位置有效", 32, 1, 1, 0, "", {0:"有效",1:"无效"}, None),
        ("VcuAccrPedl", "加速踏板位置", 40, 8, .392, 0, "%", None, None),
        ("VcuBrkPedlSts", "制动踏板状态", 54, 2, 1, 0, "", {0:"未踩下",1:"已踩下",2:"保留",3:"错误"}, None),
        ("Vcu1D1CycCntr", "本帧计数", 48, 4, 1, 0, "", None, None),
        ("VcuCRCChk1D1", "校验", 56, 8, 1, 0, "", None, "CRC_8校验，多项式采用 X8+X4+X3+X2+1；这里只展示原值"),
    ],
    0x228: [
        ("MbRegenTarWhl", "制动能量回收轮端目标扭矩", 8, 16, 1, -32768, "Nm", None, None),
        ("ESPTqDecFastwhl", "ESP 减扭轮端扭矩", 25, 15, 1, -5400, "Nm", None, None),
        ("MbRegenTarWhlQlfr", "制动能量回收轮端目标扭矩有效状态", 40, 2, 1, 0, "", {0:"未初始化",1:"正常",2:"故障"}, None),
        ("ESPTqDecActv", "减扭生效标志", 55, 1, 1, 0, "", {0:"未生效",1:"已生效"}, None),
        ("EPBi_RollingCounter_228", "本帧计数", 48, 4, 1, 0, "", None, None),
        ("EPBi_CheckSum_228", "校验和", 56, 8, 1, 0, "", None, "表中规定为前 7 字节之和按位异或 0xFF；这里只展示原值"),
    ],
}


def decode_signals(can_id, payload):
    data = bytes(payload)
    bits = int.from_bytes(data, "little")
    result = []
    for name, label, start, length, factor, offset, unit, meanings, note in SIGNALS[can_id]:
        if len(data) * 8 < start + length:
            result.append({"name": name, "label": label, "raw": "—", "value": "—",
                           "unit": unit, "meaning": "矩阵位范围超出当前报文，无法解码",
                           "note": "请核对 Excel 起始位和长度" if len(data) == 8 else "报文长度不足",
                           "start_bit": start, "bit_length": length})
            continue
        raw = (bits >> start) & ((1 << length) - 1)
        value = round(raw * factor + offset, 4)
        result.append({"name": name, "label": label, "raw": "0x{:X}".format(raw),
                       "value": value, "unit": unit, "meaning": (meanings or {}).get(raw, ""),
                       "note": note or "", "start_bit": start, "bit_length": length})
    return result


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
        self.latest_payload = bytes(msg.data)

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
            "nominal_cycle_ms": 10,
            "signals": decode_signals(can_id, self.latest_payload) if self.last is not None else [],
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


class EdgeFrameMonitor:
    """Inspector-only frames delivered by K3 through Jetson."""

    def __init__(self):
        self.lock = Lock()
        self.frames = {can_id: FrameSummary() for can_id in TARGET_IDS}
        self.last_received = 0.0

    def ingest(self, items):
        with self.lock:
            for item in items:
                can_id = int(item["can_id"])
                if can_id not in self.frames:
                    continue
                data = bytes.fromhex(item["data_hex"])
                if len(data) != 8:
                    continue
                msg = SimpleNamespace(timestamp=float(item["received_at_ms"]) / 1000,
                                      dlc=8, is_fd=False, is_rx=True, data=data)
                self.frames[can_id].add(msg)
                self.last_received = time.monotonic()
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            return {"running": self.last_received > 0 and time.monotonic() - self.last_received < 5,
                    "frames": [self.frames[can_id].json(can_id) for can_id in TARGET_IDS]}


_edge_monitors = {}
_edge_monitors_lock = Lock()


def edge_monitor_for(vehicle_id):
    with _edge_monitors_lock:
        return _edge_monitors.setdefault(vehicle_id, EdgeFrameMonitor())
