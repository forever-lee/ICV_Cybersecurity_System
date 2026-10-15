import json
import queue
import sys
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
from jetson_edge.bluetooth_edge_agent import ControlWorker, decode_ack_datagram, decode_datagram, is_recent_duplicate
from jetson_edge.bluetooth_control_protocol import make_ack, make_command, parse_ack, parse_command
from fastapi.testclient import TestClient


class BluetoothCloudTests(unittest.TestCase):
    def setUp(self):
        self.state = server.VehicleState("VHC-001")

    def test_normal_packet_waits_for_time_synchronisation(self):
        source_time_ms = server.now_ms() - 25
        record = server.record_bluetooth_packet(
            self.state,
            {
                "text": "S32K344_HELLO_01",
                "source": "BLE-GATEWAY@test",
                "source_time_ms": source_time_ms,
            },
        )
        self.assertFalse(record["abnormal"])
        self.assertIsNone(record["latency_ms"])
        self.assertEqual(record["latency_quality"], "awaiting_sync")
        self.assertEqual(self.state.bluetooth_history[0]["text"], "S32K344_HELLO_01")

    def test_timestamp_embedded_by_sender_is_not_trusted_without_sync(self):
        source_time_ms = server.now_ms() - 30
        record = server.record_bluetooth_packet(
            self.state,
            {
                "text": "S32K344_HELLO_02;TS={}".format(source_time_ms),
                "source": "S32K344-ETH@192.168.4.201:5000",
            },
        )
        self.assertEqual(record["text"], "S32K344_HELLO_02")
        self.assertEqual(record["source_time_ms"], source_time_ms)
        self.assertIsNone(record["latency_ms"])
        self.assertEqual(record["latency_quality"], "awaiting_sync")

    def test_abnormal_packet_is_normalized_and_separated(self):
        record = server.record_bluetooth_packet(
            self.state,
            {"text": "S32K344_ABNORMAL_01", "source": "BLE-GATEWAY@test"},
        )
        self.assertTrue(record["abnormal"])
        self.assertEqual(
            record["text"],
            "S32K344_ABNORMAL_0001;TYPE=BLE_ATTACK;LEVEL=HIGH",
        )
        self.assertEqual(self.state.bluetooth_abnormal_count, 1)
        self.assertEqual(len(self.state.bluetooth_abnormal_history), 1)
        self.assertEqual(len(self.state.bluetooth_history), 0)

    def test_duplicate_abnormal_packet_is_not_counted_twice(self):
        payload = {
            "text": "S32K344_ABNORMAL_0001;TYPE=BLE_ATTACK;LEVEL=HIGH",
            "source": "BLE-GATEWAY@test",
        }
        server.record_bluetooth_packet(self.state, payload)
        duplicate = server.record_bluetooth_packet(self.state, payload)
        self.assertTrue(duplicate["abnormal_duplicate"])
        self.assertEqual(self.state.bluetooth_abnormal_count, 1)

    def test_distinct_timed_attacks_with_same_label_are_not_hidden(self):
        payload = {
            "text": "S32K344_ABNORMAL_0001;TYPE=BLE_ATTACK;LEVEL=HIGH",
            "source": "S32K344-ETH@192.168.4.20:62510",
            "sender_sequence": 17, "source_tick_us": 1000000, "sync_id": 9,
        }
        first = server.record_bluetooth_packet(self.state, payload)
        replay = server.record_bluetooth_packet(self.state, payload)
        second = server.record_bluetooth_packet(
            self.state, dict(payload, sender_sequence=18, source_tick_us=1001000))
        self.assertEqual(first["sender_sequence"], 17)
        self.assertTrue(replay["abnormal_duplicate"])
        self.assertFalse(second["abnormal_duplicate"])
        self.assertEqual(self.state.bluetooth_abnormal_count, 2)

    def test_sender_sequence_is_visible_without_clock_mapping(self):
        record = server.record_bluetooth_packet(self.state, {
            "text": "S32K344_HELLO_01",
            "source": "S32K344-ETH@192.168.4.20:62510",
            "sender_sequence": 29, "source_tick_us": 1234567, "sync_id": 7,
        })
        self.assertEqual(record["sender_sequence"], 29)
        self.assertEqual(record["source_tick_us"], 1234567)
        self.assertEqual(record["sync_id"], 7)
        self.assertIsNone(record["latency_ms"])

    def test_old_gateway_source_cannot_upload_diagnostics_or_hex_dump(self):
        for text in ("5333324B3334345F48454C4C4F5F3138", "3;TYPE=BLE_ATTACK;LEVEL=HIGH"):
            with self.subTest(text=text):
                with self.assertRaises(server.HTTPException) as raised:
                    server.record_bluetooth_packet(
                        self.state, {"text": text, "source": "BLE-GATEWAY@192.168.4.20:62510"}
                    )
                self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(self.state.bluetooth_sequence, 0)


class BluetoothEdgeTests(unittest.TestCase):
    def test_k344_stripped_ack_delimiter_is_restored_and_validated(self):
        raw = make_ack(8, "applied", "off", "A1B2C3D4E5F60708")
        ack, frame = decode_ack_datagram(
            raw[:-1].encode("ascii"), ("192.168.4.20", 62510), "192.168.4.20"
        )
        self.assertEqual(ack["state"], "off")
        self.assertEqual(frame, raw)
        with self.assertRaises(ValueError):
            decode_ack_datagram(raw[:-1].encode("ascii"),
                                ("192.168.4.21", 62510), "192.168.4.20")

    def test_json_gateway_envelope_keeps_timestamp(self):
        sent_at_ms = int(time.time() * 1000)
        data = json.dumps({
            "text": "S32K344_HELLO_02#",
            "source_time_ms": sent_at_ms,
        }).encode("utf-8")
        payloads = decode_datagram(data, ("192.168.4.20", 62510))
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["text"], "S32K344_HELLO_02")
        self.assertEqual(payloads[0]["source_time_ms"], sent_at_ms)

    def test_raw_datagram_can_contain_multiple_frames(self):
        payloads = decode_datagram(
            b"S32K344_HELLO_03#S32K344_HELLO_04#",
            ("192.168.4.20", 62510),
        )
        self.assertEqual(
            [payload["text"] for payload in payloads],
            ["S32K344_HELLO_03", "S32K344_HELLO_04"],
        )

    def test_diagnostics_and_hex_dumps_are_not_forwarded(self):
        for raw in (
            b"5333324B3334345F48454C4C4F5F3138",
            b"3;TYPE=BLE_ATTACK;LEVEL=HIGH",
            b"S32K344_BRIDGE;UDP_SENT=515",
            b"S32K344_HELLO_18S32K344_HELLO_18",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(decode_datagram(raw, ("192.168.4.20", 62510)), [])

    def test_duplicate_valid_frame_is_skipped_for_ten_seconds(self):
        cache = {}
        source = "S32K344-ETH@192.168.4.20:62510"
        self.assertFalse(is_recent_duplicate(cache, source, "S32K344_HELLO_18", 100.0))
        self.assertTrue(is_recent_duplicate(cache, source, "S32K344_HELLO_18", 101.0))
        self.assertFalse(is_recent_duplicate(cache, source, "S32K344_HELLO_18", 112.0))

    def test_timed_frames_use_identity_instead_of_repeating_hello_label(self):
        cache = {}
        source = "S32K344-ETH@192.168.4.20:62510"
        self.assertFalse(is_recent_duplicate(
            cache, source, "S32K344_HELLO_18", 100.0, 29, 1000000))
        self.assertTrue(is_recent_duplicate(
            cache, source, "S32K344_HELLO_18", 101.0, 29, 1000000))
        self.assertFalse(is_recent_duplicate(
            cache, source, "S32K344_HELLO_18", 102.0, 30, 1001000))

    def test_valid_abnormal_frame_remains_separate_from_diagnostics(self):
        payloads = decode_datagram(
            b"S32K344_ABNORMAL_0003;TYPE=BLE_ATTACK;LEVEL=HIGH#",
            ("192.168.4.20", 62510),
        )
        self.assertEqual(len(payloads), 1)
        self.assertEqual(
            payloads[0]["text"],
            "S32K344_ABNORMAL_0003;TYPE=BLE_ATTACK;LEVEL=HIGH",
        )


class BluetoothControlTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.vehicle_id = "BT-CTRL-TEST"
        server.registry.vehicles.pop(self.vehicle_id, None)
        self.path = "/api/vehicles/{}/bluetooth/control".format(self.vehicle_id)
        self.headers = {"Authorization": "Bearer {}".format(server.INGEST_TOKEN)}
        server.registry.get(self.vehicle_id).bluetooth_control_mode = "hardware"

    def tearDown(self):
        server.registry.vehicles.pop(self.vehicle_id, None)
        self.client.close()

    def test_valid_device_ack_is_required_before_applied(self):
        queued = self.client.post(self.path, json={"command": "off"})
        self.assertEqual(queued.status_code, 200)
        pending = self.client.get(self.path + "/next", headers=self.headers).json()["command"]
        self.assertEqual(pending["status"], "queued")
        self.assertEqual(parse_command(make_command(
            pending["id"], "off", pending["created_at_ms"], pending["nonce"]
        ))["action"], "off")

        bare = {"id": pending["id"], "nonce": pending["nonce"], "status": "applied"}
        response = self.client.post(self.path + "/ack", json=bare, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(server.registry.get(self.vehicle_id).bluetooth_control_state, "unknown")

        wrong_ack = make_ack(pending["id"], "applied", "on", pending["nonce"])
        response = self.client.post(self.path + "/ack", json={**bare, "device_ack": wrong_ack}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        correct_ack = make_ack(pending["id"], "applied", "off", pending["nonce"])
        self.assertEqual(parse_ack(correct_ack)["state"], "off")
        response = self.client.post(self.path + "/ack", json={**bare, "device_ack": correct_ack}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        control = response.json()["control"]
        self.assertFalse(control["pending"])
        self.assertEqual(control["state"], "off")
        self.assertEqual(control["status"], "applied")

    def test_delivering_does_not_change_state_and_expiry_does_not_fake_success(self):
        self.client.post(self.path, json={"command": "on"})
        pending = self.client.get(self.path + "/next", headers=self.headers).json()["command"]
        ack = {"id": pending["id"], "nonce": pending["nonce"], "status": "delivering"}
        response = self.client.post(self.path + "/ack", json=ack, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["control"]["pending"])
        self.assertEqual(response.json()["control"]["state"], "unknown")
        server.registry.get(self.vehicle_id).bluetooth_control_pending["expires_at_ms"] = 1
        control = server.registry.get(self.vehicle_id).bluetooth_control_snapshot()
        self.assertEqual(control["status"], "expired")
        self.assertEqual(control["state"], "unknown")
        self.assertIsNone(self.client.get(self.path + "/next", headers=self.headers).json()["command"])

    def test_device_error_can_report_actual_state_without_marking_applied(self):
        self.client.post(self.path, json={"command": "off"})
        pending = self.client.get(self.path + "/next", headers=self.headers).json()["command"]
        raw_ack = make_ack(pending["id"], "error", "on", pending["nonce"])
        response = self.client.post(self.path + "/ack", headers=self.headers, json={
            "id": pending["id"], "nonce": pending["nonce"],
            "status": "error", "device_ack": raw_ack,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["control"]["status"], "error")
        self.assertEqual(response.json()["control"]["state"], "unknown")

    def test_edge_worker_only_reports_applied_after_matching_device_ack(self):
        stop = threading.Event()
        received = queue.Queue()
        events = []
        command = {"id": 8, "nonce": "A1B2C3D4E5F60708", "command": "off",
                   "created_at_ms": 1791950000123}

        class Udp:
            def sendto(self, data, address):
                events.append(("udp", parse_command(data), address))
                received.put((parse_ack(make_ack(8, "applied", "off", command["nonce"])),
                              make_ack(8, "applied", "off", command["nonce"]),
                              server.now_ms()))

        class Worker(ControlWorker):
            def request(self, method, suffix, payload=None):
                if method == "GET":
                    return {"command": command}
                events.append(("cloud", payload))
                if payload["status"] == "applied":
                    stop.set()
                return {"accepted": True}

        worker = Worker("http://invalid", "unused", Udp(), "192.168.4.20", 5001,
                        received, stop, 0.01, 1.0, 1.0)
        worker.start()
        worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(events[0][0], "udp")
        self.assertEqual(events[0][2], ("192.168.4.20", 5001))
        self.assertEqual(events[-1][1]["status"], "applied")
        self.assertEqual(events[-1][1]["device_ack"], make_ack(8, "applied", "off", command["nonce"]))


class BluetoothLocalSenderTests(unittest.TestCase):
    def setUp(self):
        self.vehicle_id = "BT-LOCAL-TEST"
        server.registry.vehicles.pop(self.vehicle_id, None)
        server.registry.get(self.vehicle_id).bluetooth_control_mode = "local_sender"
        self.client = TestClient(server.app, client=("127.0.0.1", 50000))
        self.base = "/api/vehicles/{}/bluetooth".format(self.vehicle_id)
        self.session = "a" * 32

    def tearDown(self):
        server.registry.vehicles.pop(self.vehicle_id, None)
        self.client.close()

    def status(self, state, revision, source=None):
        payload = {"session": self.session, "state": state, "revision": revision,
                   "ble_connected": True}
        if source:
            payload["source"] = source
        return self.client.post(self.base + "/local/status", json=payload)

    def test_page_waits_for_laptop_ack_and_jetson_cannot_take_command(self):
        self.assertEqual(self.status("on", 0).status_code, 200)
        queued = self.client.post(self.base + "/control", json={"command": "off"})
        self.assertEqual(queued.status_code, 200)
        self.assertEqual(queued.json()["control"]["state"], "on")
        self.assertTrue(queued.json()["control"]["pending"])
        hardware = self.client.get(self.base + "/control/next", headers={
            "Authorization": "Bearer {}".format(server.INGEST_TOKEN)})
        self.assertIsNone(hardware.json()["command"])
        command = self.client.get(self.base + "/local/next", params={
            "session": self.session}).json()["command"]
        self.assertEqual(command["command"], "off")
        self.assertEqual(self.status("off", 1).json()["control"]["state"], "on")
        applied = self.client.post(self.base + "/local/ack", json={
            "session": self.session, "id": command["id"], "nonce": command["nonce"],
            "state": "off", "revision": 1,
        })
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.json()["control"]["state"], "off")
        self.assertEqual(applied.json()["control"]["status"], "applied")

    def test_cmd_updates_page_and_mismatched_ack_is_rejected(self):
        self.status("on", 0)
        manual = self.status("off", 1, "cmd")
        self.assertEqual(manual.json()["control"]["state"], "off")
        self.assertEqual(manual.json()["control"]["last_ack"]["source"], "cmd")
        self.client.post(self.base + "/control", json={"command": "on"})
        command = self.client.get(self.base + "/local/next", params={
            "session": self.session}).json()["command"]
        bad = self.client.post(self.base + "/local/ack", json={
            "session": self.session, "id": command["id"], "nonce": "wrong",
            "state": "on", "revision": 2,
        })
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(server.registry.get(self.vehicle_id).bluetooth_control_state, "off")

    def test_local_control_rejects_non_loopback_clients(self):
        remote = TestClient(server.app, client=("192.168.1.103", 50000))
        try:
            response = remote.post(self.base + "/local/status", json={
                "session": self.session, "state": "on", "revision": 0,
            })
            self.assertEqual(response.status_code, 403)
        finally:
            remote.close()


if __name__ == "__main__":
    unittest.main()
