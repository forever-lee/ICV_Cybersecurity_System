#!/usr/bin/env python3
"""S32K344 车身域以太网适配器。

状态上行：S32K344 -> UDP/7100 -> Jetson -> HTTP -> Web
控制下行：Web -> HTTP -> Jetson -> UDP/7101 -> S32K344

UDP 载荷沿用已经验证的二进制协议：
- 状态包：16 字节，A5 5A 01 01 ... checksum
- 控制包：6 字节，A5 5A 01 02 command checksum
"""

import argparse
import os
import socket
import time
from urllib import error

from Body_Module_Board import (
    CLOUD_HTTP_URL,
    COMMAND_POLL_INTERVAL_SECONDS,
    INGEST_TOKEN,
    TIME_SYNC_INTERVAL_SECONDS,
    VEHICLE_ID,
    build_serial_command,
    build_time_sync_packet,
    body_can_frame_metadata,
    decode_serial_packet,
    forward_to_cloud,
    poll_transmission_command,
    serial_payload,
)


BODY_ETHERNET_HOST = os.getenv("BODY_ETHERNET_HOST", "0.0.0.0")
BODY_ETHERNET_STATUS_PORT = int(os.getenv("BODY_ETHERNET_STATUS_PORT", "7100"))
BODY_ETHERNET_BOARD_IP = os.getenv("BODY_ETHERNET_BOARD_IP", "192.168.4.31")
BODY_ETHERNET_CONTROL_PORT = int(os.getenv("BODY_ETHERNET_CONTROL_PORT", "7101"))


def ethernet_payload(packet, board_address, udp_received_at_ms=None):
    """Decode one board UDP datagram and map it to the cloud body schema."""
    if udp_received_at_ms is None:
        udp_received_at_ms = int(time.time() * 1000)
    udp_received_at_ms = int(udp_received_at_ms)
    decoded = decode_serial_packet(packet)
    board_sent_at_ms = decoded.get("boardSentAtMs")
    timestamp_synchronized = isinstance(board_sent_at_ms, int) and board_sent_at_ms > 0
    payload = serial_payload(
        decoded,
        "UDP/{}:{}".format(*board_address),
        board_sent_at_ms if timestamp_synchronized else udp_received_at_ms,
    )
    payload["source"] = "S32K344-Ethernet-UDP"
    payload["interface_path"] = (
        "S32K344/{}:{} -> Ethernet/UDP -> Jetson -> HTTP".format(
            board_address[0], board_address[1]
        )
    )
    payload["link"]["transport"] = "Ethernet/IPv4/UDP"
    payload["link"]["board_ip"] = board_address[0]
    payload["link"]["udp_received_at_ms"] = udp_received_at_ms
    payload["link"]["edge_received_at_ms"] = udp_received_at_ms
    payload["link"]["board_sent_at_ms"] = board_sent_at_ms
    payload["link"]["timestamp_synchronized"] = timestamp_synchronized
    payload["link"]["latency_scope"] = (
        "board_udp_send_to_cloud_ingest"
        if timestamp_synchronized
        else "jetson_udp_receive_to_cloud_ingest"
    )
    payload["raw_frame"] = body_can_frame_metadata(decoded)
    return decoded, payload


def run(args):
    status_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    status_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    status_socket.bind((args.host, args.status_port))
    status_socket.settimeout(0.2)

    control_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    board_control_address = (args.board_ip, args.control_port)

    last_command_sequence = 0
    acknowledged_sequence = 0
    next_command_poll = 0.0
    next_time_sync = 0.0

    print(
        "车身域以太网适配器已启动：{}:{}，允许板端 {}".format(
            args.host, args.status_port, args.board_ip or "任意地址"
        )
    )
    print("板端控制地址：{}:{}".format(*board_control_address))
    print("云端接口：{}/api/vehicles/{}/domains/body".format(args.cloud, args.vehicle_id))
    print("按 Ctrl+C 停止")

    while True:
        current_time = time.monotonic()
        if current_time >= next_time_sync:
            try:
                control_socket.sendto(
                    build_time_sync_packet(), board_control_address
                )
            except OSError as exc:
                print("[{}] 板端时钟校准失败：{}".format(time.strftime("%H:%M:%S"), exc))
            next_time_sync = current_time + TIME_SYNC_INTERVAL_SECONDS

        if current_time >= next_command_poll:
            try:
                command_result = poll_transmission_command(
                    args.cloud,
                    args.vehicle_id,
                    args.token,
                    last_command_sequence,
                    acknowledged_sequence,
                )
                acknowledged_sequence = 0
                if command_result.get("pending"):
                    command = command_result.get("command") or {}
                    sequence = int(command.get("sequence") or 0)
                    action = str(command.get("action") or "")
                    if sequence > last_command_sequence and action in {"start", "stop"}:
                        packet = build_serial_command(action)
                        control_socket.sendto(packet, board_control_address)
                        last_command_sequence = sequence
                        acknowledged_sequence = sequence
                        print(
                            "[{}] UDP 已下发{}命令 SEQ={} -> {}:{}".format(
                                time.strftime("%H:%M:%S"),
                                "启动发送" if action == "start" else "停止发送",
                                sequence,
                                *board_control_address
                            )
                        )
            except (error.URLError, OSError, ValueError) as exc:
                print("[{}] 控制命令轮询失败：{}".format(time.strftime("%H:%M:%S"), exc))
            next_command_poll = current_time + COMMAND_POLL_INTERVAL_SECONDS

        try:
            packet, board_address = status_socket.recvfrom(2048)
            udp_received_at_ms = int(time.time() * 1000)
        except socket.timeout:
            continue
        except OSError as exc:
            print("[{}] UDP 接收失败：{}".format(time.strftime("%H:%M:%S"), exc))
            time.sleep(1.0)
            continue

        if args.board_ip and board_address[0] != args.board_ip:
            print(
                "[{}] 已忽略非目标板端数据：{}:{}".format(
                    time.strftime("%H:%M:%S"), *board_address
                )
            )
            continue

        try:
            decoded, payload = ethernet_payload(
                packet, board_address, udp_received_at_ms
            )
            result = forward_to_cloud(payload, args.cloud, args.vehicle_id, args.token)
            print(
                "[{}] UDP SEQ={} CAN_RX={} 来源={}:{} 已上传 accepted={}".format(
                    time.strftime("%H:%M:%S"),
                    decoded["packetSequence"],
                    decoded["canRxCount"],
                    board_address[0],
                    board_address[1],
                    result.get("accepted"),
                )
            )
        except (ValueError, error.URLError, OSError) as exc:
            print("[{}] 以太网数据处理失败：{}".format(time.strftime("%H:%M:%S"), exc))


def parse_args():
    parser = argparse.ArgumentParser(description="V-SHIELD 车身域以太网适配器")
    parser.add_argument("--host", default=BODY_ETHERNET_HOST)
    parser.add_argument("--status-port", type=int, default=BODY_ETHERNET_STATUS_PORT)
    parser.add_argument("--board-ip", default=BODY_ETHERNET_BOARD_IP)
    parser.add_argument("--control-port", type=int, default=BODY_ETHERNET_CONTROL_PORT)
    parser.add_argument("--cloud", default=CLOUD_HTTP_URL)
    parser.add_argument("--vehicle-id", default=VEHICLE_ID)
    parser.add_argument("--token", default=INGEST_TOKEN)
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        print("\n车身域以太网适配器已停止")


if __name__ == "__main__":
    main()
