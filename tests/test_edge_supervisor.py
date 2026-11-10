import subprocess

from jetson_edge import start_edge


class FakeConnection:
    def close(self):
        pass


def test_supervisor_uses_central_cloud_defaults(monkeypatch):
    monkeypatch.delenv("CLOUD_HTTP_URL", raising=False)
    monkeypatch.delenv("CLOUD_WS_URL", raising=False)
    monkeypatch.delenv("VEHICLE_INGEST_TOKEN", raising=False)
    components = start_edge.build_components()

    assert components
    for component in components:
        assert component.environment["CLOUD_HTTP_URL"] == start_edge.DEFAULT_CLOUD_HTTP_URL
        assert component.environment["CLOUD_WS_URL"] == start_edge.DEFAULT_CLOUD_WS_URL
        assert (
            component.environment["VEHICLE_INGEST_TOKEN"]
            == start_edge.DEFAULT_VEHICLE_INGEST_TOKEN
        )
        assert component.environment["EDGE_SESSION_ID"]
        assert int(component.environment["EDGE_SESSION_STARTED_AT_MS"]) > 0


def test_supervisor_shares_explicit_edge_session_with_all_components(monkeypatch):
    components = start_edge.build_components("edge-session-test", 1234567890)

    assert components
    assert all(
        component.environment["EDGE_SESSION_ID"] == "edge-session-test"
        for component in components
    )
    assert all(
        component.environment["EDGE_SESSION_STARTED_AT_MS"] == "1234567890"
        for component in components
    )


def test_supervisor_allows_ingest_token_override(monkeypatch):
    monkeypatch.setenv("CLOUD_HTTP_URL", "http://192.168.2.10:8001")
    monkeypatch.setenv("VEHICLE_INGEST_TOKEN", "override-secret")

    components = start_edge.build_components()

    assert components
    assert all(
        component.environment["VEHICLE_INGEST_TOKEN"] == "override-secret"
        for component in components
    )


def test_token_is_only_in_child_environment(monkeypatch):
    token = "test-secret-not-in-command"
    monkeypatch.setenv("CLOUD_HTTP_URL", "http://192.168.2.10:8001/")
    monkeypatch.setenv("VEHICLE_INGEST_TOKEN", token)
    component = start_edge.build_components()[0]
    assert component.name == "body"
    assert token not in component.command
    assert "--token" not in component.command
    assert component.environment["VEHICLE_INGEST_TOKEN"] == token
    assert component.command[2].endswith("body_edge_agent.py")


def test_body_address_check_auto_detects_eno1(monkeypatch, capsys):
    monkeypatch.setattr(start_edge.os, "name", "posix")
    monkeypatch.setenv("BODY_ENABLED", "1")
    monkeypatch.delenv("BODY_NETWORK_INTERFACE", raising=False)
    monkeypatch.setattr(start_edge.shutil, "which", lambda name: "/usr/sbin/ip")
    monkeypatch.setattr(
        start_edge.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="2: eno1    inet 192.168.4.100/24 brd 192.168.4.255 scope global eno1\n",
            stderr="",
        ),
    )

    assert start_edge.check_expected_body_address() is True
    assert "eno1 地址正确：192.168.4.100/24" in capsys.readouterr().out


def test_rtsp_endpoint_reachable_uses_url_host_and_port(monkeypatch):
    calls = []

    def fake_create_connection(address, timeout):
        calls.append((address, timeout))
        return FakeConnection()

    monkeypatch.setattr(start_edge.socket, "create_connection", fake_create_connection)

    assert start_edge.rtsp_endpoint_reachable(
        "rtsp://192.168.4.89:8554/main", timeout=0.5
    ) is True
    assert calls == [(('192.168.4.89', 8554), 0.5)]


def test_camera_discovery_selects_only_reachable_camera(monkeypatch, capsys):
    monkeypatch.setenv("CAMERA_DISCOVERY_ATTEMPTS", "2")
    monkeypatch.setenv("CAMERA_DISCOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("CAMERA_RTSP_PROBE_TIMEOUT_SECONDS", "0.25")
    calls = []

    def fake_probe(url, timeout):
        calls.append((url, timeout))
        return "192.168.4.89" in url

    monkeypatch.setattr(start_edge, "rtsp_endpoint_reachable", fake_probe)

    reachable = start_edge.discover_reachable_camera_indexes()

    assert reachable == {2}
    assert calls == [
        ("rtsp://192.168.4.88:8554/main", 0.25),
        ("rtsp://192.168.4.89:8554/main", 0.25),
    ]
    output = capsys.readouterr().out
    assert "[CAMERA PROBE FAIL]" in output
    assert "[CAMERA PROBE OK]" in output


def test_edge_vehicle_id_is_resolved_from_reachable_camera_ip(monkeypatch, capsys):
    monkeypatch.delenv("EDGE_VEHICLE_ID", raising=False)

    first_vehicle_id = start_edge.resolve_edge_vehicle_id({1})
    second_vehicle_id = start_edge.resolve_edge_vehicle_id({2})

    assert first_vehicle_id == "VHC-001"
    assert second_vehicle_id == "VHC-002"
    output = capsys.readouterr().out
    assert "camera_ip=192.168.4.88" in output
    assert "vehicle_id=VHC-001" in output
    assert "camera_ip=192.168.4.89" in output
    assert "vehicle_id=VHC-002" in output


def test_edge_vehicle_id_requires_explicit_value_without_camera(monkeypatch):
    monkeypatch.delenv("EDGE_VEHICLE_ID", raising=False)

    try:
        start_edge.resolve_edge_vehicle_id(set())
    except SystemExit as exc:
        assert "no camera is reachable" in str(exc)
    else:
        raise AssertionError("missing camera identity must stop startup")


def test_edge_vehicle_id_allows_explicit_fallback_without_camera(monkeypatch):
    monkeypatch.setenv("EDGE_VEHICLE_ID", "VHC-002")

    assert start_edge.resolve_edge_vehicle_id(set()) == "VHC-002"


def test_build_components_starts_only_reachable_camera(monkeypatch):
    for name in ("BODY_ENABLED", "GPS_ENABLED", "WIFI_ENABLED", "BLUETOOTH_ENABLED"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("CAMERA_1_ENABLED", "1")
    monkeypatch.setenv("CAMERA_2_ENABLED", "1")

    components = start_edge.build_components(
        "edge-session-test",
        1234567890,
        reachable_camera_indexes={2},
    )

    assert [component.name for component in components] == ["camera_vhc002"]
    assert components[0].environment["VEHICLE_ID"] == "VHC-002"
    assert components[0].environment["RTSP_URL"] == "rtsp://192.168.4.89:8554/main"


def test_camera_identity_is_shared_by_body_wifi_and_bluetooth(monkeypatch):
    monkeypatch.delenv("BODY_VEHICLE_ID", raising=False)
    monkeypatch.delenv("WIFI_VEHICLE_ID", raising=False)
    monkeypatch.delenv("BLUETOOTH_VEHICLE_ID", raising=False)
    monkeypatch.setenv("GPS_ENABLED", "0")
    monkeypatch.setenv("CAMERA_1_ENABLED", "1")
    monkeypatch.setenv("CAMERA_2_ENABLED", "1")

    components = start_edge.build_components(
        "edge-session-test",
        1234567890,
        reachable_camera_indexes={2},
        edge_vehicle_id="VHC-002",
    )

    by_name = {component.name: component for component in components}
    assert set(by_name) == {"body", "wifi", "bluetooth", "camera_vhc002"}
    assert {
        by_name[name].environment["VEHICLE_ID"]
        for name in by_name
    } == {"VHC-002"}


def test_device_connectivity_checks_camera_body_wifi_and_bluetooth(monkeypatch, capsys):
    for name in (
        "CAMERA_1_RTSP_URL",
        "CAMERA_2_RTSP_URL",
        "BODY_BOARD_HOST",
        "WIFI_DEVICE_HOST",
        "BLUETOOTH_DEVICE_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(start_edge.os, "name", "posix")
    monkeypatch.setattr(start_edge.shutil, "which", lambda name: "/usr/bin/ping")
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0 if command[-1] != "192.168.4.20" else 1)

    monkeypatch.setattr(start_edge.subprocess, "run", fake_run)

    results = start_edge.check_device_connectivity()

    assert [command[-1] for command in commands] == [
        "192.168.4.88",
        "192.168.4.89",
        "192.168.4.31",
        "192.168.4.201",
        "192.168.4.20",
    ]
    assert results["192.168.4.31"] is True
    assert results["192.168.4.20"] is False
    output = capsys.readouterr().out
    assert "[PING OK]" in output
    assert "[PING FAIL]" in output
    assert "可达 4/5" in output

