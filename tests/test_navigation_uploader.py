import json

from jetson_edge import navigation_uploader


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_read_navigation_accepts_atomic_collector_output(tmp_path):
    path = tmp_path / "navigation_live.json"
    path.write_text(
        json.dumps({
            "longitude": 106.6004,
            "latitude": 29.5339,
            "captured_at_ms": 1234567890,
        }),
        encoding="utf-8",
    )

    sample = navigation_uploader.read_navigation(str(path))

    assert sample["longitude"] == 106.6004
    assert sample["latitude"] == 29.5339


def test_upload_navigation_uses_vehicle_route_and_authorization_header():
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse({"accepted": True, "vehicle_id": "VHC-001"})

    payload = navigation_uploader.build_payload(
        {
            "longitude": 106.6004,
            "latitude": 29.5339,
            "captured_at_ms": 1234567890,
        },
        "edge-session-test",
        1234567000,
    )
    result = navigation_uploader.upload_navigation(
        "https://cloud.example",
        "VHC-001",
        "secret-token",
        payload,
        5,
        opener=opener,
    )

    request = captured["request"]
    assert request.full_url == (
        "https://cloud.example/api/vehicles/VHC-001/navigation"
    )
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert captured["timeout"] == 5
    assert json.loads(request.data.decode("utf-8"))["edge_session_id"] == (
        "edge-session-test"
    )
    assert result["accepted"] is True


def test_ipinfo_location_becomes_degraded_navigation_sample():
    def opener(request, timeout):
        assert request.full_url == "https://ipinfo.io/json"
        assert timeout == 5
        return FakeResponse({
            "ip": "106.83.135.64",
            "city": "Chongqing",
            "region": "Chongqing",
            "loc": "29.5603,106.5577",
        })

    geolocation = navigation_uploader.fetch_ip_geolocation(
        "https://ipinfo.io/json", "", 5, opener=opener
    )
    navigation = navigation_uploader.build_ip_navigation(
        geolocation, 50000, captured_at_ms=1234567890
    )

    assert navigation == {
        "latitude": 29.5603,
        "longitude": 106.5577,
        "coordinate_system": "WGS84",
        "source": "EDGE-IP-GEO",
        "accuracy_m": 50000.0,
        "captured_at_ms": 1234567890,
    }


def test_navigation_freshness_uses_capture_timestamp():
    sample = {"captured_at_ms": 100000}

    assert navigation_uploader.navigation_is_fresh(sample, 15, 114999) is True
    assert navigation_uploader.navigation_is_fresh(sample, 15, 115001) is False
