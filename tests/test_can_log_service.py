import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import can_log_service as can_log_module
from can_log_service import (
    CanLogNotFoundError,
    CanLogService,
    CanLogValidationError,
)


class FakeSignal:
    def __init__(self, name, unit="", receivers=()):
        self.name = name
        self.unit = unit
        self.receivers = list(receivers)


class FakeMessageDefinition:
    def __init__(self, frame_id, name, signal_names, sender):
        self.frame_id = frame_id
        self.name = name
        self.senders = [sender]
        self.cycle_time = 100
        self.signals = [FakeSignal(value, "", ("TMS",)) for value in signal_names]

    def decode(self, data, decode_choices=False, **_kwargs):
        value = data[0] if data else 0
        if self.frame_id == 0x2F9:
            result = {"HU_ACDEFAdjustReq": value, "HU_ACFanAdjustValueReq": 7}
            if decode_choices:
                result["HU_ACDEFAdjustReq"] = {0: "NoReq", 1: "Off", 2: "On"}.get(value, "DEF")
            return result
        if self.frame_id == 0x330:
            result = {"AC_FrontDefrosterSwitch": value, "AC_SystemFailureFlag": 0}
            if decode_choices:
                result["AC_FrontDefrosterSwitch"] = "Active" if value else "Inactive"
                result["AC_SystemFailureFlag"] = "Not Failure"
            return result
        if self.frame_id == 0x340:
            return {"AC_DriverReqTemp": float(value), "AC_RecycleType": 1}
        return {}


class FakeDatabase:
    def __init__(self):
        self.messages = [
            FakeMessageDefinition(0x2F9, "GW_2F9", ("HU_ACDEFAdjustReq", "HU_ACFanAdjustValueReq"), "GW"),
            FakeMessageDefinition(0x330, "AC_330", ("AC_FrontDefrosterSwitch", "AC_SystemFailureFlag"), "AC"),
            FakeMessageDefinition(0x340, "AC_340", ("AC_DriverReqTemp", "AC_RecycleType"), "AC"),
        ]
        self._by_id = {message.frame_id: message for message in self.messages}

    def get_message_by_frame_id(self, frame_id):
        return self._by_id[frame_id]


def frame(timestamp, can_id, value, channel=0, *, error=False):
    return SimpleNamespace(
        timestamp=timestamp,
        arbitration_id=can_id,
        data=bytes([value]),
        dlc=1,
        channel=channel,
        is_error_frame=error,
        is_remote_frame=False,
    )


class FakeReader:
    def __init__(self, messages, on_close=None):
        self.messages = list(messages)
        self.on_close = on_close

    def __iter__(self):
        return iter(self.messages)

    def stop(self):
        if self.on_close:
            self.on_close()


class CanLogServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.logs = self.root / "logs"
        self.project.mkdir()
        self.logs.mkdir()
        self.hvac_log = self.logs / "vehicle-5-13.blf"
        self.other_log = self.logs / "vehicle-7-15.blf"
        self.hvac_log.write_bytes(b"fake-hvac")
        self.other_log.write_bytes(b"fake-other")
        self.reader_calls = []
        self.reader_data = {
            self.hvac_log.name: [
                frame(100.00, 0x100, 0),
                frame(100.01, 0x2F9, 0, 5),
                frame(100.11, 0x330, 0, 5),
                frame(100.21, 0x2F9, 2, 5),
                frame(100.31, 0x330, 1, 5),
                frame(100.41, 0x340, 25, 5),
            ],
            self.other_log.name: [frame(50.0, 0x180, 1, 7), frame(51.0, 0x181, 1, 7)],
        }

        def reader_factory(path):
            self.reader_calls.append(Path(path).name)
            return FakeReader(self.reader_data[Path(path).name])

        self.service = CanLogService(
            base_dir=self.project,
            log_dir=self.logs,
            reader_factory=reader_factory,
            database=FakeDatabase(),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_catalog_discovers_logs_profiles_and_recommends_hvac_source(self):
        catalog = self.service.catalog()

        self.assertEqual(len(catalog["logs"]), 2)
        hvac = next(item for item in catalog["logs"] if item["name"] == self.hvac_log.name)
        self.assertEqual(hvac["total_frames"], 6)
        self.assertEqual(hvac["duration_ms"], 410)
        self.assertEqual(hvac["channels"], ["0", "5"])
        self.assertEqual(hvac["profile_counts"]["front_defrost"], 4)
        self.assertEqual(hvac["profile_counts"]["temperature"], 3)
        self.assertEqual(catalog["default_log_id"], hvac["id"])
        front_defrost = next(item for item in catalog["features"] if item["id"] == "front_defrost")
        self.assertEqual(front_defrost["message_ids"], ["0x2F9", "0x330"])
        self.assertEqual(front_defrost["recommended_log_id"], hvac["id"])

    def test_replay_filters_decodes_and_marks_signal_changes(self):
        catalog = self.service.catalog()
        log_id = next(item["id"] for item in catalog["logs"] if item["name"] == self.hvac_log.name)

        replay = self.service.replay(log_id, "front_defrost")

        self.assertEqual(replay["matched_frames"], 4)
        self.assertEqual(replay["returned_frames"], 4)
        self.assertEqual([item["can_id"] for item in replay["frames"]], ["0x2F9", "0x330", "0x2F9", "0x330"])
        self.assertEqual([item["offset_ms"] for item in replay["frames"]], [0, 100, 200, 300])
        request = replay["frames"][2]
        self.assertEqual(request["message_name"], "GW_2F9")
        self.assertEqual(request["sender"], "HU / GW")
        self.assertEqual(request["direction"], "request")
        self.assertEqual(request["data_hex"], "02")
        self.assertEqual(request["dlc"], 1)
        self.assertEqual(request["channel"], "5")
        self.assertEqual(request["cycle_ms"], 100)
        self.assertEqual(request["receivers"], ["TMS"])
        self.assertEqual(request["sequence"], 3)
        self.assertEqual(request["dbc_source"], "injected DBC")
        self.assertEqual(request["signals"][0]["name"], "HU_ACDEFAdjustReq")
        self.assertEqual(request["signals"][0]["text"], "On")
        self.assertTrue(request["signals"][0]["changed"])
        self.assertNotIn("HU_ACFanAdjustValueReq", [item["name"] for item in request["signals"]])
        self.assertTrue(replay["frames"][3]["changed"])

    def test_replay_decodes_three_real_car_fields_from_0x330_data_byte_two(self):
        values = (0x15, 0x16, 0x1A, 0x2A)
        messages = []
        for index, data_byte in enumerate(values):
            message = frame(100.0 + index / 10.0, 0x330, 0, 5)
            message.data = bytes((0x00, 0x20, data_byte, 0x00, 0x02, 0x3E, 0x40, 0x71))
            message.dlc = 8
            messages.append(message)
        self.reader_data[self.hvac_log.name] = messages
        self.hvac_log.write_bytes(b"real-car-330-sequence")

        catalog = self.service.catalog()
        log_id = next(item["id"] for item in catalog["logs"] if item["name"] == self.hvac_log.name)

        active = self.service.replay(log_id, "unlock_active_ventilation")
        window = self.service.replay(log_id, "unlock_window_ventilation")
        drying = self.service.replay(log_id, "ac_self_drying")

        self.assertEqual([item["signals"][0]["value"] for item in active["frames"]], [1, 2, 2, 2])
        self.assertEqual([item["signals"][0]["value"] for item in window["frames"]], [1, 1, 2, 2])
        self.assertEqual([item["signals"][0]["value"] for item in drying["frames"]], [1, 1, 1, 2])
        self.assertEqual(drying["frames"][-1]["signals"][0]["text"], "开启")
        self.assertEqual(drying["frames"][-1]["dbc_source"], "实车日志逆向映射（0x330 Data[2]）")

    def test_replay_limit_is_bounded_and_keeps_last_frame(self):
        self.reader_data[self.hvac_log.name] = [
            frame(100 + index / 10.0, 0x2F9, index % 3, 5) for index in range(12)
        ]
        # Change the file fingerprint to invalidate the previous catalog cache.
        self.hvac_log.write_bytes(b"fake-hvac-expanded")
        catalog = self.service.catalog()
        log_id = next(item["id"] for item in catalog["logs"] if item["name"] == self.hvac_log.name)

        replay = self.service.replay(log_id, "front_defrost", limit=3)

        self.assertEqual(replay["matched_frames"], 12)
        self.assertLessEqual(replay["returned_frames"], 3)
        self.assertTrue(replay["sampled"])
        self.assertEqual(replay["frames"][-1]["offset_ms"], 1100)

    def test_sampling_keeps_each_periodic_message_id_visible(self):
        catalog = self.service.catalog()
        log_id = next(item["id"] for item in catalog["logs"] if item["name"] == self.hvac_log.name)

        replay = self.service.replay(log_id, "front_defrost", limit=2)

        self.assertEqual(replay["returned_frames"], 2)
        self.assertEqual({item["can_id"] for item in replay["frames"]}, {"0x2F9", "0x330"})
        self.assertEqual(replay["sampling_plan"], {"0x2F9": 1, "0x330": 1})

    def test_catalog_cache_reloads_only_after_file_fingerprint_changes(self):
        self.service.catalog()
        initial_calls = len(self.reader_calls)
        self.service.catalog()
        self.assertEqual(len(self.reader_calls), initial_calls)

        self.hvac_log.write_bytes(b"fingerprint changed")
        os.utime(self.hvac_log, ns=(time.time_ns(), time.time_ns()))
        self.service.catalog()
        self.assertEqual(len(self.reader_calls), initial_calls + 1)

    def test_dbc_directory_discovers_new_files_and_prefers_matching_signals(self):
        dbc_root = self.root / "dbc"
        nested = dbc_root / "nested"
        nested.mkdir(parents=True)
        first_path = dbc_root / "01_body.dbc"
        first_path.write_text("body", encoding="utf-8")
        load_calls = []

        unrelated = FakeMessageDefinition(0x2F9, "BODY_2F9", ("BodySignal",), "BCM")
        matching = FakeMessageDefinition(0x2F9, "GW_2F9", ("HU_ACDEFAdjustReq",), "GW")

        def load_file(path, strict=False):
            del strict
            load_calls.append(Path(path).name)
            messages = [matching] if Path(path).name == "02_tms.DBC" else [unrelated]
            return SimpleNamespace(messages=messages)

        fake_cantools = SimpleNamespace(database=SimpleNamespace(load_file=load_file))
        service = CanLogService(
            base_dir=self.project,
            log_dir=self.logs,
            dbc_path=dbc_root,
            reader_factory=lambda path: FakeReader(self.reader_data[Path(path).name]),
        )

        with patch.object(can_log_module, "_cantools", fake_cantools):
            first_metadata = service._dbc_metadata()
            self.assertEqual(first_metadata["file_count"], 1)
            self.assertEqual(first_metadata["message_count"], 1)

            second_path = nested / "02_tms.DBC"
            second_path.write_text("tms", encoding="utf-8")
            second_metadata = service._dbc_metadata()
            self.assertEqual(second_metadata["file_count"], 2)
            self.assertEqual(second_metadata["message_count"], 2)
            self.assertEqual(second_metadata["duplicate_ids"], ["0x2F9"])

            definition, source = service._message_for_id(0x2F9, ("HU_ACDEFAdjustReq",))
            self.assertEqual(definition.name, "GW_2F9")
            self.assertEqual(source, "02_tms.DBC")
            self.assertEqual(load_calls, ["01_body.dbc", "01_body.dbc", "02_tms.DBC"])

    def test_unknown_ids_profiles_and_bad_limits_are_rejected(self):
        catalog = self.service.catalog()
        valid_id = catalog["default_log_id"]
        with self.assertRaises(CanLogNotFoundError):
            self.service.replay("../vehicle-5-13.blf", "front_defrost")
        with self.assertRaises(CanLogValidationError):
            self.service.replay(valid_id, "unknown")
        for limit in (0, 10001, True, "100"):
            with self.subTest(limit=limit):
                with self.assertRaises(CanLogValidationError):
                    self.service.replay(valid_id, "front_defrost", limit=limit)


if __name__ == "__main__":
    unittest.main()
