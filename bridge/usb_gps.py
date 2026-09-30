"""USB GPS puck reader (NMEA 0183 over serial).

Reads RMC sentences from a USB GPS receiver (u-blox VK-162/VK-172, GlobalSat
BU-353, and similar) and feeds the UTC time of each valid fix into chrony via
a dedicated SOCK refclock (refid USB).

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
from bridge.nmea import TimeFeed, checksum_ok
from bridge.state import STATE


log = logging.getLogger(__name__)


# Tried in order when USB_GPS_BAUD is unset. Covers u-blox (9600), SiRF-based
# pucks like the BU-353 (4800), and receivers configured faster. CDC-ACM
# devices (/dev/ttyACM*) ignore the baud rate, so the first one just works.
_AUTO_BAUDS = (9600, 4800, 38400, 115200)

# Move on to the next baud rate if no checksum-valid NMEA arrives in this window.
_BAUD_PROBE_SEC = 3.0


class UsbGps:
    def __init__(self, chrony: ChronySock):
        self.feed = TimeFeed(chrony, STATE.usb_gps, USB_GPS_OFFSET)

    def start(self):
        threading.Thread(target=self._run, name="usb-gps", daemon=True).start()

    def _run(self):
        stats = STATE.usb_gps
        bauds = (USB_GPS_BAUD,) if USB_GPS_BAUD else _AUTO_BAUDS
        backoff = RECONNECT_MIN_SEC
        while True:
            try:
                for baud in bauds:
                    self._session(baud)
                stats.last_error = (
                    f"no NMEA data at baud {', '.join(map(str, bauds))}"
                )
                log.warning("USB GPS: %s", stats.last_error)
            except Exception as e:
                if stats.connected:
                    backoff = RECONNECT_MIN_SEC  # Was working; retry promptly.
                stats.last_error = f"{type(e).__name__}: {e}"
                log.warning("USB GPS error (will retry in %.1fs): %s", backoff, e)
            stats.connected = False
            stats.has_fix = False
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SEC)

    def _session(self, baud: int):
        """Read from the device at `baud` until it fails.

        Returns only if nothing NMEA-shaped arrived within the probe window
        (wrong baud). Otherwise runs until the device goes away or goes
        quiet, which raises.
        """
        stats = STATE.usb_gps
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
                        stats.connected = True
                        stats.last_error = None
                        STATE.usb_gps_baud = baud
                    last_nmea = time.monotonic()
                    log.debug("USB GPS: %s", line)
                    stats.record(line)
                    self.feed.handle_0183(line, recv_time)
                    continue

                idle = time.monotonic() - (last_nmea or opened)
                if last_nmea is None and idle > _BAUD_PROBE_SEC:
                    return
                if last_nmea is not None and idle > STALE_THRESHOLD_SEC:
                    raise TimeoutError(f"no NMEA data for {idle:.0f}s")
