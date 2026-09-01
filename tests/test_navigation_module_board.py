import json
import os
import sys
import tempfile
import types
import unittest
from collections import deque
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import Navigation_Module_Board as navigation


class FakeAtPort:
    def __init__(self, responses):
        self.responses = responses
        self.pending = deque()
        self.commands = []
        self.closed = False

    def reset_input_buffer(self):
        self.pending.clear()

    def write(self, payload):
        command = payload.decode("ascii").strip()
        self.commands.append(command)
        self.pending.extend(
            line.encode("ascii") + b"\r\n"
            for line in self.responses.get(command, ())
        )

    def flush(self):
        pass

    def readline(self):
        return self.pending.popleft() if self.pending else b""

    def close(self):
        self.closed = True


class InterruptingNmeaPort:
    def __init__(self, lines=()):
        self.closed = False
        self.lines = deque(
            line.encode("ascii") + b"\r\n" for line in lines
        )

    def readline(self):
        if self.lines:
            return self.lines.popleft()
        raise KeyboardInterrupt()

    def close(self):
        self.closed = True


class GnssAtSessionTests(unittest.TestCase):
    def test_starts_inactive_session_and_stops_it(self):
        port = FakeAtPort({
            "AT": ("AT", "OK"),
            'AT+QGPSCFG="outport","usbnmea"': ("OK",),
            "AT+QGPS?": ("+QGPS: 0", "OK"),
            "AT+QGPS=1": ("OK",),
            "AT+QGPSEND": ("OK",),
        })

        self.assertTrue(navigation.start_gnss_session(port))
        navigation.stop_gnss_session(port)

        self.assertEqual(port.commands, [
            "AT",
            'AT+QGPSCFG="outport","usbnmea"',
            "AT+QGPS?",
            "AT+QGPS=1",
            "AT+QGPSEND",
        ])

    def test_reuses_active_session_without_duplicate_start(self):
        port = FakeAtPort({
            "AT": ("OK",),
            'AT+QGPSCFG="outport","usbnmea"': ("OK",),
            "AT+QGPS?": ("+QGPS: 1", "OK"),
        })

        self.assertFalse(navigation.start_gnss_session(port))
        self.assertNotIn("AT+QGPS=1", port.commands)

    def test_reports_error_and_cme_error(self):
        for response in ("ERROR", "+CME ERROR: 504"):
            port = FakeAtPort({"AT": (response,)})
            with self.assertRaises(navigation.GnssAtError):
                navigation.send_at_command(port, "AT")

    def test_reports_response_timeout(self):
        port = FakeAtPort({"AT": ()})
        with mock.patch.object(navigation.time, "monotonic", side_effect=(10.0, 11.0)):
            with self.assertRaises(navigation.GnssAtError):
                navigation.send_at_command(port, "AT", timeout=0.5)

    def test_serial_mode_ends_session_and_closes_ports_on_interrupt(self):
        at_port = FakeAtPort({
            "AT": ("OK",),
            'AT+QGPSCFG="outport","usbnmea"': ("OK",),
            "AT+QGPS?": ("+QGPS: 0", "OK"),
            "AT+QGPS=1": ("OK",),
            "AT+QGPSEND": ("OK",),
        })
        nmea_port = InterruptingNmeaPort()
        ports = iter((at_port, nmea_port))
        serial_module = types.SimpleNamespace(
            Serial=lambda *args, **kwargs: next(ports),
            SerialException=OSError,
        )
        args = types.SimpleNamespace(
            at_device="/dev/ttyUSB2",
            at_baud=115200,
            nmea_device="/dev/ttyUSB1",
            nmea_baud=115200,
            at_timeout=5.0,
        )
        with tempfile.TemporaryDirectory() as directory:
            state = navigation.NavigationState(
                os.path.join(directory, "navigation_live.json")
            )
            with mock.patch.dict(sys.modules, {"serial": serial_module}):
                with self.assertRaises(KeyboardInterrupt):
                    navigation.run_serial(args, state)

        self.assertEqual(at_port.commands[-1], "AT+QGPSEND")
        self.assertTrue(at_port.closed)
        self.assertTrue(nmea_port.closed)

    def test_serial_mode_reports_invalid_fix_without_writing_json(self):
        at_port = FakeAtPort({
            "AT": ("OK",),
            'AT+QGPSCFG="outport","usbnmea"': ("OK",),
            "AT+QGPS?": ("+QGPS: 0", "OK"),
            "AT+QGPS=1": ("OK",),
            "AT+QGPSEND": ("OK",),
        })
        nmea_port = InterruptingNmeaPort((
            "$GPRMC,123519,V,,,,,,,230394,,,N*53",
        ))
        ports = iter((at_port, nmea_port))
        serial_module = types.SimpleNamespace(
            Serial=lambda *args, **kwargs: next(ports),
            SerialException=OSError,
        )
        args = types.SimpleNamespace(
            at_device="/dev/ttyUSB2", at_baud=115200,
            nmea_device="/dev/ttyUSB1", nmea_baud=115200,
            at_timeout=5.0,
        )
        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "navigation_live.json")
            state = navigation.NavigationState(output_path)
            output = StringIO()
            with mock.patch.dict(sys.modules, {"serial": serial_module}):
                with redirect_stdout(output):
                    with self.assertRaises(KeyboardInterrupt):
                        navigation.run_serial(args, state)
            self.assertFalse(os.path.exists(output_path))
            self.assertIn("无有效GPS数据", output.getvalue())


class NavigationOutputTests(unittest.TestCase):
    def test_valid_gns_produces_position(self):
        sentence_without_checksum = (
            "$GNGNS,092750.000,5321.6802,N,00630.3372,W,AA,08,"
            "1.03,61.7,55.2,,"
        )
        checksum = 0
        for character in sentence_without_checksum[1:]:
            checksum ^= ord(character)
        sample = navigation.parse_nmea(
            "{}*{:02X}".format(sentence_without_checksum, checksum)
        )
        self.assertIsNotNone(sample)
        self.assertAlmostEqual(sample["latitude"], 53.3613367, places=6)
        self.assertAlmostEqual(sample["longitude"], -6.50562, places=6)
        self.assertEqual(sample["source"], "EDGE-GNSS-GNS")

    def test_valid_nmea_updates_json_without_raw_text_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "navigation_live.json")
            state = navigation.NavigationState(output_path)

            self.assertTrue(navigation.handle_text(
                state,
                "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,"
                "230394,003.1,W*6A\r\n",
            ))
            self.assertTrue(navigation.handle_text(
                state,
                "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,"
                "545.4,M,46.9,M,,*47\r\n",
            ))
            self.assertTrue(navigation.handle_text(
                state,
                "$GPVTG,054.7,T,034.4,M,005.5,N,010.2,K*48\r\n",
            ))

            with open(output_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertAlmostEqual(payload["latitude"], 48.1173, places=6)
            self.assertAlmostEqual(payload["longitude"], 11.5166667, places=6)
            self.assertEqual(payload["speed_kph"], 10.2)
            self.assertEqual(payload["heading_deg"], 54.7)
            self.assertEqual(sorted(os.listdir(directory)), ["navigation_live.json"])

    def test_invalid_checksum_does_not_overwrite_last_position(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "navigation_live.json")
            state = navigation.NavigationState(output_path)
            valid = (
                "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,"
                "230394,003.1,W*6A"
            )
            self.assertTrue(navigation.handle_text(state, valid))
            with open(output_path, "rb") as handle:
                before = handle.read()

            self.assertFalse(navigation.handle_text(state, valid[:-2] + "00"))
            with open(output_path, "rb") as handle:
                self.assertEqual(handle.read(), before)


if __name__ == "__main__":
    unittest.main()
