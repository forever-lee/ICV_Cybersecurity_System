import ast
import asyncio
import binascii
import importlib.util
import sys
import types
from pathlib import Path
from collections import OrderedDict


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SENDER_PATH = ROOT / "bluetooth_gateway" / "ble_s32_sender.py"
APP_PATH = ROOT / "static" / "app.js"
INDEX_PATH = ROOT / "static" / "index.html"


def load_sender(monkeypatch):
    fake_bleak = types.ModuleType("bleak")
    fake_bleak.BleakClient = object
    monkeypatch.setitem(sys.modules, "bleak", fake_bleak)
    monkeypatch.setenv("BLE_ADDRESS", "00:11:22:33:44:55")
    spec = importlib.util.spec_from_file_location("isolated_ble_sender", SENDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sender_has_no_direct_cloud_control_path():
    source = SENDER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom)
        else alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "urllib" not in imported_roots
    assert "requests" not in imported_roots
    for forbidden in (
        "VEHICLE_INGEST_TOKEN",
        "register_send_time",
        "/bluetooth/timing",
        "127.0.0.1",
        "local/next",
        "LOCAL_CLOUD_URL",
    ):
        assert forbidden not in source
    assert "start_notify" in source


def test_sender_keeps_normal_and_abnormal_frame_formats(monkeypatch):
    sender = load_sender(monkeypatch)
    assert sender.normal_frame(1) == "S32K344_HELLO_01"
    assert sender.normal_frame(100) == "S32K344_HELLO_01"
    assert (
        sender.abnormal_frame(3)
        == "S32K344_ABNORMAL_0003;TYPE=BLE_ATTACK;LEVEL=HIGH"
    )


def test_sender_replaces_one_random_slot_per_ten_without_extra_frame(monkeypatch):
    monkeypatch.setenv("BLUETOOTH_ATTACK_MODE", "replace")
    sender = load_sender(monkeypatch)
    choices = iter((5, 6))
    monkeypatch.setattr(sender.random, "choice", lambda _slots: next(choices))
    state = {"abnormal_counter": 1}
    frames = [sender.next_telemetry_text(state, counter) for counter in range(1, 21)]
    assert len(frames) == 20
    assert frames[4] == sender.abnormal_frame(1)
    assert frames[15] == sender.abnormal_frame(2)
    assert all(
        frame == sender.normal_frame(counter)
        for counter, frame in enumerate(frames, 1)
        if counter not in (5, 16)
    )


def test_sender_can_disable_simulated_attacks_for_baseline(monkeypatch):
    monkeypatch.setenv("BLUETOOTH_ATTACK_MODE", "replace")
    monkeypatch.setenv("BLUETOOTH_ABNORMAL_EVERY", "0")
    sender = load_sender(monkeypatch)
    state = {"abnormal_counter": 1}
    assert [sender.next_telemetry_text(state, counter) for counter in range(1, 11)] == [
        sender.normal_frame(counter) for counter in range(1, 11)
    ]
    assert state["abnormal_counter"] == 1


def test_sender_still_writes_hash_terminated_ble_chunks(monkeypatch):
    sender = load_sender(monkeypatch)

    class Characteristic:
        max_write_without_response_size = 7
        properties = ["write-without-response"]

    class Client:
        def __init__(self):
            self.writes = []

        async def write_gatt_char(self, characteristic, data, response):
            self.writes.append((characteristic, bytes(data), response))

    client = Client()
    characteristic = Characteristic()
    asyncio.run(sender.write_frame(client, characteristic, "S32K344_HELLO_01"))
    assert b"".join(data for _characteristic, data, _response in client.writes) == (
        b"S32K344_HELLO_01#"
    )
    assert all(characteristic is item for item, _data, _response in client.writes)
    assert all(response is False for _item, _data, response in client.writes)


def test_data_ack_is_crc_checked_and_retry_reuses_the_same_sequence(monkeypatch):
    monkeypatch.setenv("BLUETOOTH_REQUIRE_DATA_ACK", "1")
    sender = load_sender(monkeypatch)
    monkeypatch.setattr(sender, "DATA_ACK_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(sender, "DATA_ACK_ATTEMPTS", 2)
    state = {"sender_sequence": 0, "sync_id": 5, "enabled": True,
             "write_lock": asyncio.Lock(),
             "pending_frame": None, "pending_data_ack": None}
    pending = sender.pending_telemetry(state, "S32K344_HELLO_01", 1)
    body = "BTDATA|1|{}|ACK|{}".format(pending["sequence"], pending["crc"])
    ack = "{}|{:04X}#".format(body, binascii.crc_hqx(body.encode("ascii"), 0xFFFF))
    assert sender.parse_data_ack(ack.encode()) == (pending["sequence"], pending["crc"])
    try:
        sender.parse_data_ack(ack.replace("ACK", "BAD").encode())
    except ValueError:
        pass
    else:
        raise AssertionError("corrupted data ACK was accepted")
    sent = []

    async def fake_write(_client, _characteristic, text):
        sent.append(text)
        if len(sent) == 2:
            state["pending_data_ack"][2].set_result(None)

    monkeypatch.setattr(sender, "write_frame", fake_write)
    asyncio.run(sender.send_pending_telemetry(state, object(), object()))
    assert sent == [pending["text"], pending["text"]]
    assert state["pending_frame"] is None


def test_unacknowledged_frame_is_retained_for_reconnect(monkeypatch):
    monkeypatch.setenv("BLUETOOTH_REQUIRE_DATA_ACK", "1")
    sender = load_sender(monkeypatch)
    monkeypatch.setattr(sender, "DATA_ACK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(sender, "DATA_ACK_ATTEMPTS", 1)
    state = {"sender_sequence": 0, "sync_id": 5, "enabled": True,
             "write_lock": asyncio.Lock(),
             "pending_frame": None, "pending_data_ack": None}
    pending = sender.pending_telemetry(state, "S32K344_HELLO_02", 2)

    async def fake_write(_client, _characteristic, _text):
        return None

    monkeypatch.setattr(sender, "write_frame", fake_write)
    try:
        asyncio.run(sender.send_pending_telemetry(state, object(), object()))
    except RuntimeError:
        pass
    else:
        raise AssertionError("missing hardware ACK did not stop the session")
    assert state["pending_frame"] is pending


def test_off_interrupts_ack_wait_without_losing_pending_frame(monkeypatch):
    monkeypatch.setenv("BLUETOOTH_REQUIRE_DATA_ACK", "1")
    sender = load_sender(monkeypatch)
    state = {"enabled": True, "revision": 0, "applied_commands": OrderedDict(),
             "lock": asyncio.Lock(), "sender_sequence": 0, "sync_id": 5,
             "write_lock": asyncio.Lock(), "pending_frame": None,
             "pending_data_ack": None}
    pending = sender.pending_telemetry(state, "S32K344_HELLO_03", 3)

    async def fake_write(_client, _characteristic, _text):
        await sender.apply_command(state, "off")

    monkeypatch.setattr(sender, "write_frame", fake_write)
    assert asyncio.run(sender.send_pending_telemetry(state, object(), object())) is None
    assert state["pending_frame"] is pending


def test_default_sender_does_not_block_or_reconnect_on_missing_data_ack(monkeypatch):
    monkeypatch.delenv("BLUETOOTH_REQUIRE_DATA_ACK", raising=False)
    sender = load_sender(monkeypatch)
    assert sender.REQUIRE_DATA_ACK is False
    state = {"sender_sequence": 0, "sync_id": 5, "enabled": True,
             "write_lock": asyncio.Lock(), "pending_frame": None,
             "pending_data_ack": None}
    pending = sender.pending_telemetry(state, "S32K344_HELLO_01", 1)
    sent = []

    async def fake_write(_client, _characteristic, text):
        sent.append(text)

    monkeypatch.setattr(sender, "write_frame", fake_write)
    completed = asyncio.run(sender.send_pending_telemetry(state, object(), object()))
    assert completed is pending
    assert sent == [pending["text"]]
    assert state["pending_frame"] is None
    assert state["pending_data_ack"] is None
    assert (pending["sequence"], pending["crc"]) in state["recent_data_acks"]


def test_fast_sender_retries_missing_k3_ack_without_blocking_next_frame(monkeypatch):
    monkeypatch.delenv("BLUETOOTH_REQUIRE_DATA_ACK", raising=False)
    sender = load_sender(monkeypatch)
    monkeypatch.setattr(sender, "DATA_ACK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(sender, "ASYNC_ACK_POLL_SECONDS", 0.005)
    state = {"sender_sequence": 0, "sync_id": 5, "enabled": True,
             "write_lock": asyncio.Lock(), "pending_frame": None,
             "pending_data_ack": None, "recent_data_acks": OrderedDict()}
    sent = []

    class Client:
        is_connected = True

    async def fake_write(_client, _characteristic, text):
        sent.append(text)

    async def exercise():
        first = sender.pending_telemetry(state, "S32K344_HELLO_01", 1)
        await sender.send_pending_telemetry(state, Client(), object())
        second = sender.pending_telemetry(state, "S32K344_HELLO_02", 2)
        await sender.send_pending_telemetry(state, Client(), object())
        task = asyncio.create_task(sender.retry_unconfirmed_telemetry(
            state, Client(), object()))
        await asyncio.sleep(0.08)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return first, second

    monkeypatch.setattr(sender, "write_frame", fake_write)
    first, second = asyncio.run(exercise())
    assert sent.count(first["text"]) == sender.DATA_ACK_ATTEMPTS
    assert sent.count(second["text"]) == sender.DATA_ACK_ATTEMPTS
    assert state["pending_frame"] is None
    assert state["recent_data_acks"] == OrderedDict()


def test_fast_sender_accounts_for_late_k3_ack(monkeypatch):
    monkeypatch.delenv("BLUETOOTH_REQUIRE_DATA_ACK", raising=False)
    sender = load_sender(monkeypatch)
    state = {"sender_sequence": 0, "sync_id": 5, "enabled": True,
             "write_lock": asyncio.Lock(), "pending_frame": None,
             "pending_data_ack": None}
    pending = sender.pending_telemetry(state, "S32K344_HELLO_01", 1)
    body = "BTDATA|1|{}|ACK|{}".format(pending["sequence"], pending["crc"])
    ack = "{}|{:04X}#".format(body, binascii.crc_hqx(body.encode("ascii"), 0xFFFF))

    class Client:
        is_connected = True

    async def fake_write(_client, _characteristic, _text):
        return None

    async def exercise():
        await sender.send_pending_telemetry(state, Client(), object())
        incoming = asyncio.Queue()
        task = asyncio.create_task(sender.notify_control_loop(
            state, Client(), object(), incoming))
        await incoming.put(ack.encode("ascii"))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    monkeypatch.setattr(sender, "write_frame", fake_write)
    asyncio.run(exercise())
    assert (pending["sequence"], pending["crc"]) not in state["recent_data_acks"]


def test_sender_cmd_and_hardware_commands_share_one_state(monkeypatch):
    sender = load_sender(monkeypatch)
    state = {
        "enabled": True, "revision": 0,
        "ble_connected": False, "applied_commands": OrderedDict(),
        "lock": asyncio.Lock(),
    }

    async def exercise():
        assert await sender.apply_command(state, "off") == 1
        assert not state["enabled"]
        key = (12, "NONCE")
        assert await sender.apply_command(state, "on", key) == 2
        assert state["enabled"]
        assert await sender.apply_command(state, "on", key) == 2
        assert state["revision"] == 2

    asyncio.run(exercise())


def test_notify_framing_and_ack_use_shared_crc_protocol(monkeypatch):
    from jetson_edge.bluetooth_control_protocol import make_ack, make_command

    sender = load_sender(monkeypatch)
    command = make_command(12, "off", 1791950000123, "A1B2C3D4E5F60708").encode("ascii")
    framer = sender.NotifyFramer()
    assert framer.feed(command[:9]) == []
    assert framer.feed(command[9:]) == [command]
    assert sender.parse_control_frame(command)["action"] == "off"
    assert sender.make_ack_frame(12, "off", "A1B2C3D4E5F60708") == make_ack(
        12, "applied", "off", "A1B2C3D4E5F60708"
    )
    corrupted = command.replace(b"OFF", b"ONN")
    try:
        sender.parse_control_frame(corrupted)
    except ValueError:
        pass
    else:
        raise AssertionError("corrupted control frame was accepted")


def test_duplicate_notify_command_reuses_ack_without_reapplying(monkeypatch):
    from jetson_edge.bluetooth_control_protocol import make_command

    sender = load_sender(monkeypatch)
    frame = make_command(8, "off", 1791950000123, "A1B2C3D4E5F60708").encode("ascii")
    state = {
        "enabled": True, "revision": 0, "applied_commands": OrderedDict(),
        "acknowledged_commands": OrderedDict(), "lock": asyncio.Lock(),
        "write_lock": asyncio.Lock(),
    }

    class Characteristic:
        max_write_without_response_size = 20
        properties = ["write-without-response"]

    class Client:
        is_connected = True

        def __init__(self):
            self.writes = []

        async def write_gatt_char(self, _characteristic, data, response):
            self.writes.append(bytes(data))

    async def exercise():
        client = Client()
        incoming = asyncio.Queue()
        task = asyncio.create_task(sender.notify_control_loop(
            state, client, Characteristic(), incoming
        ))
        await incoming.put(frame[:10])
        await incoming.put(frame[10:])
        await asyncio.sleep(0.4)
        assert state["enabled"] is False
        assert state["revision"] == 1
        first_ack = b"".join(client.writes)
        client.writes.clear()
        await incoming.put(frame)
        await asyncio.sleep(0.4)
        assert state["revision"] == 1
        assert b"".join(client.writes) == first_ack
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_web_control_waits_for_sender_ack_and_latency_remains_unsynchronised():
    app = APP_PATH.read_text(encoding="utf-8")
    index = INDEX_PATH.read_text(encoding="utf-8")
    assert '$("bluetoothLatency").textContent = "待时间同步"' in app
    assert "sendBluetoothControl" in app
    assert "/bluetooth/control" in app
    assert 'status === "applied"' in app
    assert 'id="bluetoothOnButton" type="button" disabled' in index
    assert 'id="bluetoothOffButton" type="button" disabled' in index
    assert 'id="bluetoothLatency">待时间同步</b>' in index
    assert 'ms（至 Jetson）' in app
    assert 'ms（端到端）' in app
