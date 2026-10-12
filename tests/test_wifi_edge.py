from jetson_edge.WiFi_Module import build_cloud_payload, extract_sender_time_ms


def test_computer_b_timestamp_becomes_latency_source():
    sender_time_ms = 1_700_000_000_000
    edge_time_ms = sender_time_ms + 37
    data = (
        "CHG,DATA,SEQ=0001,SOC=20.5,STATE=CHARGING,TS={}\n".format(
            sender_time_ms
        )
    ).encode("utf-8")

    payload = build_cloud_payload(data, ("192.168.4.201", 62510), edge_time_ms)

    assert payload["sender_time_ms"] == sender_time_ms
    assert payload["source_time_ms"] == sender_time_ms
    assert payload["udp_received_at_ms"] == edge_time_ms
    assert payload["sender_to_edge_latency_ms"] == 37
    assert payload["timestamp_source"] == "computer_b_ts"


def test_packet_without_timestamp_falls_back_to_edge_receive_time():
    payload = build_cloud_payload(
        b"ready", ("192.168.4.201", 62510), 1_700_000_000_100
    )

    assert "sender_time_ms" not in payload
    assert payload["source_time_ms"] == 1_700_000_000_100
    assert payload["timestamp_source"] == "jetson_udp_receive"
    assert extract_sender_time_ms(payload["text"]) is None
