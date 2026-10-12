import json
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
from jetson_edge.bluetooth_edge_agent import decode_datagram, is_recent_duplicate


class BluetoothCloudTests(unittest.TestCase):
    def setUp(self):
        self.state = server.VehicleState("VHC-001")

    def test_normal_packet_has_end_to_end_latency(self):
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
        self.assertGreaterEqual(record["latency_ms"], 25)
        self.assertLess(record["latency_ms"], 1000)
        self.assertEqual(self.state.bluetooth_history[0]["text"], "S32K344_HELLO_01")

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


if __name__ == "__main__":
    unittest.main()
