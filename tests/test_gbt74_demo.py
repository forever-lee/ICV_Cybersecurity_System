import unittest
from types import SimpleNamespace

from bms_wifi_sender import build_report
from wifi_ids import parse_charging_packet, detect_wifi_intrusion
from jetson_edge.WiFi_Module import build_cloud_payload


class Gbt74DemoTests(unittest.TestCase):
    def test_vector_and_timestamp_forwarding(self):
        text = build_report('START', 0, 20.0, 'CHARGING')
        charge = parse_charging_packet(text)
        self.assertEqual(charge['payload_hex'], '74C8000200')
        self.assertEqual(charge['soc'], 20.0)
        payload = build_cloud_payload(text.encode(), ('127.0.0.1', 1234))
        self.assertEqual(payload['sender_time_ms'], charge['sender_time_ms'])

    def test_invalid_pgi_length_and_soc(self):
        self.assertIsNone(parse_charging_packet('GBT74,DATA,SEQ=1,DATA=75C8000200,STATE=CHARGING'))
        self.assertIsNone(parse_charging_packet('GBT74,DATA,SEQ=1,DATA=74C80002,STATE=CHARGING'))
        self.assertIsNone(parse_charging_packet('GBT74,DATA,SEQ=1,DATA=74E9030200,STATE=CHARGING'))

    def test_attacks_recovery_and_next_round(self):
        state = SimpleNamespace(wifi_ids_last_good_soc=None, wifi_ids_last_good_sequence=None,
                                wifi_ids_alert_count=0, wifi_ids_latest_alert=None)
        def detect(kind, seq, soc):
            charge = parse_charging_packet(build_report(kind, seq, soc, 'CHARGING'))
            return detect_wifi_intrusion(state, charge, 1700000000000)
        self.assertEqual(detect('START', 0, 20)['verdict'], 'normal')
        self.assertEqual(detect('DATA', 1, 20.7)['verdict'], 'normal')
        self.assertEqual(detect('DATA', 2, 40)['verdict'], 'anomaly')
        self.assertEqual(detect('DATA', 3, 22)['verdict'], 'normal')
        self.assertEqual(state.wifi_ids_alert_count, 1)
        detect('START', 0, 20)
        self.assertEqual(state.wifi_ids_alert_count, 0)


if __name__ == '__main__':
    unittest.main()
