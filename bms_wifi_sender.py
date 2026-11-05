"""B sender: real board downlink, bounded manual attacks and pause-safe clock."""
import argparse
import os
import random
import select
import socket
import struct
import subprocess
import time
from collections import OrderedDict
from jetson_edge.wifi_control_protocol import parse_command

ESP_HOST, ESP_PORT = "192.168.3.1", 8080
ROUND_SECONDS, START_SOC = 120, 20.0


def timestamp_ms():
    return int(time.time() * 1000)


class SenderControl:
    def __init__(self, sock, host, port, clock=None, monotonic=None):
        self.sock = sock
        self.peer = (socket.gethostbyname(host), port)
        self.clock = clock or timestamp_ms
        self.monotonic = monotonic or time.monotonic
        self.paused = False
        self.buffer = b""
        self.seen = OrderedDict()
        self.highest_order = self.last_order = 0
        self.mode = "NONE"
        self.attack_at = self.attack_until = 0
        self.active_time = 0.0
        self.last_tick = self.monotonic()

    def attack_mode(self):
        if self.mode == "FLOOD" and self.active_time >= self.attack_until:
            self.mode = "NONE"
        return self.mode

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
            cmd = parse_command(raw.rstrip(b"\r").decode("ascii", errors="replace"))
            if not cmd:
                continue
            action, ident, order = cmd["command"], cmd["id"], cmd["order"]
            signature = (action, order, cmd["expires_at_ms"])
            result = "OK"
            if ident in self.seen:
                if self.seen[ident] != signature:
                    continue
                # Duplicate is acknowledged with CURRENT state, never reapplied.
            elif cmd["expires_at_ms"] <= self.clock():
                result = "EXPIRED"
            elif order <= self.highest_order:
                result = "STALE"
            else:
                self.highest_order = order
                self.seen[ident] = signature
                if len(self.seen) > 256:
                    self.seen.popitem(last=False)
                if action != "status":
                    self.last_order = order
                if action == "pause":
                    self.paused = True
                    self.mode = "NONE"  # pause cancels scheduled/active attacks
                elif action == "resume":
                    self.paused = False
                elif action == "stop":
                    self.mode = "NONE"
                elif action in {"soc", "flood"}:
                    if not self.paused:
                        self.mode = action.upper()
                        self.attack_at = self.active_time + 2.0
                        self.attack_until = self.attack_at + 10.0
                print("[CONTROL] {} {} state={} mode={}".format(ident, action, self.paused, self.mode), flush=True)
            ack = "ACK,ID={},ACTION={},STATUS={},STATE={},MODE={},LAST={}\n".format(
                ident, action.upper(), result, "PAUSED" if self.paused else "RUNNING", self.attack_mode(), self.last_order)
            self.sock.sendto(ack.encode("ascii"), self.peer)
        if len(self.buffer) >= 128:
            self.buffer = b""

    def tick(self, timeout=0.02):
        running = not self.paused
        self.receive(timeout)
        now = self.monotonic()
        if running:
            self.active_time += max(0, now - self.last_tick)
        self.last_tick = now

    def wait(self, seconds):
        end = self.active_time + seconds
        while self.active_time < end or self.paused:
            self.tick(min(0.02, max(0, end - self.active_time)) if not self.paused else 0.02)
        self.receive(0)
        while self.paused:
            self.tick()


def ping_gateway(host):
    command = ["ping", "-n", "1", "-w", "1000", host] if os.name == "nt" else ["ping", "-c", "1", "-W", "1", host]
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def route_local_ip(host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((host, ESP_PORT))
        return probe.getsockname()[0]


def wait_for_wifi(host, local_ip=None):
    while True:
        try:
            current = route_local_ip(host)
        except OSError:
            current = "unavailable"
        if (not local_ip or current == local_ip) and ping_gateway(host):
            print("WiFi connected: {} -> {}".format(current, host), flush=True)
            return
        print("[WAIT] Local IP {}; required {}; ESP32 {}".format(current, local_ip, host), flush=True)
        time.sleep(2)


def open_sender_socket(host, bind_host, control_port):
    wait_for_wifi(host, bind_host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((bind_host, control_port))
        return sock
    except Exception:
        sock.close()
        raise


def countdown(seconds, control):
    for remaining in range(seconds, 0, -1):
        print("Starts in {}...".format(remaining), flush=True)
        control.wait(1)


def build_report(kind, sequence, soc, state, remaining_seconds=None):
    soc_raw = max(0, min(1000, int(round(soc * 10))))
    remaining = max(0, (ROUND_SECONDS - sequence + 59) // 60) if remaining_seconds is None else max(0, int((remaining_seconds + 59) // 60))
    data = struct.pack("<BHH", 0x74, soc_raw, remaining).hex().upper()
    return "GBT74,{},SEQ={:04d},DATA={},STATE={},TS={}".format(kind, sequence, data, state, timestamp_ms())


def tampered_soc(soc):
    magnitude = random.uniform(15, 25)
    return soc + magnitude if soc + magnitude <= 100 else soc - magnitude


def send_report(sock, host, port, text, attack=False):
    sock.sendto((text + "\n").encode("ascii"), (host, port))
    print("[{}] {}".format("ATTACK" if attack else "NORMAL", text), flush=True)


def run_round(sock, host, port, round_number, interval=1.0, control=None):
    control = control or SenderControl(sock, host, port)
    start = control.active_time
    sequence = 0
    next_send = start + interval
    print("=== B charging round {} (manual attacks only) ===".format(round_number), flush=True)
    send_report(sock, host, port, build_report("START", 0, START_SOC, "CHARGING"))
    while True:
        control.tick()
        if control.paused:
            continue
        mode = control.attack_mode()
        flooding = mode == "FLOOD" and control.active_time >= control.attack_at
        period = 0.1 if flooding else interval
        # On starting a flood, do not wait for the old one-second deadline.
        if control.active_time < min(next_send, start + ROUND_SECONDS) and not (flooding and next_send - control.active_time > period):
            continue
        elapsed = min(ROUND_SECONDS, control.active_time - start)
        sequence += 1
        soc = START_SOC + (100 - START_SOC) * elapsed / ROUND_SECONDS
        attack = mode == "SOC" and control.active_time >= control.attack_at
        if attack:
            soc = tampered_soc(soc)
            control.mode = "NONE"
        send_report(sock, host, port, build_report("DATA", sequence, soc, "FULL" if elapsed >= ROUND_SECONDS else "CHARGING", ROUND_SECONDS - elapsed), attack or flooding)
        next_send = control.active_time + period
        if elapsed >= ROUND_SECONDS:
            return


def main():
    parser = argparse.ArgumentParser(description="B charging demo; keep running for downlink replies")
    parser.add_argument("--host", default=ESP_HOST)
    parser.add_argument("--port", type=int, default=ESP_PORT)
    parser.add_argument("--bind-host", default="192.168.3.2")
    parser.add_argument("--control-port", type=int, default=6001)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--rounds", type=int, default=0)
    args = parser.parse_args()
    sock = None
    try:
        sock = open_sender_socket(args.host, args.bind_host, args.control_port)
        control = SenderControl(sock, args.host, args.port)
        countdown(3, control)
        number = 1
        while args.rounds == 0 or number <= args.rounds:
            run_round(sock, args.host, args.port, number, max(0.1, args.interval), control)
            number += 1
            if args.rounds == 0 or number <= args.rounds:
                countdown(3, control)
    except KeyboardInterrupt:
        print("B sender stopped")
    except OSError as exc:
        print("Network error: {}. Check WiFi IP and close other A/B senders.".format(exc), flush=True)
        raise SystemExit(1)
    finally:
        if sock:
            sock.close()


if __name__ == "__main__":
    main()
