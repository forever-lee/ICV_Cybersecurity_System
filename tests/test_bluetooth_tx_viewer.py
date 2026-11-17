from bluetooth_tx_viewer import new_confirmed_tx


def test_viewer_shows_only_new_confirmed_tx_in_order():
    snapshot = {"bluetooth": {"traffic_history": [
        {"direction": "TX", "command_id": 3, "received_at_ms": 3000,
         "text": "第三条"},
        {"direction": "RX", "received_at_ms": 2500, "text": "遥测"},
        {"direction": "TX", "command_id": 2, "received_at_ms": 2000,
         "text": "第二条"},
        {"direction": "TX", "command_id": 1, "received_at_ms": 1000,
         "text": "第一条"},
    ]}}
    seen = set()
    assert [item["text"] for item in new_confirmed_tx(snapshot, seen)] == [
        "第一条", "第二条", "第三条",
    ]
    assert new_confirmed_tx(snapshot, seen) == []
    snapshot["bluetooth"]["traffic_history"].insert(0, {
        "direction": "TX", "command_id": 4, "received_at_ms": 4000,
        "text": "新内容",
    })
    assert [item["text"] for item in new_confirmed_tx(snapshot, seen)] == ["新内容"]
