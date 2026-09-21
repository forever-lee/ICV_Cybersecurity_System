import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wifi_ids import detect_wifi_intrusion, parse_charging_packet


class WifiIdsTests(unittest.TestCase):
    def setUp(self):
        self.state = SimpleNamespace(
            wifi_ids_last_good_soc=None,
            wifi_ids_last_good_sequence=None,
            wifi_ids_alert_count=0,
            wifi_ids_latest_alert=None,
        )
        self.detected_at_ms = 1700000000000

    def record(self, text):
        self.detected_at_ms += 1000
        return detect_wifi_intrusion(
            self.state, parse_charging_packet(text), self.detected_at_ms
        )

    def test_normal_charge_does_not_alert(self):
        self.record("CHG,START,SEQ=0000,SOC=25.0,STATE=CHARGING")
        result = self.record("CHG,DATA,SEQ=0001,SOC=25.7,STATE=CHARGING")
        self.assertEqual(result["verdict"], "normal")
        self.assertEqual(self.state.wifi_ids_alert_count, 0)
        self.assertIsNone(self.state.wifi_ids_latest_alert)

    def test_soc_jump_alerts_without_poisoning_baseline(self):
        self.record("CHG,START,SEQ=0000,SOC=25.0,STATE=CHARGING")
        self.record("CHG,DATA,SEQ=0001,SOC=25.7,STATE=CHARGING")
        attack = self.record("CHG,DATA,SEQ=0002,SOC=45.9,STATE=CHARGING")
        recovered = self.record("CHG,DATA,SEQ=0003,SOC=27.0,STATE=CHARGING")

        self.assertEqual(attack["verdict"], "anomaly")
        self.assertEqual(attack["rule"], "SOC_JUMP")
        self.assertEqual(recovered["verdict"], "normal")
        self.assertEqual(self.state.wifi_ids_alert_count, 1)
        self.assertEqual(self.state.wifi_ids_last_good_sequence, 3)
        self.assertIsNotNone(self.state.wifi_ids_latest_alert)

    def test_soc_regression_alerts(self):
        self.record("CHG,START,SEQ=0000,SOC=40.0,STATE=CHARGING")
        result = self.record("CHG,DATA,SEQ=0001,SOC=35.0,STATE=CHARGING")
        self.assertEqual(result["verdict"], "anomaly")
        self.assertLess(result["alert"]["delta_soc"], 0)

    def test_new_start_clears_latched_alert(self):
        self.record("CHG,START,SEQ=0000,SOC=25.0,STATE=CHARGING")
        self.record("CHG,DATA,SEQ=0001,SOC=50.0,STATE=CHARGING")
        result = self.record("CHG,START,SEQ=0000,SOC=28.0,STATE=CHARGING")
        self.assertEqual(result["verdict"], "normal")
        self.assertEqual(self.state.wifi_ids_alert_count, 0)
        self.assertIsNone(self.state.wifi_ids_latest_alert)

    def test_legacy_packet_without_timestamp_keeps_latency_empty(self):
        charge = parse_charging_packet(
            "CHG,START,SEQ=0000,SOC=25.0,STATE=CHARGING"
        )
        self.assertIsNone(charge["sender_time_ms"])


if __name__ == "__main__":
    unittest.main()
