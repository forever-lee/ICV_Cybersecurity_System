import pytest

from jetson_edge import start_edge


def test_supervisor_requires_cloud_url(monkeypatch):
    monkeypatch.delenv("CLOUD_HTTP_URL", raising=False)
    monkeypatch.setenv("VEHICLE_INGEST_TOKEN", "secret")
    with pytest.raises(SystemExit, match="CLOUD_HTTP_URL"):
        start_edge.build_components()


def test_supervisor_requires_ingest_token(monkeypatch):
    monkeypatch.setenv("CLOUD_HTTP_URL", "http://192.168.2.10:8001")
    monkeypatch.delenv("VEHICLE_INGEST_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="VEHICLE_INGEST_TOKEN"):
        start_edge.build_components()


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

