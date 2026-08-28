"""板子端蓝牙 UDP 数据接收与云端转发程序。

直接把本文件复制到板子上，使用 Python 3.6 或更高版本运行：
    python3 Bluetooth_Module_Board.py

测试板子能否连接云端：
    python3 Bluetooth_Module_Board.py --cloud-test
"""

import argparse
import json
import os
import random
import socket
import time
from datetime import datetime
from urllib import error, request


# ======================== 板子配置 ========================
# 监听所有网卡的 UDP 5000 端口。蓝牙转 UDP 模块应向“板子IP:5000”发送。
UDP_HOST = "0.0.0.0"
UDP_PORT = 5000

# 当前云端公网地址。cpolar 地址变化后只需要修改这一项。
CLOUD_HTTP_URL = os.getenv("CLOUD_HTTP_URL", "http://61bf8db4.vip.cpolar.top")

# 必须与云端使用的车辆编号和 run_cloud.py 中的令牌一致。
VEHICLE_ID = os.getenv("VEHICLE_ID", "VHC-001")
INGEST_TOKEN = os.getenv(
    "VEHICLE_INGEST_TOKEN",
    "vcl_687Nfse29GsoYlX0j8hPaK4ctMv_5g4nXBeYpy1Obu0",
)

HTTP_TIMEOUT_SECONDS = 8
MAX_UDP_BYTES = 2048
LOG_FILE = "ble_udp_log.txt"
# =========================================================


def now_ms():
    return int(time.time() * 1000)


def bytes_to_hex(data):
    """兼容 Python 3.7 的带空格 HEX 格式。"""
    return " ".join("{:02x}".format(value) for value in data)


def make_test_data():
    text = "TEMP={:.1f};HUM={};BAT={};TEST=1".format(
        random.uniform(18.0, 36.0),
        random.randint(35, 85),
        random.randint(45, 100),
    )
    return text.encode("utf-8")


def build_payload(data, source):
    return {
        "source_time_ms": now_ms(),
        "source": source,
        "text": data.decode("utf-8", errors="ignore").strip(),
        "hex": bytes_to_hex(data),
        "byte_count": len(data),
    }


def forward_to_cloud(data, source):
    """向云端上传一条蓝牙报文，成功返回 True。"""
    url = "{}/api/vehicles/{}/bluetooth".format(
        CLOUD_HTTP_URL.rstrip("/"), VEHICLE_ID
    )
    body = json.dumps(build_payload(data, source), ensure_ascii=False).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer {}".format(INGEST_TOKEN),
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "VCL-Bluetooth-Board/1.0",
        },
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if 200 <= response.status < 300:
                return True
            print("[云端错误] HTTP {}".format(response.status))
    except error.HTTPError as exc:
        print("[云端拒绝] HTTP {} {}".format(exc.code, exc.reason))
        if exc.code == 401:
            print("请检查 INGEST_TOKEN 是否与 run_cloud.py 完全一致。")
    except error.URLError as exc:
        print("[连接失败] {}".format(exc.reason))
        print("请检查板子网络、DNS、系统时间和 CLOUD_HTTP_URL。")
    except socket.timeout:
        print("[连接超时] 云端在 {} 秒内没有响应。".format(HTTP_TIMEOUT_SECONDS))
    except Exception as exc:
        print("[上传异常] {}".format(exc))
    return False


def append_log(line):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")
    except OSError as exc:
        print("[日志写入失败] {}".format(exc))


def run_receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_HOST, UDP_PORT))

    print("=" * 62)
    print("蓝牙 UDP 接收器已启动")
    print("监听地址：{}:{}".format(UDP_HOST, UDP_PORT))
    print("车辆编号：{}".format(VEHICLE_ID))
    print("云端地址：{}".format(CLOUD_HTTP_URL))
    print("等待蓝牙转 UDP 模块发送数据，按 Ctrl+C 停止。")
    print("=" * 62)

    try:
        while True:
            data, addr = sock.recvfrom(MAX_UDP_BYTES)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            text = data.decode("utf-8", errors="ignore").strip()
            source = "{}:{}".format(addr[0], addr[1])
            line = "[{}] From {}  Data: {}".format(timestamp, source, text)

            print(line)
            print("HEX   : {}".format(bytes_to_hex(data)))
            append_log(line)
            accepted = forward_to_cloud(data, source)
            print("CLOUD : {}".format("上传成功" if accepted else "上传失败"))
            print("-" * 62)
    except KeyboardInterrupt:
        print("\n程序已停止。")
    finally:
        sock.close()


def run_cloud_test():
    """不需要蓝牙硬件，直接验证板子到云端的 HTTPS 链路。"""
    data = make_test_data()
    text = data.decode("utf-8")
    print("正在发送云端测试数据：{}".format(text))
    if forward_to_cloud(data, "BOARD-CLOUD-TEST"):
        print("测试成功：请打开网页底部的蓝牙数据模块查看。")
        return 0
    print("测试失败：请根据上面的错误信息检查配置。")
    return 1


def send_local_udp_test(count):
    """向本机 UDP 5000 发送随机数据，需要接收程序已在另一个终端运行。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for index in range(max(1, count)):
            data = make_test_data()
            sock.sendto(data, ("127.0.0.1", UDP_PORT))
            print("已发送本机 UDP 测试数据：{}".format(data.decode("utf-8")))
            if index + 1 < count:
                time.sleep(0.3)
    finally:
        sock.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description="板子端蓝牙 UDP 云端转发程序")
    parser.add_argument(
        "--cloud-test",
        action="store_true",
        help="生成一条随机数据直接上传云端，然后退出",
    )
    parser.add_argument(
        "--udp-test",
        type=int,
        metavar="COUNT",
        help="向本机 UDP 端口发送指定条数的测试数据，然后退出",
    )
    args = parser.parse_args()

    if args.cloud_test:
        return run_cloud_test()
    if args.udp_test is not None:
        return send_local_udp_test(args.udp_test)
    run_receiver()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
