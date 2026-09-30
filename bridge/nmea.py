"""NMEA 0183 parsing and the shared GNSS time -> chrony feed.

Used by every source that delivers GNSS time as text lines: the USB GPS puck
(serial) and NMEA 0183 over TCP.
"""

from datetime import datetime, timezone
from typing import Optional

from bridge.chrony_sock import ChronySock
from bridge.state import SourceState


# Reject times before this. Older SiRF III pucks hit by the 2019 GPS
# week-number rollover report dates ~19.6 years in the past.
_MIN_PLAUSIBLE_EPOCH = 1577836800  # 2020-01-01 UTC


# ---- NMEA 0183 parsing -----------------------------------------------------

def checksum_ok(line: str) -> bool:
    """True for a well-formed `$...*HH` sentence with a matching checksum."""
    if not line.startswith("$") or len(line) < 4 or line[-3] != "*":
        return False
    calc = 0
    for ch in line[1:-3]:
        calc ^= ord(ch)
    try:
        return calc == int(line[-2:], 16)
    except ValueError:
        return False


def _fields(line: str) -> list[str]:
    """Split a checksum-valid sentence into fields, talker+type first."""
    return line[1:-3].split(",")


def parse_rmc(line: str) -> Optional[tuple[bool, Optional[float]]]:
    """Parse an RMC sentence from any talker (GP, GN, GL, GA, BD...).

    Returns None if the line isn't a parseable RMC sentence, else
    (has_fix, utc_epoch). utc_epoch is None when the receiver has no fix —
    many receivers still report RTC time without a fix, which we don't trust.
    """
    f = _fields(line)
    if len(f) < 10 or not f[0].endswith("RMC"):
        return None
    hhmmss, status, ddmmyy = f[1], f[2], f[9]
    if status != "A":
        return (False, None)
    if len(hhmmss) < 6 or len(ddmmyy) != 6:
        return None
    try:
        dt = datetime(
            2000 + int(ddmmyy[4:6]), int(ddmmyy[2:4]), int(ddmmyy[0:2]),
            int(hhmmss[0:2]), int(hhmmss[2:4]),
            tzinfo=timezone.utc,
        )
        return (True, dt.timestamp() + float(hhmmss[4:]))
    except ValueError:
        return None


def parse_gga_satellites(line: str) -> Optional[int]:
    """Satellites used in the fix, from a GGA sentence. None otherwise."""
    f = _fields(line)
    if len(f) < 8 or not f[0].endswith("GGA"):
        return None
    try:
        return int(f[7])
    except ValueError:
        return None


# ---- Feed ------------------------------------------------------------------

class TimeFeed:
    """Sanity-checks GNSS time readings and forwards them to one chrony SOCK.

    `offset` is the source's fixed delivery latency in seconds: how long after
    the instant a reading describes it reaches us. It's subtracted from the
    receive timestamp.
    """

    def __init__(self, chrony: ChronySock, stats: SourceState, offset: float):
        self.chrony = chrony
        self.stats = stats
        self.offset = offset
        self._last_true_time: Optional[float] = None

    def handle_0183(self, line: str, recv_time: float) -> bool:
        """Feed one checksum-valid sentence. True if it carried fix/time info."""
        sats = parse_gga_satellites(line)
        if sats is not None:
            self.stats.satellites = sats
            return True

        rmc = parse_rmc(line)
        if rmc is None:
            return False
        has_fix, true_time = rmc
        self.stats.has_fix = has_fix
        if true_time is not None:
            self.submit(recv_time, true_time)
        return True

    def submit(self, recv_time: float, true_time: float):
        if true_time < _MIN_PLAUSIBLE_EPOCH:
            self.stats.last_error = (
                "receiver reports an implausible date (GPS week rollover?)"
            )
            return
        # Sources that report each epoch more than once (GPRMC + GNRMC, or
        # several GNSS devices on one bus) would otherwise double-count it.
        if true_time == self._last_true_time:
            return
        self._last_true_time = true_time

        try:
            offset = self.chrony.send_sample(recv_time - self.offset, true_time)
        except OSError:
            return  # ChronySock logged; retry on next reading.
        self.stats.samples_sent += 1
        self.stats.last_sample_at = recv_time
        self.stats.last_sample_offset = offset
        self.stats.last_datetime_value = (
            datetime.fromtimestamp(true_time, tz=timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
