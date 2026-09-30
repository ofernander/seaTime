"""NMEA 2000 message decoding, independent of how the frames arrive.

NMEA 2000 is J1939-style CAN: a 29-bit identifier carrying priority, PGN
and source address, plus up to 8 data bytes. Transports (SocketCAN today)
hand us the identifier and data; this module turns them into time readings.
"""

import struct
from typing import Optional


PGN_SYSTEM_TIME = 126992

# 126992's "Source" field. Only GNSS-derived time is worth feeding chrony;
# other values (radio station, local crystal...) mean a device such as a
# chartplotter is echoing its own clock.
GNSS_TIME_SOURCES = {0: "GPS", 1: "GLONASS"}

# Time of day in 126992 is in units of 0.0001s; anything at or past a full
# day is invalid, as are the "not available" / reserved values at the top
# of each field's range.
_TENTH_MS_PER_DAY = 86400 * 10000
_DATE_NA = 0xFFFD


def pgn_of(can_id: int) -> int:
    """PGN from a 29-bit CAN identifier."""
    pgn = (can_id >> 8) & 0x3FFFF
    if (pgn >> 8) & 0xFF < 240:
        # PDU1 (addressed) PGNs: the low byte is the destination, not PGN.
        pgn &= 0x3FF00
    return pgn


def source_of(can_id: int) -> int:
    """Sender's bus address from a 29-bit CAN identifier."""
    return can_id & 0xFF


def pgn_id_and_mask(pgn: int, source: Optional[int] = None) -> tuple[int, int]:
    """(id, mask) matching a PDU2 PGN, optionally from one source address.

    Bits compared are the PGN (bits 8-25) and, if given, the source (bits
    0-7). Priority (bits 26-28) is ignored. For use as a CAN acceptance
    filter: a frame matches when `frame_id & mask == id & mask`.
    """
    can_id = pgn << 8
    mask = 0x3FFFF << 8
    if source is not None:
        can_id |= source
        mask |= 0xFF
    return can_id, mask


def parse_system_time(data: bytes) -> Optional[tuple[int, Optional[float]]]:
    """PGN 126992 payload -> (time_source, utc_epoch).

    utc_epoch is None when the sender marks date or time as unavailable.
    Returns None for a payload too short to be 126992.
    """
    if len(data) < 8:
        return None
    time_source = data[1] & 0x0F
    days, tenth_ms = struct.unpack_from("<HI", data, 2)
    if days >= _DATE_NA or tenth_ms >= _TENTH_MS_PER_DAY:
        return time_source, None
    return time_source, days * 86400 + tenth_ms / 10000
