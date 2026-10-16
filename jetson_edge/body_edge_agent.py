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
        CONTROL_ACK_PACKET_SIZE,
        TRANSMISSION_ACK_PACKET_SIZE,
        build_body_event_command,
        build_serial_command,
        build_transmission_command,
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
        CONTROL_ACK_PACKET_SIZE,
        TRANSMISSION_ACK_PACKET_SIZE,
        build_body_event_command,
        build_serial_command,
        build_transmission_command,
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
    cloud_url, vehicle_id, token, last_sequence, dispatched_sequence=0,
    last_event_sequence=0, dispatched_event_sequence=0
):
    url = "{}/api/vehicles/{}/domains/body/control".format(
        cloud_url.rstrip("/"), vehicle_id
    )
    body = json.dumps(
        {
            "last_sequence": int(last_sequence),
            "dispatched_sequence": int(dispatched_sequence),
            "last_event_sequence": int(last_event_sequence),
            "dispatched_event_sequence": int(dispatched_event_sequence),
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
    result, last_sequence, send_packet, sequenced=True
):
    if not result.get("pending"):
        return last_sequence, 0
    command = result.get("command") or {}
    sequence = int(command.get("sequence") or 0)
    action = str(command.get("action") or "")
    if sequence <= last_sequence or action not in {"start", "stop"}:
        return last_sequence, 0
    send_packet(
        build_transmission_command(sequence, action)
        if sequenced else build_serial_command(action)
    )
    print(
        "[{}] 已向开发板下发{}命令 SEQ={}".format(
            time.strftime("%H:%M:%S"),
            "启动发送" if action == "start" else "停止发送",
            sequence,
        )
    )
    return sequence, 0 if sequenced else sequence


def service_transmission_command(result, runtime, send_packet, args, current_time=None):
    """Retry one idempotent K3 command until a real board ACK or timeout."""
    if current_time is None:
        current_time = time.monotonic()
    command = result.get("command") or {}
    if not result.get("pending"):
        with runtime["lock"]:
            runtime["pending"] = None
        return 0
    sequence = int(command.get("sequence") or 0)
    action = str(command.get("action") or "")
    if not 0 < sequence <= 0xFFFFFFFF or action not in {"start", "stop"}:
        raise ValueError("invalid transmission command from cloud")

    with runtime["lock"]:
        pending = runtime.get("pending")
        if not pending or pending["sequence"] != sequence:
            pending = {
                "sequence": sequence,
                "action": action,
                "first_seen": current_time,
                "last_sent": None,
                "attempts": 0,
                "confirmed": False,
            }
            runtime["pending"] = pending
        elapsed = current_time - pending["first_seen"]
        due = (
            pending["last_sent"] is None
            or current_time - pending["last_sent"] >= args.control_retry_interval
        )
        if elapsed >= args.control_timeout or pending["attempts"] >= args.control_max_attempts:
            return 0
        if not due:
            return 0
        packet = build_transmission_command(sequence, action)
        send_packet(packet)
        pending["last_sent"] = current_time
        pending["attempts"] += 1
        attempt = pending["attempts"]

    print("[{}] 已向K3下发{}命令 SEQ={}（第{}/{}次）".format(
        time.strftime("%H:%M:%S"),
        "启动发送" if action == "start" else "停止发送",
        sequence, attempt, args.control_max_attempts,
    ))
    return sequence


def validate_transmission_ack(decoded, runtime):
    """Accept only an ACK whose sequence/action/result matches real K3 state."""
    ack = decoded.get("transmissionAck")
    if not isinstance(ack, dict) or int(ack.get("sequence") or 0) <= 0:
        return None
    action = ack.get("action")
    action_code = ack.get("action_code")
    result = ack.get("result")
    actual_enabled = bool(decoded.get("transmissionEnabled"))
    state_matches = actual_enabled == (action == "start")
    if action not in {"start", "stop"} or action_code not in {0, 1}:
        return None
    if result in {"applied", "duplicate"} and not state_matches:
        return None
    if result not in {"applied", "duplicate", "error"}:
        return None

    with runtime["lock"]:
        pending = runtime.get("pending")
        if pending and pending["sequence"] == ack["sequence"]:
            if pending["action"] != action:
                return None
            pending["confirmed"] = True
    return ack


def process_pending_body_event(result, last_sequence, send_packet):
    if not result.get("event_pending"):
        return last_sequence, 0
    command = result.get("event_command") or {}
    sequence = int(command.get("sequence") or 0)
    action = str(command.get("action") or "")
    # Retransmit the same idempotent command until the cloud observes K3's
    # CAN0 readback.  This tolerates a lost UDP command or status packet.
    if sequence < last_sequence:
        return last_sequence, 0
    packet = build_body_event_command(
        sequence,
        action,
        command.get("target"),
        command.get("hold_seconds", 5),
    )
    send_packet(packet)
    print("[{}] 已向K3下发车身事件 {}={} SEQ={}".format(
        time.strftime("%H:%M:%S"), action,
        1 if command.get("target") else 0, sequence,
    ))
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


def run_udp_maintenance(args, udp_socket, board_control_address, control_runtime):
    """Keep cloud control polling and board time sync off the receive path."""
    last_command_sequence = 0
    dispatched_sequence = 0
    next_command_poll = 0.0
    next_time_sync = 0.0
    last_event_sequence = 0
    dispatched_event_sequence = 0
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
                    last_command_sequence, dispatched_sequence,
                    last_event_sequence, dispatched_event_sequence,
                )
                dispatched_sequence = 0
                dispatched_event_sequence = 0
                dispatched = service_transmission_command(
                    result, control_runtime,
                    lambda packet: udp_socket.sendto(packet, board_control_address),
                    args, current_time,
                )
                if dispatched:
                    last_command_sequence = max(last_command_sequence, dispatched)
                    dispatched_sequence = dispatched
                last_event_sequence, dispatched_event_sequence = process_pending_body_event(
                    result, last_event_sequence,
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
                                sequenced=False,
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
    control_runtime = {"lock": threading.Lock(), "pending": None}
    threading.Thread(
        target=run_udp_uploader,
        args=(args, upload_queue),
        name="body-cloud-uploader",
        daemon=True,
    ).start()
    threading.Thread(
        target=run_udp_maintenance,
        args=(args, udp_socket, board_control_address, control_runtime),
        name="body-edge-maintenance",
        daemon=True,
    ).start()
    print("车身域 UDP 数据适配器已启动：{}:{}".format(args.host, args.port))
    print(
        "[BODY LINK] S32K344 {} -> cable/UDP -> switch -> Jetson {} {}:{}".format(
            args.board_host, args.network_interface, args.jetson_address, args.port
        )
    )
    print(
        "[BODY CONTROL] Jetson {} {} -> UDP -> S32K344 {}:{}".format(
            args.network_interface, args.jetson_address,
            args.board_host, args.control_port,
        )
    )
    print("只接收开发板：{}；控制接口：{}:{}".format(
        args.board_host, args.board_host, args.control_port
    ))
    print("云端接口：{}/api/vehicles/{}/domains/body".format(args.cloud, args.vehicle_id))

    started_at = time.monotonic()
    last_packet_at = None
    next_diagnostic_at = started_at + args.diagnostic_interval
    packet_count = 0

    while True:
        try:
            data, address = udp_socket.recvfrom(8192)
            received_at_ms = now_ms()
        except socket.timeout:
            current_time = time.monotonic()
            if current_time >= next_diagnostic_at:
                if last_packet_at is None:
                    print(
                        "[BODY RX WAIT] {}s内未收到UDP报文；检查 {} -> {} {}:{}".format(
                            int(current_time - started_at), args.board_host,
                            args.network_interface, args.jetson_address, args.port,
                        )
                    )
                else:
                    print(
                        "[BODY RX WAIT] {}s内无新报文；累计收到 {} 个UDP报文".format(
                            int(current_time - last_packet_at), packet_count
                        )
                    )
                next_diagnostic_at = current_time + args.diagnostic_interval
            continue
        except OSError as exc:
            print("[{}] UDP 接收失败：{}".format(time.strftime("%H:%M:%S"), exc))
            time.sleep(1)
            continue

        packet_count += 1
        last_packet_at = time.monotonic()
        next_diagnostic_at = last_packet_at + args.diagnostic_interval
        print(
            "[BODY RX OK] packet={} source={}:{} bytes={} received_at={}".format(
                packet_count, address[0], address[1], len(data),
                time.strftime("%H:%M:%S"),
            )
        )

        if address[0] != args.board_host:
            print("[BODY RX WARN] 已忽略非目标板端数据：{}:{}；期望 {}".format(
                address[0], address[1], args.board_host
            ))
            continue

        try:
            if len(data) in (
                PACKET_SIZE, TIMESTAMPED_PACKET_SIZE, CONTROL_ACK_PACKET_SIZE,
                TRANSMISSION_ACK_PACKET_SIZE,
            ) and data[:2] == PACKET_MAGIC:
                decoded = decode_serial_packet(data)
                if decoded.get("transmissionAck") and not validate_transmission_ack(
                    decoded, control_runtime
                ):
                    print("[{}] 已忽略不一致的K3发送控制确认".format(
                        time.strftime("%H:%M:%S")
                    ))
                    decoded.pop("transmissionAck", None)
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
    parser.add_argument("--control-timeout", type=float, default=float(os.getenv("BODY_CONTROL_TIMEOUT", "10")))
    parser.add_argument("--control-retry-interval", type=float, default=float(os.getenv("BODY_CONTROL_RETRY_INTERVAL", "2")))
    parser.add_argument("--control-max-attempts", type=int, default=int(os.getenv("BODY_CONTROL_MAX_ATTEMPTS", "3")))
    parser.add_argument("--network-interface", default=os.getenv("BODY_NETWORK_INTERFACE", "eno1"))
    parser.add_argument("--jetson-address", default=os.getenv("BODY_JETSON_ADDRESS", "192.168.4.100"))
    parser.add_argument("--diagnostic-interval", type=float, default=float(os.getenv("BODY_DIAGNOSTIC_INTERVAL_SECONDS", "5")))
    parser.add_argument("--cloud", default=os.getenv("CLOUD_HTTP_URL", "").rstrip("/"))
    parser.add_argument("--vehicle-id", default=os.getenv("VEHICLE_ID", "VHC-001"))
    args = parser.parse_args(argv)
    args.token = os.getenv("VEHICLE_INGEST_TOKEN", "").strip()
    if not args.cloud:
        parser.error("请设置 CLOUD_HTTP_URL")
    if not args.token:
        parser.error("请设置 VEHICLE_INGEST_TOKEN")
    args.diagnostic_interval = max(1.0, args.diagnostic_interval)
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
