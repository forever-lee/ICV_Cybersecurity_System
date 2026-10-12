import subprocess

from jetson_edge import start_edge


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

