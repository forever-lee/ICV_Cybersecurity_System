import socket
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import server
from wifi_control import WifiControl
from bms_wifi_sender import SenderControl, build_report
from jetson_edge.wifi_control_agent import WifiControlAgent


class ControlStateTests(unittest.TestCase):
    def test_expiry_and_only_matching_sender_ack_changes_state(self):
        clock = [1000]
        control = WifiControl(lambda: clock[0])
        with self.assertRaises(ConnectionError):
            control.queue("pause")
        control.poll()
        pending = control.queue("pause")["pending"]
        with self.assertRaises(ValueError):
            control.acknowledge({"status": "applied"})
        with self.assertRaises(RuntimeError):
            control.acknowledge({"device_ack": "ACK,ID=wrong,ACTION=PAUSE,STATUS=OK"})
        self.assertEqual(control.confirmed_state, "unknown")
        clock[0] += 15000
        self.assertEqual(control.snapshot()["status"], "expired")
        self.assertEqual(control.confirmed_state, "unknown")
        control.poll()
        next_command = control.queue("resume")["pending"]
        self.assertNotEqual(pending["id"], next_command["id"])


class PlatformLoopTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(server, "INGEST_TOKEN", "test-edge"),
                        patch.object(server, "DASHBOARD_ACCESS_TOKEN", "test-ui"),
                        patch.object(server, "registry", server.Registry())]
        for item in self.patches:
            item.start()
        self.client = TestClient(server.app)
        self.edge_headers = {"Authorization": "Bearer test-edge"}
        self.client.cookies.set("vcl_access", "test-ui")
        self.path = "/api/vehicles/VHC-001/wifi"
        self.sockets = []

    def tearDown(self):
        self.client.close()
        for sock in self.sockets:
            sock.close()
        for item in reversed(self.patches):
            item.stop()

    def udp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.5)
        self.sockets.append(sock)
        return sock

    def test_http_udp_sender_ack_loop_and_resume_without_telemetry(self):
        edge, board, esp, pc = [self.udp() for _ in range(4)]
        sender = SenderControl(pc, *esp.getsockname())

        def http(suffix, payload=None):
            url = self.path + "/control" + suffix
            response = self.client.get(url, headers=self.edge_headers) if payload is None else self.client.post(url, json=payload, headers=self.edge_headers)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        agent = WifiControlAgent(edge, board.getsockname(), "http://localhost", "VHC-001", "test-edge", http=http)
        agent.step()  # registers edge heartbeat with no telemetry at all
        for action, expected_state in [("pause", "paused"), ("resume", "running")]:
            queued = self.client.post(self.path + "/control", json={"command": action})
            self.assertEqual(queued.status_code, 200)
            self.assertEqual(queued.json()["control"]["status"], "queued")
            self.assertEqual(self.client.post(self.path + "/control", json={"command": "pause"}).status_code, 409)
            agent.step()
            wire, _ = board.recvfrom(4096)
            esp.sendto(wire, pc.getsockname())  # S32K UART -> ESP32 passthrough
            sender.receive(0.5)
            ack, _ = esp.recvfrom(4096)
            board.sendto(ack, edge.getsockname())  # original Ethernet uplink
            data, addr = edge.recvfrom(4096)
            self.assertTrue(agent.receive_ack(data, addr))
            agent.step()
            snapshot = server.registry.get("VHC-001").wifi_snapshot()
            self.assertEqual(snapshot["control"]["state"], expected_state)
            self.assertEqual(snapshot["control"]["status"], "applied")
            self.assertEqual(snapshot["packet_count"], 0, "ACK must not become charging telemetry")
        self.assertFalse(sender.paused)
        # Real charging payload remains parseable after control traffic.
        result = self.client.post(self.path, headers=self.edge_headers, json={"text": build_report("START", 0, 20, "CHARGING")})
        self.assertEqual(result.json()["wifi"]["parsed_charge"]["soc"], 20)

    def test_authorization_and_invalid_acks(self):
        with TestClient(server.app) as client:
            self.assertEqual(client.post(self.path + "/control", json={"command": "pause"}).status_code, 401)
            self.assertEqual(client.get(self.path + "/control/next").status_code, 401)
            self.assertEqual(client.post(self.path + "/control/ack", json={}).status_code, 401)
        self.assertEqual(self.client.post(self.path + "/control", json=[]).status_code, 400)
        self.client.get(self.path + "/control/next", headers=self.edge_headers)
        self.assertEqual(self.client.post(self.path + "/control", json={"command": "reboot"}).status_code, 400)
        self.assertEqual(self.client.post(self.path + "/control/ack", headers=self.edge_headers, json={"status": "applied"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
