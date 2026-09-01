#!/usr/bin/env python3
"""Jetson edge GNSS/CAN navigation collector.

The collector accepts either standard GNSS NMEA sentences from a serial port,
JSON/NMEA packets over UDP, or a deterministic test route.  It atomically
writes the latest vehicle navigation sample for ``h264_vehicle_agent.py``.

Examples:
    python3 Navigation_Module_Board.py --mode udp
    python3 Navigation_Module_Board.py --mode serial
    python3 Navigation_Module_Board.py --mode test
"""

import argparse
import json
import math
import os
import socket
import sys
import time


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT = os.path.join(BASE_DIR, "navigation_live.json")
DEFAULT_AT_DEVICE = "/dev/ttyUSB2"
DEFAULT_NMEA_DEVICE = "/dev/ttyUSB1"
DEFAULT_SERIAL_BAUD = 115200
AT_COMMAND_TIMEOUT_SECONDS = 5.0


class GnssAtError(RuntimeError):
    """Raised when the EC20 rejects an AT command or does not answer in time."""


def now_ms():
    return int(time.time() * 1000)


def serial_text(raw):
    if isinstance(raw, bytes):
        return raw.decode("ascii", "replace").strip()
    return str(raw or "").strip()


def send_at_command(port, command, timeout=AT_COMMAND_TIMEOUT_SECONDS):
    """Send one EC20 AT command and return response lines preceding ``OK``."""
    try:
        port.reset_input_buffer()
    except (AttributeError, OSError):
        pass

    try:
        port.write((command + "\r\n").encode("ascii"))
        port.flush()
    except (OSError, ValueError) as exc:
        raise GnssAtError("AT命令发送失败 {}: {}".format(command, exc))

    response = []
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        try:
            line = serial_text(port.readline())
        except OSError as exc:
            raise GnssAtError("AT响应读取失败 {}: {}".format(command, exc))
        if not line or line == command:
            continue
        if line == "OK":
            return response
        if line == "ERROR" or line.startswith("+CME ERROR"):
            raise GnssAtError("EC20拒绝命令 {}: {}".format(command, line))
        response.append(line)
    raise GnssAtError("等待EC20响应超时: {}".format(command))


def parse_qgps_state(response):
    for line in response:
        if not line.startswith("+QGPS:"):
            continue
        value = line.split(":", 1)[1].strip()
        if value in ("0", "1"):
            return int(value)
    raise GnssAtError("AT+QGPS?未返回有效GNSS状态: {}".format(", ".join(response)))


def start_gnss_session(port, timeout=AT_COMMAND_TIMEOUT_SECONDS):
    """Configure EC20 USB NMEA output and ensure a continuous GNSS session."""
    send_at_command(port, "AT", timeout)
    send_at_command(port, 'AT+QGPSCFG="outport","usbnmea"', timeout)
    state = parse_qgps_state(send_at_command(port, "AT+QGPS?", timeout))
    if state == 0:
        send_at_command(port, "AT+QGPS=1", timeout)
        return True
    return False


def stop_gnss_session(port, timeout=AT_COMMAND_TIMEOUT_SECONDS):
    send_at_command(port, "AT+QGPSEND", timeout)


def finite_number(value, minimum, maximum):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


def first_value(payload, names):
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return None


def normalize_json_payload(payload):
    if isinstance(payload, dict) and isinstance(payload.get("navigation"), dict):
        payload = payload["navigation"]
    if not isinstance(payload, dict):
        return None

    latitude = finite_number(first_value(payload, ("latitude", "lat")), -90, 90)
    longitude = finite_number(
        first_value(payload, ("longitude", "lng", "lon")), -180, 180
    )
    speed = finite_number(first_value(payload, ("speed_kph", "speed")), 0, 500)
    speed_unit = str(payload.get("speed_unit", "kph")).lower().replace("/", "")
    if speed is not None:
        if speed_unit in ("mps", "ms", "metersecond", "meterssecond"):
            speed *= 3.6
        elif speed_unit in ("knot", "knots", "kt", "kts"):
            speed *= 1.852
        speed = finite_number(speed, 0, 500)
    heading = finite_number(
        first_value(payload, ("heading_deg", "heading", "course")), 0, 360
    )
    accuracy = finite_number(
        first_value(payload, ("accuracy_m", "accuracy")), 0, 100000
    )
    coordinate_system = str(payload.get("coordinate_system", "WGS84")).upper()
    coordinate_system = coordinate_system.replace("-", "")
    if coordinate_system not in ("WGS84", "GCJ02"):
        coordinate_system = "WGS84"

    result = {
        "coordinate_system": coordinate_system,
        "source": str(payload.get("source", "EDGE-GNSS/CAN"))[:32],
        "captured_at_ms": now_ms(),
    }
    if latitude is not None:
        result["latitude"] = latitude
    if longitude is not None:
        result["longitude"] = longitude
    if speed is not None:
        result["speed_kph"] = speed
    if heading is not None:
        result["heading_deg"] = heading % 360
    if accuracy is not None:
        result["accuracy_m"] = accuracy
    return result


def nmea_checksum_valid(sentence):
    sentence = sentence.strip()
    if not sentence.startswith("$") or "*" not in sentence:
        return sentence.startswith("$")
    body, expected = sentence[1:].split("*", 1)
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    try:
        return checksum == int(expected[:2], 16)
    except ValueError:
        return False


def nmea_coordinate(value, hemisphere):
    number = finite_number(value, 0, 18000)
    if number is None:
        return None
    degrees = int(number // 100)
    minutes = number - degrees * 100
    coordinate = degrees + minutes / 60.0
    if hemisphere in ("S", "W"):
        coordinate = -coordinate
    return coordinate


def nmea_message_type(sentence):
    sentence = sentence.strip()
    if not sentence.startswith("$"):
        return "UNKNOWN"
    body = sentence[1:].split("*", 1)[0]
    first_field = body.split(",", 1)[0]
    return first_field[-3:].upper() if first_field else "UNKNOWN"


def nmea_rejection_reason(sentence):
    """Explain why a received NMEA sentence does not contain a usable fix."""
    sentence = sentence.strip()
    if not sentence:
        return "空报文"
    if not sentence.startswith("$"):
        return "不是NMEA报文"
    if not nmea_checksum_valid(sentence):
        return "校验和错误"

    fields = sentence[1:].split("*", 1)[0].split(",")
    message_type = fields[0][-3:].upper() if fields else ""
    if message_type == "RMC":
        if len(fields) < 9:
            return "RMC字段不完整"
        if fields[2].upper() != "A":
            return "RMC状态为{}，尚未定位".format(fields[2] or "空")
        return "RMC经纬度无效"
    if message_type == "GGA":
        if len(fields) < 7:
            return "GGA字段不完整"
        return "GGA定位质量为{}，尚未定位".format(fields[6] or "0")
    if message_type == "GNS":
        if len(fields) < 7:
            return "GNS字段不完整"
        return "GNS模式为{}，尚未定位".format(fields[6] or "N")
    if message_type == "VTG":
        return "速度/航向报文，等待GGA、RMC或GNS经纬度"
    if message_type in ("GSA", "GSV"):
        return "卫星状态报文，不包含完整经纬度"
    return "暂不支持的NMEA类型 {}".format(message_type or "UNKNOWN")


def parse_nmea(sentence):
    sentence = sentence.strip()
    if not sentence or not nmea_checksum_valid(sentence):
        return None
    body = sentence[1:].split("*", 1)[0]
    fields = body.split(",")
    message_type = fields[0][-3:].upper() if fields else ""

    if message_type == "RMC" and len(fields) >= 9:
        if fields[2].upper() != "A":
            return None
        latitude = nmea_coordinate(fields[3], fields[4].upper())
        longitude = nmea_coordinate(fields[5], fields[6].upper())
        if latitude is None or longitude is None:
            return None
        speed_knots = finite_number(fields[7], 0, 270)
        heading = finite_number(fields[8], 0, 360)
        result = {
            "latitude": latitude,
            "longitude": longitude,
            "coordinate_system": "WGS84",
            "source": "EDGE-GNSS-RMC",
            "captured_at_ms": now_ms(),
        }
        if speed_knots is not None:
            result["speed_kph"] = speed_knots * 1.852
        if heading is not None:
            result["heading_deg"] = heading % 360
        return result

    if message_type == "GGA" and len(fields) >= 7:
        fix_quality = finite_number(fields[6], 0, 9)
        if not fix_quality:
            return None
        latitude = nmea_coordinate(fields[2], fields[3].upper())
        longitude = nmea_coordinate(fields[4], fields[5].upper())
        if latitude is None or longitude is None:
            return None
        return {
            "latitude": latitude,
            "longitude": longitude,
            "coordinate_system": "WGS84",
            "source": "EDGE-GNSS-GGA",
            "captured_at_ms": now_ms(),
        }

    if message_type == "GNS" and len(fields) >= 7:
        mode = fields[6].upper()
        if not mode or all(character == "N" for character in mode):
            return None
        latitude = nmea_coordinate(fields[2], fields[3].upper())
        longitude = nmea_coordinate(fields[4], fields[5].upper())
        if latitude is None or longitude is None:
            return None
        return {
            "latitude": latitude,
            "longitude": longitude,
            "coordinate_system": "WGS84",
            "source": "EDGE-GNSS-GNS",
            "captured_at_ms": now_ms(),
        }

    if message_type == "VTG" and len(fields) >= 8:
        heading = finite_number(fields[1], 0, 360)
        speed = finite_number(fields[7], 0, 500)
        result = {
            "source": "EDGE-GNSS-VTG",
            "captured_at_ms": now_ms(),
        }
        if heading is not None:
            result["heading_deg"] = heading % 360
        if speed is not None:
            result["speed_kph"] = speed
        return result if len(result) > 2 else None
    return None


class NavigationState:
    def __init__(self, output_path):
        self.output_path = os.path.abspath(output_path)
        self.values = {}

    def update(self, sample):
        if not sample:
            return False
        self.values.update(sample)
        latitude = finite_number(self.values.get("latitude"), -90, 90)
        longitude = finite_number(self.values.get("longitude"), -180, 180)
        if latitude is None or longitude is None:
            return False
        self.values["latitude"] = round(latitude, 7)
        self.values["longitude"] = round(longitude, 7)
        if "speed_kph" in self.values:
            self.values["speed_kph"] = round(float(self.values["speed_kph"]), 1)
        if "heading_deg" in self.values:
            self.values["heading_deg"] = round(float(self.values["heading_deg"]) % 360, 1)
        if "accuracy_m" in self.values:
            self.values["accuracy_m"] = round(float(self.values["accuracy_m"]), 1)
        self.values["captured_at_ms"] = int(sample.get("captured_at_ms") or now_ms())
        self.write_atomic()
        return True

    def write_atomic(self):
        directory = os.path.dirname(self.output_path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        temporary_path = self.output_path + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump(self.values, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, self.output_path)


def handle_text(state, text):
    updated = False
    for line in text.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            payload = parse_nmea(line)
        else:
            payload = normalize_json_payload(payload)
        if state.update(payload):
            updated = True
    return updated


def run_udp(args, state):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.udp_host, args.udp_port))
    print("边缘导航UDP监听：{}:{}".format(args.udp_host, args.udp_port))
    print("输出文件：{}".format(state.output_path))
    while True:
        packet, address = sock.recvfrom(65535)
        text = packet.decode("utf-8", "replace")
        if handle_text(state, text):
            print(
                "导航更新 {}:{}  经度={:.6f} 纬度={:.6f} 速度={} km/h".format(
                    address[0], address[1], state.values["longitude"],
                    state.values["latitude"], state.values.get("speed_kph", "—")
                )
            )


def run_serial(args, state):
    try:
        import serial
    except ImportError:
        raise SystemExit("串口模式需要安装 pyserial：python3 -m pip install pyserial")

    at_port = None
    nmea_port = None
    session_active = False
    print("EC20 AT串口：{} @ {}".format(args.at_device, args.at_baud))
    print("EC20 NMEA串口：{} @ {}".format(args.nmea_device, args.nmea_baud))
    print("输出文件：{}".format(state.output_path))
    try:
        at_port = serial.Serial(
            args.at_device,
            args.at_baud,
            timeout=0.2,
            write_timeout=2,
        )
        nmea_port = serial.Serial(args.nmea_device, args.nmea_baud, timeout=2)
        started_now = start_gnss_session(at_port, args.at_timeout)
        session_active = True
        print("EC20 GNSS会话{}，开始接收NMEA".format(
            "已启动" if started_now else "已经运行"
        ))
        while True:
            sentence = nmea_port.readline().decode("ascii", "replace").strip()
            if not sentence:
                continue
            sample = parse_nmea(sentence)
            message_type = nmea_message_type(sentence)
            if sample is None:
                print("NMEA {}：无有效GPS数据（{}）".format(
                    message_type, nmea_rejection_reason(sentence)
                ))
                continue
            if state.update(sample):
                if "latitude" not in sample or "longitude" not in sample:
                    print("NMEA {}：辅助数据已合并，等待/沿用最近有效位置".format(
                        message_type
                    ))
                    continue
                print(
                    "NMEA {}：有效GPS定位 经度={:.6f} 纬度={:.6f} "
                    "速度={} km/h，已写入 {}".format(
                        message_type,
                        state.values["longitude"], state.values["latitude"],
                        state.values.get("speed_kph", "—"), state.output_path
                    )
                )
            else:
                print("NMEA {}：已接收辅助数据，等待有效经纬度".format(message_type))
    except getattr(serial, "SerialException", OSError) as exc:
        raise GnssAtError("EC20串口访问失败: {}".format(exc))
    finally:
        if session_active and at_port is not None:
            try:
                stop_gnss_session(at_port, args.at_timeout)
                print("EC20 GNSS会话已结束")
            except GnssAtError as exc:
                print("警告：无法结束EC20 GNSS会话：{}".format(exc), file=sys.stderr)
        for port in (nmea_port, at_port):
            if port is not None:
                try:
                    port.close()
                except OSError:
                    pass


def run_test_route(args, state):
    print("边缘导航测试轨迹启动；该模式不读取浏览器或手机定位。")
    center_latitude = args.test_latitude
    center_longitude = args.test_longitude
    step = 0
    while True:
        angle = math.radians(step % 360)
        sample = {
            "latitude": center_latitude + math.sin(angle) * 0.003,
            "longitude": center_longitude + math.cos(angle) * 0.003,
            "speed_kph": 36.0,
            "heading_deg": (step + 90) % 360,
            "accuracy_m": 1.5,
            "coordinate_system": "WGS84",
            "source": "EDGE-GNSS-TEST",
            "captured_at_ms": now_ms(),
        }
        state.update(sample)
        step += 3
        time.sleep(1)


def parse_args():
    parser = argparse.ArgumentParser(description="Jetson GNSS/CAN navigation collector")
    parser.add_argument("--mode", choices=("udp", "serial", "test"), default="udp")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--udp-host", default="0.0.0.0")
    parser.add_argument("--udp-port", type=int, default=7000)
    parser.add_argument("--at-device", default=DEFAULT_AT_DEVICE)
    parser.add_argument("--at-baud", type=int, default=DEFAULT_SERIAL_BAUD)
    parser.add_argument(
        "--nmea-device", "--serial-device", dest="nmea_device",
        default=DEFAULT_NMEA_DEVICE,
        help="EC20 USB NMEA port; --serial-device is a compatibility alias",
    )
    parser.add_argument(
        "--nmea-baud", "--baud", dest="nmea_baud", type=int,
        default=DEFAULT_SERIAL_BAUD,
        help="EC20 USB NMEA baud rate; --baud is a compatibility alias",
    )
    parser.add_argument("--at-timeout", type=float, default=AT_COMMAND_TIMEOUT_SECONDS)
    parser.add_argument("--test-latitude", type=float, default=23.1291)
    parser.add_argument("--test-longitude", type=float, default=113.2644)
    return parser.parse_args()


def main():
    args = parse_args()
    state = NavigationState(args.output)
    try:
        if args.mode == "serial":
            run_serial(args, state)
        elif args.mode == "test":
            run_test_route(args, state)
        else:
            run_udp(args, state)
    except KeyboardInterrupt:
        print("边缘导航采集已停止")
    except GnssAtError as exc:
        print("边缘GNSS启动或采集失败：{}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
