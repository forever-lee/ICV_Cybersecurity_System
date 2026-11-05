"""B sender real-UDP tests: fragmentation, ordering, expiry, attack dedup."""
import socket
import unittest
from unittest.mock import patch
import bms_wifi_sender as sender
from jetson_edge.wifi_control_protocol import encode_command, parse_ack, parse_command


class DownlinkTests(unittest.TestCase):
    def setUp(self):
        self.esp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.pc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.esp.bind(("127.0.0.1", 0))
        self.pc.bind(("127.0.0.1", 0))
        self.esp.settimeout(0.5)
        self.now = 1700000000000
        self.control = sender.SenderControl(self.pc, *self.esp.getsockname(), clock=lambda: self.now)

    def tearDown(self):
        self.esp.close()
        self.pc.close()

    def wire(self, action, order, ident=None, expiry=None):
        return encode_command({"id": ident or str(order), "command": action, "order": order,
                               "expires_at_ms": expiry or self.now + 12000})

    def command(self, action, order, ident=None, expiry=None):
        self.esp.sendto(self.wire(action, order, ident, expiry), self.pc.getsockname())
        self.control.receive(0.1)
        return parse_ack(self.esp.recv(4096).decode())

    def test_split_duplicate_and_late_pause_never_overrides_resume(self):
        wire = self.wire("pause", 10)
        self.esp.sendto(wire[:20], self.pc.getsockname())
        self.control.receive(0.1)
        self.assertFalse(self.control.paused)
        self.esp.sendto(wire[20:], self.pc.getsockname())
        self.control.receive(0.1)
        self.esp.recv(4096)
        self.assertTrue(self.control.paused)
        self.command("resume", 12)
        ack = self.command("pause", 10)
        self.assertEqual(ack["state"], "running")
        self.assertFalse(self.control.paused)
        self.assertEqual(self.command("pause", 11)["status"], "stale")
        self.assertFalse(self.control.paused)

    def test_expired_wrong_peer_and_oversized_command(self):
        self.assertEqual(self.command("pause", 1, expiry=self.now - 1)["status"], "expired")
        self.assertFalse(self.control.paused)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
            other.sendto(self.wire("pause", 2), self.pc.getsockname())
            self.control.receive(0.1)
        self.assertFalse(self.control.paused)
        self.assertIsNone(parse_command("CTRL,ID=a,ACTION=REBOOT,SEQ=3,EXP=999"))
        with self.assertRaises(ValueError):
            encode_command({"id": "x" * 41, "command": "pause", "order": 3, "expires_at_ms": self.now})

    def test_status_while_paused_and_manual_attack_dedup(self):
        self.command("pause", 1)
        self.assertEqual(self.command("status", 2)["state"], "paused")
        self.command("resume", 3)
        self.command("flood", 4)
        deadline = self.control.attack_at
        self.control.active_time += 1
        self.command("flood", 4)
        self.assertEqual(self.control.attack_at, deadline)
        self.control.active_time = self.control.attack_until
        self.assertEqual(self.control.attack_mode(), "NONE")
        self.command("soc", 5)
        self.command("pause", 6)
        self.assertEqual(self.control.mode, "NONE")

    def test_simulated_round_rate_and_progress_depend_on_time_not_packet_count(self):
        # Deterministic ticks execute a full flood round without 120 seconds of waiting.
        c = self.control
        c.mode, c.attack_at, c.attack_until = "FLOOD", 2, 12
        records = []
        def tick(*args):
            c.active_time += 0.02
        with patch.object(c, "tick", side_effect=tick), patch.object(sender, "ROUND_SECONDS", 15), \
                patch.object(sender, "send_report", side_effect=lambda *args: records.append((c.active_time, args[3], args[4] if len(args) > 4 else False))):
            sender.run_round(self.pc, *self.esp.getsockname(), 1, control=c)
        from wifi_ids import parse_charging_packet
        flood = [(at, parse_charging_packet(text)) for at, text, attack in records if attack]
        self.assertGreater(len(flood), 85)
        self.assertLessEqual(len(flood), 101)
        self.assertAlmostEqual(flood[0][1]["soc"], 20 + 80 * flood[0][0] / 15, delta=0.1)
        self.assertEqual(parse_charging_packet(records[-1][1])["state"], "FULL")
        self.assertEqual(parse_charging_packet(records[-1][1])["soc"], 100)

    def test_single_soc_injection_and_normal_default(self):
        c = self.control
        records = []
        with patch.object(c, "tick", side_effect=lambda *a: setattr(c, "active_time", c.active_time + .02)), \
                patch.object(sender, "ROUND_SECONDS", 5), \
                patch.object(sender, "send_report", side_effect=lambda *a: records.append(a)):
            sender.run_round(self.pc, *self.esp.getsockname(), 1, control=c)
        self.assertFalse(any(len(a) > 4 and a[4] for a in records))
        c.active_time = 0
        c.mode, c.attack_at = "SOC", 2
        records.clear()
        with patch.object(c, "tick", side_effect=lambda *a: setattr(c, "active_time", c.active_time + .02)), \
                patch.object(sender, "ROUND_SECONDS", 5), \
                patch.object(sender, "send_report", side_effect=lambda *a: records.append(a)):
            sender.run_round(self.pc, *self.esp.getsockname(), 2, control=c)
        self.assertEqual(sum(bool(a[4]) for a in records if len(a) > 4), 1)


if __name__ == "__main__":
    unittest.main()
