import ast
import asyncio
import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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


def test_sender_has_no_cloud_or_http_dependency():
    source = SENDER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "urllib" not in imported_roots
    assert "requests" not in imported_roots
    for forbidden in (
        "CLOUD_HTTP_URL",
        "VEHICLE_INGEST_TOKEN",
        "cloud_request",
        "register_send_time",
        "control/next",
        "/bluetooth/timing",
    ):
        assert forbidden not in source


def test_sender_keeps_normal_and_abnormal_frame_formats(monkeypatch):
    sender = load_sender(monkeypatch)
    assert sender.normal_frame(1) == "S32K344_HELLO_01"
    assert sender.normal_frame(100) == "S32K344_HELLO_01"
    assert (
        sender.abnormal_frame(3)
        == "S32K344_ABNORMAL_0003;TYPE=BLE_ATTACK;LEVEL=HIGH"
    )


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


def test_web_auxiliary_features_are_reserved_and_inactive():
    app = APP_PATH.read_text(encoding="utf-8")
    index = INDEX_PATH.read_text(encoding="utf-8")
    assert '$("bluetoothLatency").textContent = "待接入"' in app
    assert "sendBluetoothControl" not in app
    assert "/bluetooth/control" not in app
    assert 'id="bluetoothOnButton" type="button" disabled' in index
    assert 'id="bluetoothOffButton" type="button" disabled' in index
    assert 'id="bluetoothLatency">待接入</b>' in index
