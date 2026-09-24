"""Automatic Computer A BMS sender for the S32K344 WiFi-to-Ethernet demo."""

import argparse
import os
import random
import socket
import subprocess
import time


ESP_HOST = "192.168.3.1"
ESP_PORT = 8080
ROUND_SECONDS = 120
START_SOC = 20.0


def timestamp_ms():
    return int(time.time() * 1000)


def ping_gateway(host):
    if os.name == "nt":
        command = ["ping", "-n", "1", "-w", "1000", host]
    else:
        command = ["ping", "-c", "1", "-W", "1", host]
    return subprocess.run(command, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def wait_for_wifi(host):
    print("checking S32K344_WIFI gateway {} ...".format(host))
    while not ping_gateway(host):
        print("WiFi hotspot is not reachable; connect to S32K344_WIFI and retrying in 2 seconds...")
        time.sleep(2)
    print("WiFi hotspot connected.")


def countdown(seconds):
    for remaining in range(seconds, 0, -1):
        print("BMS simulation starts in {}...".format(remaining))
        time.sleep(1)


def send_report(sock, host, port, text, attack):
    # The S32K344 uses CR/LF as one report boundary.
    payload = (text.rstrip("\r\n") + "\n").encode("utf-8")
    sock.sendto(payload, (host, port))
    print("{}  {}".format("[ATTACK]" if attack else "[NORMAL]", text))


def build_report(kind, sequence, soc, state):
    return "CHG,{},SEQ={:04d},SOC={:.1f},STATE={},TS={}".format(
        kind, sequence, soc, state, timestamp_ms()
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


def run_round(sock, host, port, round_number, interval):
    attacks = attack_sequences()
    step = (100.0 - START_SOC) / ROUND_SECONDS
    print("\n=== charging round {} | IDS attack sequences: {} ===".format(
        round_number, ", ".join("{:04d}".format(item) for item in sorted(attacks))))

    send_report(sock, host, port,
                build_report("START", 0, START_SOC, "CHARGING"), False)

    for sequence in range(1, ROUND_SECONDS + 1):
        time.sleep(interval)
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
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between reports; default is 1")
    parser.add_argument("--rounds", type=int, default=0,
                        help="number of rounds; 0 means run continuously")
    args = parser.parse_args()

    wait_for_wifi(args.host)
    countdown(3)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    round_number = 1
    try:
        while args.rounds == 0 or round_number <= args.rounds:
            run_round(sock, args.host, args.port, round_number,
                      max(0.05, args.interval))
            round_number += 1
            if args.rounds == 0 or round_number <= args.rounds:
                print("round complete; a new round starts in 3 seconds.")
                countdown(3)
    except KeyboardInterrupt:
        print("\nBMS simulation stopped.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
