#!/usr/bin/env python3
"""Jetson body-domain edge agent for S32K344 UDP/serial data."""

import argparse
import json
import os
import queue
import socket
import threading
import time
from urllib import error, request

try:
    from .body_protocol import (
        PACKET_MAGIC,
        PACKET_SIZE,
        TIMESTAMPED_PACKET_SIZE,
        build_serial_command,
        build_time_sync_packet,
        decode_serial_packet,
        ethernet_payload,
        normalize_update,
        now_ms,
        serial_payload,
    )
except ImportError:
    from body_protocol import (
        PACKET_MAGIC,
        PACKET_SIZE,
        TIMESTAMPED_PACKET_SIZE,
        build_serial_command,
        build_time_sync_packet,
        decode_serial_packet,
        ethernet_payload,
        normalize_update,
        now_ms,
        serial_payload,
    )


COMMAND_POLL_INTERVAL_SECONDS = 0.5
TIME_SYNC_INTERVAL_SECONDS = 5.0
UPLOAD_QUEUE_SIZE = 4


def forward_to_cloud(payload, cloud_url, vehicle_id, token):
    url = "{}/api/vehicles/{}/domains/body".format(cloud_url.rstrip("/"), vehicle_id)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "V-SHIELD-Body-Edge/1.0",
        },
        method="POST",
    )
    with request.urlopen(http_request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def poll_transmission_command(
    cloud_url, vehicle_id, token, last_sequence, acknowledged_sequence=0
):
    url = "{}/api/vehicles/{}/domains/body/control".format(
        cloud_url.rstrip("/"), vehicle_id
    )
    body = json.dumps(
        {
            "last_sequence": int(last_sequence),
            "acknowledged_sequence": int(acknowledged_sequence),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "V-SHIELD-Body-Edge/1.0",
        },
        method="POST",
    )
    with request.urlopen(http_request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def process_pending_command(
    result, last_sequence, send_packet
):
    if not result.get("pending"):
        return last_sequence, 0
    command = result.get("command") or {}
    sequence = int(command.get("sequence") or 0)
    action = str(command.get("action") or "")
    if sequence <= last_sequence or action not in {"start", "stop"}:
        return last_sequence, 0
    send_packet(build_serial_command(action))
    print(
        "[{}] 已向开发板下发{}命令 SEQ={}".format(
            time.strftime("%H:%M:%S"),
            "启动发送" if action == "start" else "停止发送",
            sequence,
        )
    )
    return sequence, sequence


def enqueue_latest(upload_queue, item):
    """Queue a live update without allowing stale frames to build up."""
    try:
        upload_queue.put_nowait(item)
        return False
    except queue.Full:
        try:
            upload_queue.get_nowait()
            upload_queue.task_done()
        except queue.Empty:
            pass
        upload_queue.put_nowait(item)
        return True


def run_udp_uploader(args, upload_queue):
    while True:
        payload, description = upload_queue.get()
        try:
            result = forward_to_cloud(
                payload, args.cloud, args.vehicle_id, args.token
            )
            print("[{}] {} 已上传 accepted={}".format(
                time.strftime("%H:%M:%S"), description, result.get("accepted")
            ))
        except (error.URLError, OSError, ValueError) as exc:
            print("[{}] 云端上传失败：{}".format(time.strftime("%H:%M:%S"), exc))
        finally:
            upload_queue.task_done()


def run_udp_maintenance(args, udp_socket, board_control_address):
    """Keep cloud control polling and board time sync off the receive path."""
    last_command_sequence = 0
    acknowledged_sequence = 0
    next_command_poll = 0.0
    next_time_sync = 0.0
    while True:
        current_time = time.monotonic()
        if current_time >= next_time_sync:
            try:
                udp_socket.sendto(build_time_sync_packet(), board_control_address)
            except OSError as exc:
                print("[{}] 板端时钟校准失败：{}".format(time.strftime("%H:%M:%S"), exc))
            next_time_sync = current_time + TIME_SYNC_INTERVAL_SECONDS

        if current_time >= next_command_poll:
            try:
                result = poll_transmission_command(
                    args.cloud, args.vehicle_id, args.token,
                    last_command_sequence, acknowledged_sequence,
                )
                acknowledged_sequence = 0
                last_command_sequence, acknowledged_sequence = process_pending_command(
                    result, last_command_sequence,
                    lambda packet: udp_socket.sendto(packet, board_control_address),
                )
            except (error.URLError, OSError, ValueError) as exc:
                print("[{}] 控制命令轮询失败：{}".format(time.strftime("%H:%M:%S"), exc))
            next_command_poll = current_time + COMMAND_POLL_INTERVAL_SECONDS
        time.sleep(0.05)


def run_serial(args):
    try:
        import serial
    except ImportError as exc:
        raise SystemExit("串口模式需要 pyserial，请执行：python3 -m pip install pyserial") from exc

    buffer = bytearray()
    last_command_sequence = 0
    acknowledged_sequence = 0
    next_command_poll = 0.0
    while True:
        try:
            with serial.Serial(args.serial_port, baudrate=args.baudrate, timeout=1) as connection:
                print("车身域串口已连接：{} @ {} 8N1".format(args.serial_port, args.baudrate))
                buffer.clear()
                while True:
                    current_time = time.monotonic()
                    if current_time >= next_command_poll:
                        try:
                            result = poll_transmission_command(
                                args.cloud, args.vehicle_id, args.token,
                                last_command_sequence, acknowledged_sequence,
                            )
                            acknowledged_sequence = 0
                            last_command_sequence, acknowledged_sequence = process_pending_command(
                                result, last_command_sequence,
                                lambda packet: (connection.write(packet), connection.flush()),
                            )
                        except (error.URLError, OSError, ValueError) as exc:
                            print("[{}] 控制命令轮询失败：{}".format(time.strftime("%H:%M:%S"), exc))
                        next_command_poll = current_time + COMMAND_POLL_INTERVAL_SECONDS

                    chunk = connection.read(64)
                    if not chunk:
                        continue
                    buffer.extend(chunk)
                    while True:
                        magic_index = buffer.find(PACKET_MAGIC)
                        if magic_index < 0:
                            if buffer[-1:] != PACKET_MAGIC[:1]:
                                buffer.clear()
                            break
                        if magic_index:
                            del buffer[:magic_index]
                        if len(buffer) < PACKET_SIZE:
                            break
                        packet = bytes(buffer[:PACKET_SIZE])
                        try:
                            decoded = decode_serial_packet(packet)
                        except ValueError:
                            del buffer[0]
                            continue
                        del buffer[:PACKET_SIZE]
                        try:
                            result = forward_to_cloud(
                                serial_payload(decoded, args.serial_port),
                                args.cloud, args.vehicle_id, args.token,
                            )
                            print("[{}] SEQ={} CAN_RX={} 已上传 accepted={}".format(
                                time.strftime("%H:%M:%S"), decoded["packetSequence"],
                                decoded["canRxCount"], result.get("accepted"),
                            ))
                        except (error.URLError, OSError, ValueError) as exc:
                            print("[{}] 云端上传失败：{}".format(time.strftime("%H:%M:%S"), exc))
        except (serial.SerialException, OSError) as exc:
            print("[{}] 串口不可用：{}；{} 秒后重连".format(
                time.strftime("%H:%M:%S"), exc, args.reconnect_delay
            ))
            time.sleep(args.reconnect_delay)


def run_udp(args):
    state = {"source": "S32K344-CAN"}
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_socket.bind((args.host, args.port))
    udp_socket.settimeout(0.2)
    board_control_address = (args.board_host, args.control_port)
    upload_queue = queue.Queue(maxsize=UPLOAD_QUEUE_SIZE)
    threading.Thread(
        target=run_udp_uploader,
        args=(args, upload_queue),
        name="body-cloud-uploader",
        daemon=True,
    ).start()
    threading.Thread(
        target=run_udp_maintenance,
        args=(args, udp_socket, board_control_address),
        name="body-edge-maintenance",
        daemon=True,
    ).start()
    print("车身域 UDP 数据适配器已启动：{}:{}".format(args.host, args.port))
    print("只接收开发板：{}；控制接口：{}:{}".format(
        args.board_host, args.board_host, args.control_port
    ))
    print("云端接口：{}/api/vehicles/{}/domains/body".format(args.cloud, args.vehicle_id))

    while True:
        try:
            data, address = udp_socket.recvfrom(8192)
            received_at_ms = now_ms()
        except socket.timeout:
            continue
        except OSError as exc:
            print("[{}] UDP 接收失败：{}".format(time.strftime("%H:%M:%S"), exc))
            time.sleep(1)
            continue

        if address[0] != args.board_host:
            print("[{}] 已忽略非目标板端数据：{}:{}".format(
                time.strftime("%H:%M:%S"), address[0], address[1]
            ))
            continue

        try:
            if len(data) in (PACKET_SIZE, TIMESTAMPED_PACKET_SIZE) and data[:2] == PACKET_MAGIC:
                decoded = decode_serial_packet(data)
                payload = ethernet_payload(decoded, address, received_at_ms)
                dropped = enqueue_latest(upload_queue, (
                    payload,
                    "UDP SEQ={} CAN_RX={}".format(
                        decoded["packetSequence"], decoded["canRxCount"]
                    ),
                ))
                if dropped:
                    print("[{}] 公网上传较慢，已丢弃一条过期报文".format(
                        time.strftime("%H:%M:%S")
                    ))
            else:
                update = normalize_update(json.loads(data.decode("utf-8")))
                if not update:
                    raise ValueError("payload does not contain supported body signals")
                state.update(update)
                state.setdefault("status", "online")
                state["captured_at_ms"] = received_at_ms
                enqueue_latest(upload_queue, (
                    dict(state),
                    "{}:{} 已转发 {} 个信号".format(
                        address[0], address[1], len(update)
                    ),
                ))
        except (UnicodeDecodeError, ValueError, error.URLError, OSError) as exc:
            print("[{}] 数据处理失败：{}".format(time.strftime("%H:%M:%S"), exc))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="V-SHIELD 车身域 Jetson 边缘代理")
    parser.add_argument("--mode", choices=("serial", "udp"), default=os.getenv("BODY_INPUT_MODE", "udp"))
    parser.add_argument("--host", default=os.getenv("BODY_UDP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("BODY_UDP_PORT", "7100")))
    parser.add_argument("--board-host", default=os.getenv("BODY_BOARD_HOST", "192.168.4.31"))
    parser.add_argument("--control-port", type=int, default=int(os.getenv("BODY_CONTROL_PORT", "7101")))
    parser.add_argument("--serial-port", default=os.getenv("BODY_SERIAL_PORT", "/dev/ttyUSB0"))
    parser.add_argument("--baudrate", type=int, default=int(os.getenv("BODY_SERIAL_BAUDRATE", "115200")))
    parser.add_argument("--reconnect-delay", type=float, default=2.0)
    parser.add_argument("--cloud", default=os.getenv("CLOUD_HTTP_URL", "").rstrip("/"))
    parser.add_argument("--vehicle-id", default=os.getenv("VEHICLE_ID", "VHC-001"))
    args = parser.parse_args(argv)
    args.token = os.getenv("VEHICLE_INGEST_TOKEN", "").strip()
    if not args.cloud:
        parser.error("请设置 CLOUD_HTTP_URL")
    if not args.token:
        parser.error("请设置 VEHICLE_INGEST_TOKEN")
    return args


def main():
    args = parse_args()
    try:
        if args.mode == "serial":
            run_serial(args)
        else:
            run_udp(args)
    except KeyboardInterrupt:
        print("\n车身域数据适配器已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
