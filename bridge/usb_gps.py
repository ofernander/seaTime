"""USB GPS puck reader (NMEA 0183 over serial).

Reads RMC sentences from a USB GPS receiver (u-blox VK-162/VK-172, GlobalSat
BU-353, and similar) and feeds the UTC time of each valid fix into chrony via
a dedicated SOCK refclock (refid NMEA).

Runs in a daemon thread rather than on the asyncio loop: pyserial is blocking,
and taking the receive timestamp in a thread that does nothing else keeps it
free of event-loop scheduling jitter.

Timing: a receiver emits the RMC sentence for second T some tens to hundreds
of milliseconds after T, and that delay is roughly constant for a given
receiver and baud rate. USB_GPS_OFFSET subtracts it. USB pucks don't expose
PPS, so expect ~10-50ms accuracy once calibrated — well ahead of SignalK's
WebSocket path, well behind Starlink.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import serial

from bridge.config import (
    USB_GPS_DEVICE,
    USB_GPS_BAUD,
    USB_GPS_OFFSET,
    STALE_THRESHOLD_SEC,
    RECONNECT_MIN_SEC,
    RECONNECT_MAX_SEC,
)
from bridge.chrony_sock import ChronySock
from bridge.state import STATE


log = logging.getLogger(__name__)


# Tried in order when USB_GPS_BAUD is unset. Covers u-blox (9600), SiRF-based
# pucks like the BU-353 (4800), and receivers configured faster. CDC-ACM
# devices (/dev/ttyACM*) ignore the baud rate, so the first one just works.
_AUTO_BAUDS = (9600, 4800, 38400, 115200)

# Move on to the next baud rate if no checksum-valid NMEA arrives in this window.
_BAUD_PROBE_SEC = 3.0

# Reject fixes dated before this. Older SiRF III pucks hit by the 2019 GPS
# week-number rollover report dates ~19.6 years in the past.
_MIN_PLAUSIBLE_EPOCH = 1577836800  # 2020-01-01 UTC


# ---- NMEA parsing ----------------------------------------------------------

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


# ---- Reader ----------------------------------------------------------------

class UsbGps:
    def __init__(self, chrony: ChronySock):
        self.chrony = chrony
        self._last_true_time: Optional[float] = None

    def start(self):
        threading.Thread(target=self._run, name="usb-gps", daemon=True).start()

    def _run(self):
        bauds = (USB_GPS_BAUD,) if USB_GPS_BAUD else _AUTO_BAUDS
        backoff = RECONNECT_MIN_SEC
        while True:
            try:
                for baud in bauds:
                    self._session(baud)
                STATE.usb_gps_last_error = (
                    f"no NMEA data at baud {', '.join(map(str, bauds))}"
                )
                log.warning("USB GPS: %s", STATE.usb_gps_last_error)
            except Exception as e:
                if STATE.usb_gps_connected:
                    backoff = RECONNECT_MIN_SEC  # Was working; retry promptly.
                STATE.usb_gps_last_error = f"{type(e).__name__}: {e}"
                log.warning("USB GPS error (will retry in %.1fs): %s", backoff, e)
            STATE.usb_gps_connected = False
            STATE.usb_gps_has_fix = False
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SEC)

    def _session(self, baud: int):
        """Read from the device at `baud` until it fails.

        Returns only if nothing NMEA-shaped arrived within the probe window
        (wrong baud). Otherwise runs until the device goes away or goes
        quiet, which raises.
        """
        with serial.Serial(USB_GPS_DEVICE, baud, timeout=1.0) as port:
            # Drop whatever the kernel buffered while nobody was reading —
            # stale sentences would carry a stale timestamp.
            port.reset_input_buffer()
            opened = time.monotonic()
            last_nmea: Optional[float] = None
            while True:
                raw = port.readline()
                recv_time = time.time()  # Tight to recv, before any parsing.
                line = raw.decode("ascii", errors="replace").strip()
                if checksum_ok(line):
                    if last_nmea is None:
                        log.info("USB GPS: receiving NMEA on %s at %d baud",
                                 USB_GPS_DEVICE, baud)
                        STATE.usb_gps_connected = True
                        STATE.usb_gps_baud = baud
                        STATE.usb_gps_last_error = None
                    last_nmea = time.monotonic()
                    self._handle(line, recv_time)
                    continue

                idle = time.monotonic() - (last_nmea or opened)
                if last_nmea is None and idle > _BAUD_PROBE_SEC:
                    return
                if last_nmea is not None and idle > STALE_THRESHOLD_SEC:
                    raise TimeoutError(f"no NMEA data for {idle:.0f}s")

    def _handle(self, line: str, recv_time: float):
        sats = parse_gga_satellites(line)
        if sats is not None:
            STATE.usb_gps_satellites = sats
            return

        rmc = parse_rmc(line)
        if rmc is None:
            return
        has_fix, true_time = rmc
        STATE.usb_gps_has_fix = has_fix
        if true_time is None:
            return
        if true_time < _MIN_PLAUSIBLE_EPOCH:
            STATE.usb_gps_last_error = (
                "receiver reports an implausible date (GPS week rollover?)"
            )
            return
        # Receivers that emit RMC from several talkers (GPRMC + GNRMC)
        # would otherwise double-count each epoch.
        if true_time == self._last_true_time:
            return
        self._last_true_time = true_time

        try:
            offset = self.chrony.send_sample(recv_time - USB_GPS_OFFSET, true_time)
        except OSError:
            return  # ChronySock logged; retry on next sentence.
        STATE.usb_gps_samples_sent += 1
        STATE.usb_gps_last_sample_at = recv_time
        STATE.usb_gps_last_sample_offset = offset
        STATE.usb_gps_last_datetime_value = (
            datetime.fromtimestamp(true_time, tz=timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
