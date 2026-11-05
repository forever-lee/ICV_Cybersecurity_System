"""B HTTP + real UDP bridge. Drop commands/ACKs and verify recovery/counting."""
import select
import socket
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import server
from wifi_control import WifiControl
from bms_wifi_sender import SenderControl, build_report
from jetson_edge.wifi_control_agent import WifiControlAgent
from jetson_edge.wifi_control_protocol import parse_ack


class PlatformLoopTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(server, "INGEST_TOKEN", "test-edge"),
                        patch.object(server, "DASHBOARD_ACCESS_TOKEN", "test-ui"),
                        patch.object(server, "registry", server.Registry())]
        for item in self.patches:
            item.start()
        self.client = TestClient(server.app)
        self.headers = {"Authorization": "Bearer test-edge"}
        self.client.cookies.set("vcl_access", "test-ui")
        self.path = "/api/vehicles/VHC-001/wifi"
        self.sockets = []
        for _ in range(4):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sock.settimeout(.3)
            self.sockets.append(sock)
        self.edge, self.board, self.esp, self.pc = self.sockets
        self.sender = SenderControl(self.pc, *self.esp.getsockname())
        self.control = server.registry.get("VHC-001").wifi_control
        self.agent = WifiControlAgent(self.edge, self.board.getsockname(), "http://localhost", "VHC-001", "test-edge", http=self.http)

    def tearDown(self):
        for sock in self.sockets:
            sock.close()
        self.client.close()
        for item in reversed(self.patches):
            item.stop()

    def http(self, suffix, payload=None):
        url = self.path + "/control" + suffix
        response = self.client.get(url, headers=self.headers) if payload is None else self.client.post(url, json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def pump(self, drop_command=False, drop_ack=False):
        self.agent.last_sent -= 1.1
        self.agent.step()
        while select.select([self.board], [], [], 0)[0]:
            wire, _ = self.board.recvfrom(4096)
            if drop_command:
                continue
            self.esp.sendto(wire, self.pc.getsockname())
            self.sender.receive(.1)
            ack, _ = self.esp.recvfrom(4096)
            if drop_ack:
                continue
            self.board.sendto(ack, self.edge.getsockname())
            data, addr = self.edge.recvfrom(4096)
            self.assertTrue(self.agent.receive_ack(data, addr))

    def flush(self):
        for _ in range(12):
            self.pump()

    def queue(self, action):
        response = self.client.post(self.path + "/control", json={"command": action})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["control"]["pending"]

    def test_loss_retry_ack_recovery_and_pause_without_telemetry(self):
        self.flush()  # periodic STATUS establishes actual sender state
        self.assertEqual(self.control.snapshot()["state"], "running")
        self.queue("pause")
        self.pump(drop_command=True)
        self.pump(drop_ack=True)
        self.assertTrue(self.sender.paused)
        self.flush()
        self.assertEqual(self.control.snapshot()["state"], "paused")
        self.assertEqual(self.control.snapshot()["status"], "applied")
        self.queue("resume")
        self.flush()
        self.assertFalse(self.sender.paused)
        snapshot = server.registry.get("VHC-001").wifi_snapshot()
        self.assertEqual(snapshot["packet_count"], 0)
        self.assertGreater(snapshot["tx_count"], 0)
        self.assertGreater(snapshot["rx_count"], 0)
        self.assertEqual({r["direction"] for r in snapshot["traffic_history"]}, {"TX", "RX"})

    def test_state_unknown_after_timeout_and_manual_action_preempts_probe(self):
        clock = [1700000000000]
        c = WifiControl(lambda: clock[0])
        probe = c.poll()
        pending = c.queue("pause")["pending"]
        self.assertNotEqual(probe["id"], pending["id"])
        self.assertEqual(c.poll()["id"], pending["id"])
        clock[0] += 12001
        self.assertEqual(c.snapshot()["status"], "expired")
        self.assertEqual(c.snapshot()["state"], "unknown")
        with self.assertRaises(ConnectionError):
            c.queue("resume")

    def test_no_repeat_attack_http_receipt_dedup_and_authorization(self):
        self.flush()
        job = self.queue("flood")
        self.pump(drop_ack=True)
        deadline = self.sender.attack_at
        self.flush()
        self.assertEqual(deadline, self.sender.attack_at)
        self.assertEqual(self.control.snapshot()["mode"], "flood")
        self.queue("stop")
        self.flush()
        self.assertEqual(self.sender.mode, "NONE")
        receipt = {"sent": {"id": job["id"], "attempt": 1, "event_id": "test-transport-retry"}}
        self.http("/ack", receipt)
        count = self.control.tx_count
        self.http("/ack", receipt)
        self.assertEqual(count, self.control.tx_count)
        with TestClient(server.app) as other:
            self.assertEqual(other.get(self.path + "/control/next").status_code, 401)
            self.assertEqual(other.post(self.path + "/control", json={"command": "pause"}).status_code, 401)
            self.assertEqual(other.post(self.path + "/control/ack", json={}).status_code, 401)
        self.assertEqual(self.client.post(self.path + "/control/ack", headers=self.headers, json={"status": "applied"}).status_code, 400)

    def test_http_failure_retains_ack_and_retries_later(self):
        self.flush()
        self.queue("pause")
        self.pump()
        original = self.agent.http
        self.agent.http = lambda *args: (_ for _ in ()).throw(OSError("simulated network outage"))
        with self.assertRaises(OSError):
            self.agent.step()
        self.assertIsNotNone(self.agent.pending_ack)
        self.agent.http = original
        self.flush()
        self.assertEqual(self.control.snapshot()["state"], "paused")

    def test_rate_ids_only_charging_packets_and_edge_receive_time(self):
        state = server.registry.get("VHC-001")
        start = 1700000000000
        def record(kind, seq, soc, at):
            return server.record_wifi_packet(state, {"text": build_report(kind, seq, soc, "CHARGING"), "udp_received_at_ms": at})
        record("START", 0, 20, start)
        # HTTP burst of buffered NORMAL one-per-second packets must not false-alarm.
        for seq in range(1, 121):
            record("DATA", seq, 20 + seq * 80 / 120, start + seq * 1000)
        self.assertEqual(state.wifi_ids_alert_count, 0)
        record("START", 0, 20, start + 125000)
        for seq in range(1, 31):
            result = record("DATA", seq, 20.1 + seq * .067, start + 125000 + seq * 100)
        self.assertEqual(result["ids_rule"], "WIFI_RATE")
        self.assertEqual(state.wifi_ids_alert_count, 1)
        for seq in range(31, 36):
            record("DATA", seq, 23, start + 130000 + seq * 1000)
        self.assertEqual(state.wifi_ids_latest_alert["rule"], "WIFI_RATE", "alert remains visible after flood stops")
        record("START", 0, 20, start + 200000)
        record("DATA", 1, 20.7, start + 201000)
        bad = record("DATA", 2, 44, start + 202000)
        self.assertEqual(bad["ids_rule"], "SOC_JUMP")
        recovered = record("DATA", 3, 22, start + 203000)
        self.assertEqual(recovered["ids_verdict"], "normal")

    def test_all_five_acks_lost_then_status_recovers_actual_paused_state(self):
        self.flush()
        self.queue("pause")
        before = self.control.tx_count
        for _ in range(14):
            self.pump(drop_ack=True)
        self.assertTrue(self.sender.paused)
        self.assertEqual(self.control.tx_count - before, 5, "bounded retries, no infinite flooding")
        self.control.clock = lambda: int(__import__("time").time() * 1000) + 13000
        self.flush()
        self.assertEqual(self.control.snapshot()["state"], "paused")
        self.assertEqual(self.control.snapshot()["status"], "applied")

    def test_stale_http_reply_does_not_claim_live_connection(self):
        self.flush()
        job = self.queue("pause")
        old = "ACK,ID={},ACTION=PAUSE,STATUS=OK,STATE=PAUSED,MODE=NONE,LAST={}".format(job["id"], job["order"])
        now = int(__import__("time").time() * 1000)
        self.control.sender_seen_ms = 0
        self.control.acknowledge({"device_ack": old, "received_at_ms": now - 30000})
        self.assertEqual(self.control.snapshot()["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
