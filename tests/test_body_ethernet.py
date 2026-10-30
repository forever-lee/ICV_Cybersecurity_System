from jetson_edge.body_protocol import (
    build_time_sync_packet,
    decode_serial_packet,
    ethernet_payload,
)


def body_packet():
    packet = bytearray(16)
    packet[0:4] = bytes((0xA5, 0x5A, 0x01, 0x01))
    packet[4] = 0x01 | 0x20 | 0x40
    packet[5] = 0x04
    packet[6] = 7
    packet[7] = 1
    packet[8:12] = (123).to_bytes(4, byteorder="little")
    packet[12] = 1
    packet[13] = 9
    packet[14] = 1
    checksum = 0
    for value in packet[:15]:
        checksum ^= value
    packet[15] = checksum
    return bytes(packet)


def timestamped_body_packet(sent_at_ms):
    packet = bytearray(body_packet())
    packet.extend(int(sent_at_ms).to_bytes(8, byteorder="little"))
    checksum = 0
    for value in packet:
        checksum ^= value
    packet.append(checksum)
    return bytes(packet)


def acknowledged_body_packet(sent_at_ms, sequence, signal_id, target, result):
    packet = bytearray(body_packet())
    packet.extend(int(sent_at_ms).to_bytes(8, byteorder="little"))
    packet.extend(int(sequence).to_bytes(4, byteorder="little"))
    packet.extend(bytes((signal_id, int(target), result)))
    checksum = 0
    for value in packet:
        checksum ^= value
    packet.append(checksum)
    return bytes(packet)


def raw_can_body_packet(can_id, data):
    packet = bytearray(acknowledged_body_packet(1700000000100, 0, 0, False, 0))
    packet.extend(bytes(8))
    legacy_checksum = 0
    for value in packet[:39]:
        legacy_checksum ^= value
    packet[39] = legacy_checksum
    packet.extend(int(can_id).to_bytes(4, byteorder="little"))
    packet.append(len(data))
    packet.extend(bytes(data))
    packet.extend(bytes(8 - len(data)))
    checksum = 0
    for value in packet:
        checksum ^= value
    packet.append(checksum)
    return bytes(packet)


def test_binary_udp_packet_maps_to_cloud_schema():
    received_at_ms = 1700000000123
    decoded = decode_serial_packet(body_packet())
    payload = ethernet_payload(decoded, ("192.168.4.31", 46000), received_at_ms)
    assert decoded["Door_FL"] is True
    assert decoded["TurnLeft"] is True
    assert decoded["canRxCount"] == 123
    assert payload["source"] == "S32K344-Ethernet-UDP"
    assert payload["link"]["transport"] == "Ethernet/IPv4/UDP"
    assert payload["link"]["board_ip"] == "192.168.4.31"
    assert payload["captured_at_ms"] == received_at_ms
    assert payload["link"]["udp_received_at_ms"] == received_at_ms
    assert payload["link"]["latency_scope"] == "jetson_udp_receive_to_cloud_ingest"
    assert payload["raw_frame"]["can_id"] == "0x351"
    assert payload["raw_frame"]["cycle_ms"] == 100
    assert "192.168.4.31:46000" in payload["interface_path"]


def test_timestamped_packet_uses_board_send_time():
    sent_at_ms = 1700000000100
    received_at_ms = 1700000000123
    decoded = decode_serial_packet(timestamped_body_packet(sent_at_ms))
    payload = ethernet_payload(decoded, ("192.168.4.31", 7100), received_at_ms)
    assert decoded["boardSentAtMs"] == sent_at_ms
    assert payload["captured_at_ms"] == sent_at_ms
    assert payload["link"]["udp_received_at_ms"] == received_at_ms
    assert payload["link"]["timestamp_synchronized"] is True
    assert payload["link"]["latency_scope"] == "board_udp_send_to_cloud_ingest"


def test_time_sync_packet_contains_epoch_ms_and_checksum():
    epoch_ms = 1700000000456
    packet = build_time_sync_packet(epoch_ms)
    assert len(packet) == 13
    assert packet[:4] == bytes((0xA5, 0x5A, 0x01, 0x03))
    assert int.from_bytes(packet[4:12], byteorder="little") == epoch_ms
    checksum = 0
    for value in packet[:12]:
        checksum ^= value
    assert packet[12] == checksum


def test_reported_can_profile_follows_simulation_step():
    packet = bytearray(body_packet())

    packet[7] = 0
    packet[15] = 0
    for value in packet[:15]:
        packet[15] ^= value
    lock_payload = ethernet_payload(
        decode_serial_packet(bytes(packet)), ("192.168.4.31", 7100)
    )
    assert lock_payload["raw_frame"]["can_id"] == "0x352"
    assert lock_payload["raw_frame"]["cycle_ms"] == 500

    packet[7] = 5
    packet[15] = 0
    for value in packet[:15]:
        packet[15] ^= value
    light_payload = ethernet_payload(
        decode_serial_packet(bytes(packet)), ("192.168.4.31", 7100)
    )
    assert light_payload["raw_frame"]["can_id"] == "0x353"
    assert light_payload["raw_frame"]["cycle_ms"] == 50


def test_extended_status_exposes_real_can_control_ack():
    decoded = decode_serial_packet(acknowledged_body_packet(1700000000100, 42, 6, True, 1))
    payload = ethernet_payload(decoded, ("192.168.4.31", 7100), 1700000000123)
    assert payload["control_ack"] == {
        "sequence": 42, "signal_id": 6, "action": "low_beam",
        "target": True, "result_code": 1, "result": "applied",
    }
    assert payload["raw_frame"]["can_id"] == "0x353"
    assert payload["raw_frame"]["send_type"] == "Web事件触发/CAN回读"


def test_raw_can_packet_is_preferred_over_reconstructed_metadata():
    packet = raw_can_body_packet(0x351, bytes.fromhex("01 00 00 00 00 00 05 06"))
    decoded = decode_serial_packet(packet)
    payload = ethernet_payload(decoded, ("192.168.4.31", 7100), 1700000000123)
    assert len(packet) == 54
    assert decoded["rawCanFrame"]["can_id"] == 0x351
    assert decoded["rawCanFrame"]["dlc"] == 8
    assert payload["raw_frame"] == {
        "can_id": "0x351", "dlc": 8,
        "data_hex": "01 00 00 00 00 00 05 06",
        "frame_name": "BodyDoorStatus", "cycle_ms": 100,
        "send_type": "CANoe周期报文",
    }


def test_restored_event_ack_returns_raw_frames_to_periodic_classification():
    decoded = decode_serial_packet(
        raw_can_body_packet(0x180, bytes.fromhex("00 0F 00 01 00 00 05 0B"))
    )
    decoded["controlAck"] = {
        "sequence": 42, "signal_id": 1, "action": "door_fl",
        "target": True, "result_code": 4, "result": "restored",
    }
    payload = ethernet_payload(decoded, ("192.168.4.31", 7100), 1700000000123)
    assert payload["raw_frame"]["can_id"] == "0x180"
    assert payload["raw_frame"]["send_type"] == "K3周期报文"
    assert payload["raw_frame"]["cycle_ms"] == 100


def test_raw_can_packet_rejects_invalid_dlc_and_checksum():
    packet = bytearray(raw_can_body_packet(0x351, bytes.fromhex("01 00 00 00 00 00 05 06")))
    packet[44] = 9
    packet[53] = 0
    for value in packet[:53]:
        packet[53] ^= value
    try:
        decode_serial_packet(bytes(packet))
        assert False, "invalid DLC must be rejected"
    except ValueError as exc:
        assert "DLC" in str(exc)

    packet = bytearray(raw_can_body_packet(0x351, bytes.fromhex("01 00 00 00 00 00 05 06")))
    packet[53] ^= 0xFF
    try:
        decode_serial_packet(bytes(packet))
        assert False, "invalid checksum must be rejected"
    except ValueError as exc:
        assert "checksum" in str(exc)
