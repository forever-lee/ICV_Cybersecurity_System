"""Real UDP tests for pause/resume, framing, retries and the test tool."""
import socket
import threading
import unittest
from unittest.mock import patch

import bms_wifi_sender as sender
from wifi_control_test import execute


class DownlinkTests(unittest.TestCase):
    def test_startup_waits_for_correct_local_ip_despite_successful_ping(self):
        with patch.object(sender, "route_local_ip", side_effect=["192.168.3.3", "192.168.3.2"]), \
                patch.object(sender, "ping_gateway", return_value=True) as ping, \
                patch.object(sender.time, "sleep") as sleep:
            sender.wait_for_wifi("192.168.3.1", "192.168.3.2")
            self.assertEqual(ping.call_count, 1)
            sleep.assert_called_once_with(2)

    def setUp(self):
        self.esp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.pc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.esp.bind(("127.0.0.1", 0))
        self.pc.bind(("127.0.0.1", 0))
        self.esp.settimeout(1)
        self.control = sender.SenderControl(self.pc, *self.esp.getsockname())

    def tearDown(self):
        self.esp.close()
        self.pc.close()

    def command(self, text):
        self.esp.sendto(text.encode("ascii"), self.pc.getsockname())
        self.control.receive(0.1)

    def test_split_commands_duplicates_and_wrong_source(self):
        self.command("CTRL,ID=one,ACTION=PA")
        self.assertFalse(self.control.paused)
        self.command("USE\n")
        self.assertTrue(self.control.paused)
        self.assertIn(b"ACK,ID=one", self.esp.recv(1024))
        self.command("CTRL,ID=two,ACTION=RESUME\n")
        self.esp.recv(1024)
        self.assertFalse(self.control.paused)
        self.command("CTRL,ID=one,ACTION=PAUSE\n")
        self.esp.recv(1024)
        self.assertFalse(self.control.paused, "old retry must not reapply PAUSE")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stranger:
            stranger.sendto(b"CTRL,ID=bad,ACTION=PAUSE\n", self.pc.getsockname())
            self.control.receive(0.1)
        self.assertFalse(self.control.paused)

    def test_round_pauses_without_losing_sequence(self):
        host, port = self.esp.getsockname()
        with patch.object(sender, "ROUND_SECONDS", 4), patch.object(sender, "attack_sequences", return_value=set()):
            worker = threading.Thread(target=sender.run_round,
                                      args=(self.pc, host, port, 1, 0.15, self.control), daemon=True)
            worker.start()
            self.assertIn(b"START,SEQ=0000", self.esp.recv(1024))
            self.assertIn(b"DATA,SEQ=0001", self.esp.recv(1024))
            self.esp.sendto(b"CTRL,ID=p,ACTION=PAUSE\n", self.pc.getsockname())
            self.assertIn(b"ACK,ID=p", self.esp.recv(1024))
            self.esp.settimeout(0.3)
            with self.assertRaises(socket.timeout):
                self.esp.recv(1024)
            self.esp.sendto(b"CTRL,ID=r,ACTION=RESUME\n", self.pc.getsockname())
            self.assertIn(b"ACK,ID=r", self.esp.recv(1024))
            self.assertIn(b"DATA,SEQ=0002", self.esp.recv(1024))
            self.esp.settimeout(1)
            self.assertIn(b"SEQ=0003", self.esp.recv(1024))
            self.assertIn(b"SEQ=0004", self.esp.recv(1024))
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_tool_retries_and_only_accepts_matching_ack(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tool:
            tool.bind(("127.0.0.1", 0))
            def bridge():
                first, _ = self.esp.recvfrom(1024)  # simulate a lost command
                second, addr = self.esp.recvfrom(1024)
                self.assertEqual(first, second)
                self.esp.sendto(b"ACK,ID=wrong,ACTION=PAUSE,STATUS=OK", addr)
                ack = second.decode().strip().replace("CTRL,", "ACK,", 1) + ",STATUS=OK"
                self.esp.sendto(ack.encode(), addr)
            worker = threading.Thread(target=bridge)
            worker.start()
            self.assertTrue(execute(tool, self.esp.getsockname(), "PAUSE", timeout=0.1))
            worker.join(1)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
