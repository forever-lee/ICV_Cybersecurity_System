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

