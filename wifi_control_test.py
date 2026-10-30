"""Ethernet-side PAUSE/RESUME test. No third-party packages required."""

import argparse
import socket
import time
import uuid


def execute(sock, board, action, timeout=2.0, attempts=3):
    command_id = uuid.uuid4().hex[:16]
    command = "CTRL,ID={},ACTION={}\n".format(command_id, action).encode("ascii")
    expected = "ACK,ID={},ACTION={},STATUS=OK".format(command_id, action)
    for attempt in range(attempts):
        sock.sendto(command, board)
        print("{} -> {}:{} (attempt {})".format(action, board[0], board[1], attempt + 1), flush=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            if addr[0] != board[0]:
                continue
            if expected in data.decode("ascii", errors="replace").strip().splitlines():
                print("CONFIRMED by sender: {} (ID={})".format(action, command_id), flush=True)
                return True
    print("NO CONFIRMATION. Check board counters, PTC7 -> GPIO16, sender WiFi IP and firewall.")
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind-host", default="192.168.4.100", help="Ethernet IP of this computer/Jetson")
    parser.add_argument("--listen-port", type=int, default=6001)
    parser.add_argument("--board", default="192.168.4.201")
    parser.add_argument("--port", type=int, default=6002)
    parser.add_argument("--action", choices=["PAUSE", "RESUME"], type=str.upper)
    args = parser.parse_args()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind((args.bind_host, args.listen_port))
        except OSError as exc:
            parser.exit(1, "Cannot listen: {}. Stop WiFi_Module.py during this test and check Ethernet IP.\n".format(exc))
        print("1: PAUSE   2: RESUME   q: quit (does not resume automatically)")
        print("ACK listener {}:{}".format(args.bind_host, args.listen_port))
        if args.action:
            return 0 if execute(sock, (args.board, args.port), args.action) else 1
        try:
            while True:
                choice = input("Choose 1 / 2 / q: ").strip().lower()
                if choice == "q":
                    break
                if choice in ("1", "2"):
                    # Discard old telemetry before issuing a new command.
                    sock.setblocking(False)
                    try:
                        while True:
                            sock.recvfrom(4096)
                    except BlockingIOError:
                        pass
                    sock.setblocking(True)
                    execute(sock, (args.board, args.port), "PAUSE" if choice == "1" else "RESUME")
        except (KeyboardInterrupt, EOFError):
            print("\nControl test stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
