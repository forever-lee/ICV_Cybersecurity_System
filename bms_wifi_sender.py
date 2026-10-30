"""Automatic Computer A BMS sender for the S32K344 WiFi-to-Ethernet demo."""

import argparse
import os
import random
import re
import select
import socket
import struct
import subprocess
import time
from collections import OrderedDict


ESP_HOST = "192.168.3.1"
ESP_PORT = 8080
ROUND_SECONDS = 120
START_SOC = 20.0


class SenderControl:
    """Listen even while paused; only elapsed running time advances the demo."""

    def __init__(self, sock, host, port):
        self.sock = sock
        self.peer = (socket.gethostbyname(host), port)
        self.paused = False
        self.buffer = b""
        self.seen = OrderedDict()

    def receive(self, timeout):
        readable, _, _ = select.select([self.sock], [], [], timeout)
        if not readable:
            return
        data, addr = self.sock.recvfrom(4096)
        if addr != self.peer:
            return
        self.buffer += data
        while b"\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n", 1)
            text = raw.rstrip(b"\r").decode("ascii", errors="replace")
            match = re.fullmatch(r"CTRL,ID=([A-Za-z0-9_-]{1,40}),ACTION=(PAUSE|RESUME)", text)
            if not match:
                continue
            command_id, action = match.groups()
            if command_id in self.seen:
                if self.seen[command_id] != action:
                    continue
            else:
                self.paused = action == "PAUSE"
                self.seen[command_id] = action
                if len(self.seen) > 128:
                    self.seen.popitem(last=False)
                print("[CONTROL] {}: {}".format(command_id,
                      "PAUSED (waiting for RESUME)" if self.paused else "RUNNING"), flush=True)
            ack = "ACK,ID={},ACTION={},STATUS=OK\n".format(command_id, action)
            # ACK follows the original WiFi -> UART -> Ethernet uplink.
            self.sock.sendto(ack.encode("ascii"), self.peer)
        if len(self.buffer) > 128:
            self.buffer = b""

    def wait(self, seconds):
        remaining = seconds
        while remaining > 0 or self.paused:
            was_running = not self.paused
            started = time.monotonic()
            self.receive(min(0.1, max(0, remaining)) if was_running else 0.1)
            if was_running:
                remaining = max(0, remaining - (time.monotonic() - started))
        # Drain a command already queued before sending the next report.
        self.receive(0)
        while self.paused:
            self.receive(0.1)


def timestamp_ms():
    return int(time.time() * 1000)


def ping_gateway(host):
    if os.name == "nt":
        command = ["ping", "-n", "1", "-w", "1000", host]
    else:
        command = ["ping", "-c", "1", "-W", "1", host]
    return subprocess.run(command, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def route_local_ip(host):
    """Read the selected local interface without transmitting application data."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((host, ESP_PORT))
        return probe.getsockname()[0]


def wait_for_wifi(host, local_ip=None):
    print("checking S32K344_WIFI gateway {} ...".format(host))
    while True:
        try:
            current_ip = route_local_ip(host)
        except OSError:
            current_ip = "unavailable"
        if local_ip and current_ip != local_ip:
            print("[WAIT] Current route IP: {}; required WiFi IP: {}. Waiting for hotspot/IP settings...".format(
                current_ip, local_ip), flush=True)
        elif ping_gateway(host):
            print("WiFi hotspot connected. Local IP: {}".format(current_ip), flush=True)
            return
        else:
            print("[WAIT] ESP32 {} is not reachable; retrying...".format(host), flush=True)
        time.sleep(2)


def open_sender_socket(host, bind_host, control_port):
    while True:
        wait_for_wifi(host, bind_host)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((bind_host, control_port))
            return sock
        except OSError as exc:
            sock.close()
            if getattr(exc, "winerror", None) == 10049 or exc.errno == 99:
                print("[WAIT] WiFi address changed during startup; checking again...", flush=True)
                time.sleep(2)
                continue
            raise


def countdown(seconds, control=None):
    for remaining in range(seconds, 0, -1):
        print("BMS simulation starts in {}...".format(remaining))
        control.wait(1) if control else time.sleep(1)


def send_report(sock, host, port, text, attack):
    # The S32K344 uses CR/LF as one report boundary.
    payload = (text.rstrip("\r\n") + "\n").encode("utf-8")
    sock.sendto(payload, (host, port))
    print("{}  {}".format("[ATTACK]" if attack else "[NORMAL]", text))


def build_report(kind, sequence, soc, state):
    # Demonstration envelope: a 0x74-style payload carried as hex over UART/UDP.
    # SEQ, STATE and TS are application metadata, not GB/T CAN fields.
    soc_raw = max(0, min(1000, int(round(soc * 10))))
    remaining = max(0, (ROUND_SECONDS - sequence + 59) // 60)
    data = struct.pack("<BHH", 0x74, soc_raw, remaining).hex().upper()
    return "GBT74,{},SEQ={:04d},DATA={},STATE={},TS={}".format(
        kind, sequence, data, state, timestamp_ms()
    )


def attack_sequences():
    """Choose 1-3 separated positions during one 120-second charge round."""
    count = random.randint(1, 3)
    if count == 1:
        return {random.randint(15, 95)}
    if count == 2:
        return {random.randint(15, 35), random.randint(75, 105)}
    return {random.randint(15, 35), random.randint(50, 70), random.randint(85, 105)}


def tampered_soc(true_soc):
    magnitude = random.uniform(15.0, 25.0)
    if true_soc + magnitude <= 100.0:
        return true_soc + magnitude
    return true_soc - magnitude


def run_round(sock, host, port, round_number, interval, control=None):
    attacks = attack_sequences()
    step = (100.0 - START_SOC) / ROUND_SECONDS
    print("\n=== charging round {} | IDS attack sequences: {} ===".format(
        round_number, ", ".join("{:04d}".format(item) for item in sorted(attacks))))

    send_report(sock, host, port,
                build_report("START", 0, START_SOC, "CHARGING"), False)

    for sequence in range(1, ROUND_SECONDS + 1):
        control.wait(interval) if control else time.sleep(interval)
        true_soc = min(100.0, START_SOC + (step * sequence))
        is_attack = sequence in attacks
        transmitted_soc = tampered_soc(true_soc) if is_attack else true_soc
        state = "FULL" if sequence == ROUND_SECONDS else "CHARGING"
        send_report(sock, host, port,
                    build_report("DATA", sequence, transmitted_soc, state),
                    is_attack)


def main():
    parser = argparse.ArgumentParser(description="automatic Computer A BMS simulator")
    parser.add_argument("--host", default=ESP_HOST, help="ESP32 hotspot IP")
    parser.add_argument("--port", type=int, default=ESP_PORT, help="ESP32 UDP port")
    parser.add_argument("--bind-host", default="192.168.3.2",
                        help="Computer A WiFi IP; must match ESP32 saved remote host")
    parser.add_argument("--control-port", type=int, default=6001,
                        help="UDP receive port; must match ESP32 saved remote port")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between reports; default is 1")
    parser.add_argument("--rounds", type=int, default=0,
                        help="number of rounds; 0 means run continuously")
    args = parser.parse_args()

    sock = None
    round_number = 1
    try:
        sock = open_sender_socket(args.host, args.bind_host, args.control_port)
        control = SenderControl(sock, args.host, args.port)
        print("Control listener {}:{}; keep this window open while paused.".format(
            args.bind_host, args.control_port), flush=True)
        countdown(3, control)
        while args.rounds == 0 or round_number <= args.rounds:
            run_round(sock, args.host, args.port, round_number,
                      max(0.05, args.interval), control)
            round_number += 1
            if args.rounds == 0 or round_number <= args.rounds:
                print("round complete; a new round starts in 3 seconds.")
                countdown(3, control)
    except OSError as exc:
        print("Network error: {}. Check WiFi IP {} and port {}; close any other sender.".format(
            exc, args.bind_host, args.control_port))
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\nBMS simulation stopped.")
    finally:
        if sock is not None:
            sock.close()


if __name__ == "__main__":
    main()
