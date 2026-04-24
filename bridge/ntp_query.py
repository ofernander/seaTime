"""Self-query chrony via its LAN NTP server at 127.0.0.1:123.

Used by the web UI to show the authoritative served time — whatever chrony
is telling LAN clients right now, based on its currently-selected source.
This is the most honest "what time are we serving" display possible since
it uses the same protocol path real LAN NTP clients use.

Stdlib-only, no `ntplib` dependency. Results cached briefly to avoid
redundant queries when multiple things call snapshot() close together.
"""

import logging
import socket
import struct
import time
from datetime import datetime, timezone


log = logging.getLogger(__name__)


# NTP epoch (1900-01-01) to Unix epoch (1970-01-01) offset in seconds.
NTP_DELTA = 2208988800

# Client request packet: LI=0, VN=3, Mode=3 (client) = 0b00_011_011 = 0x1b.
# All other bytes zero.
_CLIENT_PACKET = b"\x1b" + b"\x00" * 47

_CACHE: dict = {"at": 0.0, "data": None}
_CACHE_TTL = 1.5  # seconds


def _query(host: str = "127.0.0.1", port: int = 123, timeout: float = 0.5) -> dict:
    """Send an NTP client request, return parsed response fields.

    Returns a dict with keys:
      time_utc  : ISO 8601 UTC string of server's transmit timestamp, or None on failure
      stratum   : NTP stratum (0-16; 16 = unsynced), or None on failure
      leap      : leap indicator (0 normal, 3 unsynced), or None on failure
      error     : error message string, or None on success
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(_CLIENT_PACKET, (host, port))
        packet, _ = sock.recvfrom(48)
    except (socket.timeout, OSError) as e:
        log.debug("NTP query to %s:%d failed: %s", host, port, e)
        return {"time_utc": None, "stratum": None, "leap": None, "error": str(e)}
    finally:
        sock.close()

    if len(packet) < 48:
        return {"time_utc": None, "stratum": None, "leap": None, "error": "short response"}

    # First byte: LI (2 bits) | VN (3 bits) | Mode (3 bits)
    first_byte = packet[0]
    leap = (first_byte >> 6) & 0b11
    stratum = packet[1]

    # Transmit Timestamp: bytes 40-47, big-endian.
    # First 4 bytes = seconds since NTP epoch, next 4 = fractional seconds.
    tx_seconds, tx_fraction = struct.unpack("!II", packet[40:48])
    unix_seconds = tx_seconds - NTP_DELTA
    # Convert fraction to decimal. 2^32 = 4294967296.
    unix_fraction = tx_fraction / 4294967296.0
    unix_timestamp = unix_seconds + unix_fraction

    # Guard against wildly bogus timestamps (e.g. chrony not yet initialized
    # and returning 0). Anything before year 2020 is treated as invalid.
    if unix_timestamp < 1577836800:  # 2020-01-01 UTC
        return {
            "time_utc": None,
            "stratum": stratum,
            "leap": leap,
            "error": "server returned invalid timestamp (not yet synced?)",
        }

    time_utc = (
        datetime.fromtimestamp(unix_timestamp, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return {"time_utc": time_utc, "stratum": stratum, "leap": leap, "error": None}


def snapshot() -> dict:
    """Return current NTP query result, cached for 1.5s."""
    now = time.time()
    if _CACHE["data"] and (now - _CACHE["at"] < _CACHE_TTL):
        return _CACHE["data"]
    data = _query()
    _CACHE["at"] = now
    _CACHE["data"] = data
    return data
