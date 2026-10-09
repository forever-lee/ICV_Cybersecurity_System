"""Binary frame protocol shared by the vehicle agent and cloud service."""

import struct
import time


MAGIC = b"VCS1"
VERSION = 1
# magic, sequence, captured_at_ms, sent_at_ms, width, height, jpeg_quality
FRAME_HEADER = struct.Struct("!4sIQQHHB")
HEADER_SIZE = FRAME_HEADER.size

# Fragmented MP4 transport.  The payload is either an ISO-BMFF initialization
# segment (ftyp+moov) or a self-contained media fragment (moof+mdat).
FMP4_MAGIC = b"FMP4"
FMP4_KIND_INIT = 0
FMP4_KIND_MEDIA = 1
FMP4_HEADER = struct.Struct("!4sBIQ")
FMP4_HEADER_SIZE = FMP4_HEADER.size


def now_ms():
    return int(time.time() * 1000)


def pack_frame(sequence, captured_at_ms, sent_at_ms, width, height, quality, jpeg):
    header = FRAME_HEADER.pack(
        MAGIC,
        int(sequence) & 0xFFFFFFFF,
        int(captured_at_ms),
        int(sent_at_ms),
        int(width),
        int(height),
        int(quality),
    )
    return header + jpeg


def unpack_frame(packet):
    if len(packet) <= HEADER_SIZE:
        raise ValueError("frame packet is too short")
    magic, sequence, captured_at_ms, sent_at_ms, width, height, quality = FRAME_HEADER.unpack_from(packet)
    if magic != MAGIC:
        raise ValueError("invalid frame magic")
    return {
        "sequence": sequence,
        "captured_at_ms": captured_at_ms,
        "sent_at_ms": sent_at_ms,
        "width": width,
        "height": height,
        "quality": quality,
        "jpeg": packet[HEADER_SIZE:],
    }


def pack_fmp4(kind, sequence, created_at_ms, payload):
    if kind not in (FMP4_KIND_INIT, FMP4_KIND_MEDIA):
        raise ValueError("invalid fMP4 packet kind")
    return FMP4_HEADER.pack(
        FMP4_MAGIC,
        int(kind),
        int(sequence) & 0xFFFFFFFF,
        int(created_at_ms),
    ) + payload


def unpack_fmp4(packet):
    if len(packet) <= FMP4_HEADER_SIZE:
        raise ValueError("fMP4 packet is too short")
    magic, kind, sequence, created_at_ms = FMP4_HEADER.unpack_from(packet)
    if magic != FMP4_MAGIC or kind not in (FMP4_KIND_INIT, FMP4_KIND_MEDIA):
        raise ValueError("invalid fMP4 packet")
    return {
        "kind": kind,
        "sequence": sequence,
        "created_at_ms": created_at_ms,
        "payload": packet[FMP4_HEADER_SIZE:],
    }
