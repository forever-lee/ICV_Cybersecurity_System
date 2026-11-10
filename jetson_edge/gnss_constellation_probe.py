#!/usr/bin/env python3
"""Read u-blox NMEA from a UART and identify the reported GNSS constellations.

GSV describes satellites in view, while GSA describes satellites used by the
navigation solution.  The distinction is important: seeing BeiDou satellites
does not by itself prove that BeiDou participates in the current fix.
"""

import argparse
import collections
import datetime
import os
import sys
import time


DEFAULT_DEVICE = "/dev/ttyTHS1"
DEFAULT_BAUD = 38400

TALKER_NAMES = {
    "GP": "GPS",
    "GB": "BeiDou",
    "BD": "BeiDou",
    "GL": "GLONASS",
    "GA": "Galileo",
    "GQ": "QZSS",
    "GI": "NavIC",
    "GN": "Mixed/combined GNSS",
}

# NMEA 4.x system IDs used in GSA/GSV extensions.
SYSTEM_ID_NAMES = {
    1: "GPS",
    2: "GLONASS",
    3: "Galileo",
    4: "BeiDou",
    5: "QZSS",
    6: "NavIC",
}


def checksum_valid(sentence):
    if not sentence.startswith("$") or "*" not in sentence:
        return False
    body, supplied = sentence[1:].split("*", 1)
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    try:
        return checksum == int(supplied[:2], 16)
    except ValueError:
        return False


def split_sentence(sentence):
    """Return (talker, formatter, fields) for a valid standard NMEA line."""
    if not checksum_valid(sentence):
        return None
    body = sentence[1:].split("*", 1)[0]
    fields = body.split(",")
    header = fields[0].upper()
    if len(header) != 5:
        return None
    return header[:2], header[2:], fields


def integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def constellation_from_sv_id(sv_id):
    """Classify an NMEA satellite ID when the talker/system ID is unavailable."""
    if sv_id is None:
        return None
    if 1 <= sv_id <= 32:
        return "GPS"
    if 33 <= sv_id <= 64:
        return "SBAS"
    if 65 <= sv_id <= 96:
        return "GLONASS"
    if 193 <= sv_id <= 200:
        return "QZSS"
    if 201 <= sv_id <= 237:
        return "BeiDou"
    if 301 <= sv_id <= 336:
        return "Galileo"
    if 401 <= sv_id <= 414:
        return "NavIC"
    return None


def constellation_for(talker, system_id=None, sv_id=None):
    if system_id in SYSTEM_ID_NAMES:
        return SYSTEM_ID_NAMES[system_id]
    if talker != "GN" and talker in TALKER_NAMES:
        return TALKER_NAMES[talker]
    return constellation_from_sv_id(sv_id)


class Evidence:
    def __init__(self):
        self.valid_lines = 0
        self.bad_checksum_lines = 0
        self.non_nmea_lines = 0
        self.talkers = collections.Counter()
        self.messages = collections.Counter()
        self.visible = collections.defaultdict(set)
        self.used = collections.defaultdict(set)
        self.fix_sentences = collections.Counter()

    def add(self, sentence):
        parsed = split_sentence(sentence)
        if parsed is None:
            if sentence.startswith("$"):
                self.bad_checksum_lines += 1
            else:
                self.non_nmea_lines += 1
            return

        talker, formatter, fields = parsed
        self.valid_lines += 1
        self.talkers[talker] += 1
        self.messages[talker + formatter] += 1

        if formatter in ("GGA", "RMC", "GNS"):
            self.fix_sentences[formatter] += 1
        elif formatter == "GSV":
            self._add_gsv(talker, fields)
        elif formatter == "GSA":
            self._add_gsa(talker, fields)

    def _add_gsv(self, talker, fields):
        total_satellites = integer(fields[3]) if len(fields) > 3 else None
        message_number = integer(fields[2]) if len(fields) > 2 else None
        if total_satellites is None or message_number is None:
            return

        entries = max(0, min(4, total_satellites - (message_number - 1) * 4))
        for entry in range(entries):
            index = 4 + entry * 4
            if index >= len(fields):
                break
            sv_id = integer(fields[index])
            constellation = constellation_for(talker, sv_id=sv_id)
            if constellation and sv_id is not None:
                self.visible[constellation].add(sv_id)

    def _add_gsa(self, talker, fields):
        # GSA: mode1, mode2, 12 satellite IDs, PDOP, HDOP, VDOP, [system ID].
        system_id = integer(fields[18]) if len(fields) > 18 else None
        for value in fields[3:15]:
            sv_id = integer(value)
            if sv_id is None:
                continue
            constellation = constellation_for(talker, system_id, sv_id)
            if constellation:
                self.used[constellation].add(sv_id)


def join_names(names):
    order = ("GPS", "BeiDou", "GLONASS", "Galileo", "QZSS", "NavIC", "SBAS")
    return ", ".join(name for name in order if name in names) or "无"


def print_summary(evidence, device, baud, raw_log):
    used_names = set(evidence.used)
    visible_names = set(evidence.visible)

    print("\n========== GNSS 星座分析结果 ==========")
    print("串口：{} @ {} baud".format(device, baud))
    print("有效NMEA：{} 条；校验失败：{} 条；非NMEA：{} 条".format(
        evidence.valid_lines,
        evidence.bad_checksum_lines,
        evidence.non_nmea_lines,
    ))
    print("Talker ID：{}".format(
        ", ".join(
            "{}={}({})".format(code, TALKER_NAMES.get(code, "未知"), count)
            for code, count in sorted(evidence.talkers.items())
        ) or "无"
    ))
    print("GSV可见星座：{}".format(join_names(visible_names)))
    for name in sorted(visible_names):
        print("  {:8s} 可见卫星ID：{}".format(
            name, ",".join(str(value) for value in sorted(evidence.visible[name]))
        ))
    print("GSA参与定位：{}".format(join_names(used_names)))
    for name in sorted(used_names):
        print("  {:8s} 使用卫星ID：{}".format(
            name, ",".join(str(value) for value in sorted(evidence.used[name]))
        ))

    if len(used_names) > 1:
        conclusion = "{} 多星座联合定位（GSA证据）".format(join_names(used_names))
    elif used_names == {"BeiDou"}:
        conclusion = "北斗参与并单独构成当前已识别定位星座（GSA证据）"
    elif used_names == {"GPS"}:
        conclusion = "GPS参与并单独构成当前已识别定位星座（GSA证据）"
    elif used_names:
        conclusion = "参与定位的星座为：{}（GSA证据）".format(join_names(used_names))
    elif "GPS" in visible_names and "BeiDou" in visible_names:
        conclusion = "能接收到GPS和北斗，但缺少GSA，无法确认哪些卫星参与定位"
    elif visible_names:
        conclusion = "仅确认可见星座为：{}；缺少GSA，不能证明参与定位".format(
            join_names(visible_names)
        )
    elif evidence.valid_lines:
        conclusion = "收到NMEA，但没有足够的GSA/GSV证据，无法判断GPS还是北斗"
    else:
        conclusion = "未收到有效NMEA，请检查串口、波特率、接线和串口占用"

    print("结论：{}".format(conclusion))
    print("原始报文日志：{}".format(os.path.abspath(raw_log)))
    print("说明：GSV表示可见卫星，GSA表示参与定位；GGA/RMC本身不能证明星座。")


def parse_args():
    parser = argparse.ArgumentParser(
        description="读取u-blox NMEA并判断GPS、北斗或混合星座"
    )
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="NMEA串口设备")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="串口波特率")
    parser.add_argument(
        "--duration", type=float, default=60.0,
        help="采集秒数，默认60；设置0表示持续运行到Ctrl+C",
    )
    parser.add_argument(
        "--log", default=None,
        help="原始NMEA日志路径；默认写入当前目录logs/",
    )
    parser.add_argument(
        "--show-raw", action="store_true", help="同时在终端打印每条原始NMEA",
    )
    return parser.parse_args()


def default_log_path():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join("logs", "gnss_nmea_probe_{}.log".format(timestamp))


def main():
    args = parse_args()
    raw_log = args.log or default_log_path()
    log_directory = os.path.dirname(os.path.abspath(raw_log))
    os.makedirs(log_directory, exist_ok=True)

    try:
        import serial
    except ImportError:
        print("缺少pyserial，请执行：python3 -m pip install pyserial", file=sys.stderr)
        return 2

    evidence = Evidence()
    port = None
    started = time.monotonic()
    deadline = started + args.duration if args.duration > 0 else None

    print("打开串口 {} @ {} baud".format(args.device, args.baud))
    print("采集时间：{}".format(
        "持续到Ctrl+C" if deadline is None else "{}秒".format(args.duration)
    ))
    print("注意：运行本脚本前请停止其他读取同一串口的程序。")

    try:
        port = serial.Serial(args.device, args.baud, timeout=1)
        try:
            port.reset_input_buffer()
        except (AttributeError, OSError):
            pass

        with open(raw_log, "w", encoding="ascii", errors="backslashreplace") as log_file:
            while deadline is None or time.monotonic() < deadline:
                raw = port.readline()
                if not raw:
                    continue
                sentence = raw.decode("ascii", "replace").strip()
                if not sentence:
                    continue
                received_at = datetime.datetime.now().isoformat(timespec="milliseconds")
                log_file.write("{} {}\n".format(received_at, sentence))
                log_file.flush()
                if args.show_raw:
                    print(sentence)
                evidence.add(sentence)
    except KeyboardInterrupt:
        print("\n收到Ctrl+C，停止采集。")
    except (OSError, serial.SerialException) as exc:
        print("串口读取失败：{}".format(exc), file=sys.stderr)
        print("请确认start_edge.py/navigation_gps.py未占用该串口，并检查用户是否属于dialout组。", file=sys.stderr)
        return 1
    finally:
        if port is not None:
            try:
                port.close()
            except OSError:
                pass

    print_summary(evidence, args.device, args.baud, raw_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
