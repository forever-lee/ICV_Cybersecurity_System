"""Hardware clock exchange, telemetry mapping, and cloud latency regression tests."""

import asyncio
import importlib.util
import sys
import types
from collections import OrderedDict
from pathlib import Path

import pytest

import server
from jetson_edge.bluetooth_edge_agent import (
    CloudClockMonitor, decode_datagram, decode_sync_datagram,
)
from jetson_edge.bluetooth_time_sync import (
    ClockSynchronizer, make_request, make_response, parse_request,
    parse_response, parse_timed_telemetry,
)


ROOT = Path(__file__).resolve().parents[1]


def test_sync_protocol_crc_and_source_validation():
    request = make_request(17, 1792000000000)
    assert parse_request(request) == {"sync_id": 17, "t1_ms": 1792000000000}
    response = make_response(17, 1792000000000, 90000000, 90001000)
    assert parse_response(response)["u3_us"] == 90001000
    assert decode_sync_datagram(response[:-1].encode(), ("192.168.4.20", 5000),
                                "192.168.4.20")["sync_id"] == 17
    with pytest.raises(ValueError):
        parse_request(request.replace("REQ", "REX"))
    with pytest.raises(ValueError):
        parse_response(response.replace("90001000", "90001001"))
    with pytest.raises(ValueError):
        decode_sync_datagram(response.encode(), ("192.168.4.21", 5000), "192.168.4.20")


def test_compact_sync_round_trip_and_crc_rejection():
    request = make_request(17, 1792000000000, version=2)
    response = make_response(17, 1792000000000, 90000000, 90001000, version=2)
    assert len(request.encode("ascii")) <= 40
    assert len(response.encode("ascii")) <= 40
    assert "+" not in request + response
    assert parse_request(request) == {"sync_id": 17, "t1_ms": 1792000000000}
    assert parse_response(response) == {"sync_id": 17, "t1_ms": None,
                                        "u2_us": 90000000, "u3_us": 90001000}
    assert decode_sync_datagram(response[:-1].encode(), ("192.168.4.20", 5000),
                                "192.168.4.20")["u3_us"] == 90001000
    with pytest.raises(ValueError):
        parse_request(request[:12] + ("A" if request[12] != "A" else "B") + request[13:])
    with pytest.raises(ValueError):
        parse_response(response[:12] + ("A" if response[12] != "A" else "B") + response[13:])


def test_clock_mapping_rejects_expiry_wrong_id_and_large_rtt():
    sync = ClockSynchronizer()
    outbound = sync.begin(1792000000000, 100000.0)
    assert outbound.startswith("BTSYNC|2|Q|")
    request = parse_request(outbound)
    response = parse_response(make_response(request["sync_id"], request["t1_ms"],
                                            90000000, 90001000))
    sample = sync.accept(response, 1792000000100, 100100.0)
    assert sample is not None
    assert sample["rtt_ms"] == 99
    mapped = sync.map_tick(90002000, request["sync_id"], 1792000000200, 100200.0)
    assert mapped["latency_quality"] == "synced"
    assert mapped["source_time_ms"] == 1792000000051.5
    fallback = sync.map_tick(90002000, request["sync_id"] + 1,
                             1792000000200, 100200)
    assert fallback["latency_quality"] == "synced"
    assert fallback["mapping_sync_id"] == request["sync_id"]
    assert fallback["sync_id"] == request["sync_id"] + 1
    assert sync.map_tick(90002000, 0, 1792000000200, 100200) is None
    assert sync.map_tick(90002000, request["sync_id"], 1792000031000, 131000) is None
    assert sync.map_tick(59000000, request["sync_id"], 1792000000200, 100200) is None

    next_request = parse_request(sync.begin(1792000001000, 101000.0))
    slow = parse_response(make_response(next_request["sync_id"], next_request["t1_ms"],
                                        91000000, 91001000))
    assert sync.accept(slow, 1792000001600, 101600.0) is None


def test_clock_mapping_accepts_compact_response_using_pending_t1():
    sync = ClockSynchronizer()
    request = parse_request(sync.begin(1792000000000, 100000.0))
    response = parse_response(make_response(
        request["sync_id"], request["t1_ms"], 90000000, 90001000, version=2))
    sample = sync.accept(response, 1792000000100, 100100.0)
    assert sample is not None
    assert sample["rtt_ms"] == 99


def test_clock_rejection_reports_measurable_reason_without_accepting_bad_sample():
    sync = ClockSynchronizer()
    request = parse_request(sync.begin(1792000000000, 100000.0))
    response = parse_response(make_response(request["sync_id"], request["t1_ms"],
                                            90000000, 90001000))
    sample, reason = sync.accept_detailed(response, 1792000000600, 100600.0)
    assert sample is None
    assert "RTT=599.0 ms" in reason
    assert "500.0 ms" in reason
    sample, reason = sync.accept_detailed(response, 1792000000700, 100700.0)
    assert sample is None
    assert "没有待确认请求" in reason


def test_timed_telemetry_is_stripped_for_display_but_metadata_is_preserved():
    frame = "S32K344_HELLO_07;SEQ=42;TICK_US=90002000;SYNC_ID=17#"
    payload = decode_datagram(frame.encode(), ("192.168.4.20", 5000))[0]
    assert payload["text"] == "S32K344_HELLO_07"
    assert payload["sender_sequence"] == 42
    assert payload["source_tick_us"] == 90002000
    assert payload["sync_id"] == 17
    assert parse_timed_telemetry("S32K344_HELLO_07;SEQ=42;TICK_US=x;SYNC_ID=17") is None


def test_crc_protected_telemetry_is_accepted_only_when_intact(monkeypatch):
    monkeypatch.setenv("BLE_ADDRESS", "test-address")
    fake_bleak = types.ModuleType("bleak")
    fake_bleak.BleakClient = object
    monkeypatch.setitem(sys.modules, "bleak", fake_bleak)
    spec = importlib.util.spec_from_file_location(
        "sender_crc_test", ROOT / "bluetooth_gateway" / "ble_s32_sender.py")
    sender = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sender)
    frame = sender.timed_telemetry("S32K344_HELLO_07", 42, 90002000, 17)
    payloads = decode_datagram(frame.encode(), ("192.168.4.20", 5000))
    assert len(payloads) == 1
    assert payloads[0]["sender_sequence"] == 42
    assert decode_datagram(frame.replace("HELLO_07", "HELLO_08").encode(),
                           ("192.168.4.20", 5000)) == []


def test_cloud_displays_only_valid_hardware_mapping():
    state = server.VehicleState("VHC-001")
    received = server.now_ms()
    payload = {
        "text": "S32K344_HELLO_07", "source": "S32K344-ETH@192.168.4.20:5000",
        "source_time_ms": received - 28, "udp_received_at_ms": received - 10,
        "sender_sequence": 42, "source_tick_us": 90002000, "sync_id": 17,
        "sync_rtt_ms": 39.5, "clock_offset_ms": 1791999910000,
        "cloud_clock_offset_ms": 0, "cloud_clock_rtt_ms": 12,
        "cloud_clock_age_ms": 100,
        "latency_quality": "synced",
    }
    record = server.record_bluetooth_packet(state, payload)
    assert record["latency_quality"] == "synced"
    assert record["latency_ms"] is not None
    assert 25 <= record["latency_ms"] <= 500
    assert record["source_to_jetson_ms"] == 18
    assert record["clock_unsynced"] is False

    test_record = server.record_bluetooth_packet(state, payload, is_test=True)
    assert test_record["latency_ms"] is None
    assert test_record["latency_quality"] == "awaiting_sync"
    invalid = dict(payload, source_time_ms=received + 1000)
    bad_record = server.record_bluetooth_packet(state, invalid)
    assert bad_record["latency_ms"] is None
    assert bad_record["latency_quality"] == "invalid_time"

    cloud_clock_skew = dict(payload, source_time_ms=received - 120030,
                            udp_received_at_ms=received - 120000)
    edge_record = server.record_bluetooth_packet(state, cloud_clock_skew)
    assert edge_record["latency_ms"] is None
    assert edge_record["latency_quality"] == "invalid_time"
    assert edge_record["latency_reason"] == "cloud_clock_skew_or_upload_delay"
    assert edge_record["source_to_jetson_ms"] == 30

    missing_cloud_clock = dict(payload)
    missing_cloud_clock.pop("cloud_clock_offset_ms")
    pending_record = server.record_bluetooth_packet(state, missing_cloud_clock)
    assert pending_record["latency_ms"] is None
    assert pending_record["source_to_jetson_ms"] == 18
    assert pending_record["latency_reason"] == "awaiting_cloud_clock"

    # Jetson is 700 ms ahead of cloud: raw subtraction is negative, but a
    # measured clock-domain conversion recovers the true 50 ms path latency.
    offset_record = server.record_bluetooth_packet(state, dict(
        payload, source_time_ms=server.now_ms() + 650,
        udp_received_at_ms=server.now_ms() + 680,
        cloud_clock_offset_ms=-700,
    ))
    assert offset_record["latency_quality"] == "synced"
    assert 40 <= offset_record["latency_ms"] <= 80
    assert 10 <= offset_record["jetson_to_cloud_ms"] <= 40


def test_cloud_clock_probe_rejects_slow_or_jumped_samples():
    monitor = CloudClockMonitor("http://cloud.invalid", None, 1)
    assert monitor.accept(1000, 1020, 1040, 2000, 2020)
    sample = monitor.latest(2025)
    assert sample["cloud_clock_offset_ms"] == -30
    assert sample["cloud_clock_rtt_ms"] == 20
    assert sample["cloud_clock_age_ms"] == 5
    assert not monitor.accept(1000, 1020, 1250, 2000, 2230)
    assert not monitor.accept(1000, 1020, 1110, 2000, 2010)
    assert monitor.latest(32021) is None


def test_sender_answers_sync_notify_while_business_send_is_off(monkeypatch):
    monkeypatch.setenv("BLE_ADDRESS", "test-address")
    fake_bleak = types.ModuleType("bleak")
    fake_bleak.BleakClient = object
    monkeypatch.setitem(sys.modules, "bleak", fake_bleak)
    spec = importlib.util.spec_from_file_location(
        "sender_sync_test", ROOT / "bluetooth_gateway" / "ble_s32_sender.py")
    sender = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sender)
    request = make_request(7, 1792000000000).encode("ascii")
    assert sender.parse_sync_request(request) == (7, 1792000000000)
    assert parse_response(sender.make_sync_response(
        7, 1792000000000, 90000000, 90001000))["u3_us"] == 90001000
    state = {"enabled": False, "sync_id": 0, "write_lock": asyncio.Lock(),
             "lock": asyncio.Lock(), "applied_commands": OrderedDict(),
             "acknowledged_commands": OrderedDict()}

    class Characteristic:
        max_write_without_response_size = 20
        properties = ["write-without-response"]

    class Client:
        is_connected = True

        def __init__(self):
            self.writes = []

        async def write_gatt_char(self, _characteristic, data, response):
            self.writes.append(bytes(data))

    async def run():
        client = Client()
        incoming = asyncio.Queue()
        task = asyncio.create_task(sender.notify_control_loop(
            state, client, Characteristic(), incoming))
        await incoming.put((request[:12], 90000000))
        await incoming.put((request[12:], 90001000))
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return client.writes

    writes = asyncio.run(run())
    response = parse_response(b"".join(writes))
    assert response["u2_us"] == 90000000
    assert response["sync_id"] == 7
    assert state["enabled"] is False
    assert state["sync_id"] == 7


def test_sender_answers_compact_sync_notify_while_business_send_is_off(monkeypatch):
    monkeypatch.setenv("BLE_ADDRESS", "test-address")
    fake_bleak = types.ModuleType("bleak")
    fake_bleak.BleakClient = object
    monkeypatch.setitem(sys.modules, "bleak", fake_bleak)
    spec = importlib.util.spec_from_file_location(
        "sender_compact_sync_test", ROOT / "bluetooth_gateway" / "ble_s32_sender.py")
    sender = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sender)
    request = make_request(7, 1792000000000, version=2).encode("ascii")
    assert sender.parse_sync_request(request) == (7, 1792000000000)
    state = {"enabled": False, "sync_id": 0, "write_lock": asyncio.Lock(),
             "lock": asyncio.Lock(), "applied_commands": OrderedDict(),
             "acknowledged_commands": OrderedDict()}

    class Characteristic:
        max_write_without_response_size = 20
        properties = ["write-without-response"]

    class Client:
        is_connected = True

        def __init__(self):
            self.writes = []

        async def write_gatt_char(self, _characteristic, data, response):
            self.writes.append(bytes(data))

    async def run():
        client = Client()
        incoming = asyncio.Queue()
        task = asyncio.create_task(sender.notify_control_loop(
            state, client, Characteristic(), incoming))
        started_us = sender.monotonic_us()
        await incoming.put((request[:12], started_us))
        await incoming.put((request[12:], started_us + 1000))
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return client.writes, started_us

    writes, started_us = asyncio.run(run())
    assert len(writes) == 2
    response = parse_response(b"".join(writes))
    assert response["u2_us"] == started_us
    assert response["sync_id"] == 7
    assert state["enabled"] is False
    assert state["sync_id"] == 7
