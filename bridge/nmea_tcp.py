"""NMEA 0183 over TCP client.

Connects to any device that serves NMEA 0183 sentences on a TCP port — NMEA
2000 / 0183 Ethernet and WiFi gateways, multiplexers, SignalK's NMEA 0183
server, and similar — and feeds the UTC time of each valid RMC fix into
chrony via a dedicated SOCK refclock (refid NTCP).

Runs in a daemon thread for the same reason as the USB GPS reader: the
receive timestamp should be free of event-loop jitter (the loop blocks on
chronyc subprocess calls while serving the web UI).
"""

import logging
import socket
import threading
import time

from bridge.config import (
    NMEA_TCP_HOST,
    NMEA_TCP_PORT,
    NMEA_TCP_OFFSET,
    STALE_THRESHOLD_SEC,
    RECONNECT_MIN_SEC,
    RECONNECT_MAX_SEC,
)
from bridge.chrony_sock import ChronySock
from bridge.nmea import TimeFeed, checksum_ok
from bridge.state import STATE


log = logging.getLogger(__name__)


class NmeaTcp:
    def __init__(self, chrony: ChronySock):
        self.feed = TimeFeed(chrony, STATE.nmea_tcp, NMEA_TCP_OFFSET)

    def start(self):
        threading.Thread(target=self._run, name="nmea-tcp", daemon=True).start()

    def _run(self):
        stats = STATE.nmea_tcp
        backoff = RECONNECT_MIN_SEC
        while True:
            try:
                self._session()
            except Exception as e:
                if stats.connected:
                    backoff = RECONNECT_MIN_SEC  # Was working; retry promptly.
                stats.last_error = f"{type(e).__name__}: {e}"
                log.warning("NMEA TCP error (will retry in %.1fs): %s", backoff, e)
            stats.connected = False
            stats.has_fix = False
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SEC)

    def _session(self):
        """Read from the server until the connection fails or goes quiet."""
        stats = STATE.nmea_tcp
        with socket.create_connection((NMEA_TCP_HOST, NMEA_TCP_PORT), timeout=10) as sock:
            # A GPS emits at least one sentence a second; silence means a dead
            # connection. recv timing out raises and triggers a reconnect.
            sock.settimeout(STALE_THRESHOLD_SEC)
            log.info("NMEA TCP: connected to %s:%d", NMEA_TCP_HOST, NMEA_TCP_PORT)
            stats.connected = True
            stats.last_error = None
            for raw in sock.makefile("rb"):
                recv_time = time.time()  # Tight to recv, before any parsing.
                line = raw.decode("ascii", errors="replace").strip()
                if not checksum_ok(line):
                    continue
                # Servers fed from a whole bus emit everything on it (depth,
                # wind, AIS...). Only keep time-bearing sentences for
                # diagnostics.
                if self.feed.handle_0183(line, recv_time):
                    log.debug("NMEA TCP: %s", line)
                    stats.record(line)
            raise ConnectionError("server closed the connection")
