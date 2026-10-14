from jetson_edge.body_protocol import (
    build_serial_command,
    build_body_event_command,
    decode_serial_packet,
)


def body_packet(transmission_enabled):
    packet = bytearray(16)
    packet[0:4] = bytes((0xA5, 0x5A, 0x01, 0x01))
    packet[12] = 0x01
    packet[14] = 0x01 if transmission_enabled else 0x00
    checksum = 0
    for value in packet[:15]:
        checksum ^= value
    packet[15] = checksum
    return bytes(packet)


def test_start_command_packet():
    assert build_serial_command("start") == bytes((0xA5, 0x5A, 0x01, 0x02, 0x01, 0xFD))


def test_stop_command_packet():
    assert build_serial_command("stop") == bytes((0xA5, 0x5A, 0x01, 0x02, 0x00, 0xFC))


def test_status_packet_reports_transmission_state():
    assert decode_serial_packet(body_packet(True))["transmissionEnabled"] is True
    assert decode_serial_packet(body_packet(False))["transmissionEnabled"] is False


def test_body_event_command_contains_sequence_signal_target_and_checksum():
    packet = build_body_event_command(0x12345678, "low_beam", True, 5)
    assert len(packet) == 12
    assert packet[:4] == bytes((0xA5, 0x5A, 0x01, 0x04))
    assert packet[4:8] == bytes((0x78, 0x56, 0x34, 0x12))
    assert packet[8:11] == bytes((6, 1, 5))
    checksum = 0
    for value in packet[:11]:
        checksum ^= value
    assert packet[11] == checksum
