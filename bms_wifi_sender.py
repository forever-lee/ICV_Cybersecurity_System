"""Computer A BMS report sender for the S32K344 WiFi-to-Ethernet gateway.

Usage examples:
    python bms_wifi_sender.py --demo
    python bms_wifi_sender.py --text "CHG,DATA,SEQ=8,SOC=92.0,STATE=CHARGING"
"""

import argparse
import socket
import time


ESP_HOST = "192.168.3.1"
ESP_PORT = 8080


def timestamp_ms():
    return int(time.time() * 1000)


def send_report(sock, host, port, text):
    # The MCU uses CR/LF as the report boundary; do not omit it.
    payload = (text.rstrip("\r\n") + "\n").encode("utf-8")
    sock.sendto(payload, (host, port))
    print("sent {} bytes -> {}:{}  {}".format(len(payload), host, port, text))


def build_report(kind, sequence, soc, state):
    return "CHG,{},SEQ={:04d},SOC={:.1f},STATE={},TS={}".format(
        kind, sequence, soc, state, timestamp_ms()
    )


def run_demo(sock, host, port, count, interval):
    soc = 20.0
    send_report(sock, host, port, build_report("START", 0, soc, "CHARGING"))
    for sequence in range(1, count + 1):
        time.sleep(interval)
        soc = min(100.0, soc + 0.7)
        state = "FULL" if soc >= 100.0 else "CHARGING"
        send_report(sock, host, port, build_report("DATA", sequence, soc, state))


def main():
    parser = argparse.ArgumentParser(description="Computer A BMS WiFi sender")
    parser.add_argument("--host", default=ESP_HOST, help="ESP32 hotspot IP")
    parser.add_argument("--port", type=int, default=ESP_PORT, help="ESP32 UDP port")
    parser.add_argument("--text", help="send one custom report exactly as supplied")
    parser.add_argument("--demo", action="store_true", help="send START and normal DATA reports")
    parser.add_argument("--count", type=int, default=10, help="DATA report count for --demo")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between demo reports")
    args = parser.parse_args()

    if not args.text and not args.demo:
        parser.error("choose --demo or --text")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if args.text:
            send_report(sock, args.host, args.port, args.text)
        else:
            run_demo(sock, args.host, args.port, max(1, args.count), max(0.05, args.interval))
    finally:
        sock.close()


if __name__ == "__main__":
    main()
