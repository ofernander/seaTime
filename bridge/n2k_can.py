"""NMEA 2000 over SocketCAN reader.

Reads PGN 126992 System Time straight off a Linux CAN interface (a USB-CAN
adapter or CAN hat that shows up as can0) and feeds GNSS time into chrony
via a dedicated SOCK refclock (refid NCAN).

- A kernel acceptance filter delivers only 126992 frames (optionally only
  from N2K_CAN_SOURCE), so the rest of the bus never reaches Python.
- Each frame is stamped by the kernel on arrival (SO_TIMESTAMP), so the
  receive time is free of Python and scheduling latency.
- Read-only: nothing is ever transmitted onto the bus.

The host brings the interface up (250 kbit/s for NMEA 2000); the container
needs host networking to see it. SocketCAN delivers every frame to every
reader, so SignalK can share the same interface.

Runs in a daemon thread like the other readers.
"""

import logging
import socket
import struct
import threading
import time
from typing import Optional

from bridge.config import (
    N2K_CAN_INTERFACE,
    N2K_CAN_OFFSET,
    N2K_CAN_SOURCE,
    RECONNECT_MIN_SEC,
    RECONNECT_MAX_SEC,
)
from bridge.chrony_sock import ChronySock
from bridge.n2k import (
    GNSS_TIME_SOURCES,
    PGN_SYSTEM_TIME,
    parse_system_time,
    pgn_id_and_mask,
    source_of,
)
from bridge.nmea import TimeFeed
from bridge.state import STATE


log = logging.getLogger(__name__)


# Linux <linux/can.h> constants. Defined here rather than read from the
# socket module so this file imports (and its frame handling can be tested)
# on platforms without SocketCAN.
CAN_EFF_FLAG = 0x80000000  # 29-bit extended frame
CAN_RTR_FLAG = 0x40000000  # remote transmission request
CAN_EFF_MASK = 0x1FFFFFFF

# <asm-generic/socket.h> SO_TIMESTAMP (SO_TIMESTAMP_OLD): kernel receive time
# as a struct timeval of native longs. Python's socket module doesn't expose it.
SO_TIMESTAMP = 29

# struct can_frame: u32 can_id, u8 len, 3 pad/reserved bytes, u8 data[8].
_CAN_FRAME = struct.Struct("=IB3x8s")

# struct can_filter: u32 can_id, u32 can_mask.
_CAN_FILTER = struct.Struct("=II")

# struct timeval as delivered with SO_TIMESTAMP: two native longs.
_TIMEVAL = struct.Struct("@ll")


def acceptance_filter(source: Optional[int]) -> bytes:
    """CAN_RAW_FILTER value passing only 126992 data frames.

    Requires the extended-frame flag and a clear RTR flag, so 11-bit and
    remote frames never match.
    """
    can_id, mask = pgn_id_and_mask(PGN_SYSTEM_TIME, source)
    return _CAN_FILTER.pack(can_id | CAN_EFF_FLAG, mask | CAN_EFF_FLAG | CAN_RTR_FLAG)


def parse_frame(frame: bytes) -> tuple[int, bytes]:
    """(29-bit id, data) from a raw struct can_frame."""
    can_id, length, data = _CAN_FRAME.unpack_from(frame)
    return can_id & CAN_EFF_MASK, data[:length]


def kernel_timestamp(ancdata) -> Optional[float]:
    """Arrival time from SO_TIMESTAMP ancillary data, if present."""
    for level, kind, cdata in ancdata:
        if level == socket.SOL_SOCKET and kind == SO_TIMESTAMP:
            sec, usec = _TIMEVAL.unpack_from(cdata)
            return sec + usec / 1_000_000
    return None


def candump_line(can_id: int, data: bytes) -> str:
    """Format a frame like `candump`, for diagnostics."""
    return f"{can_id:08X} [{len(data)}] " + " ".join(f"{b:02X}" for b in data)


class N2kCan:
    def __init__(self, chrony: ChronySock):
        self.feed = TimeFeed(chrony, STATE.n2k_can, N2K_CAN_OFFSET)
        self._ignored_sources: set[tuple[int, int]] = set()

    def start(self):
        threading.Thread(target=self._run, name="n2k-can", daemon=True).start()

    def _run(self):
        stats = STATE.n2k_can
        backoff = RECONNECT_MIN_SEC
        while True:
            try:
                self._session()
            except Exception as e:
                if stats.connected:
                    backoff = RECONNECT_MIN_SEC  # Was working; retry promptly.
                stats.last_error = f"{type(e).__name__}: {e}"
                log.warning("N2K CAN error (will retry in %.1fs): %s", backoff, e)
            stats.connected = False
            stats.has_fix = False
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SEC)

    def _session(self):
        """Listen on the interface until it goes down or disappears.

        A quiet bus isn't a fault (the GPS may simply not be sending), so
        silence only shows up as a stale source in the UI. The kernel wakes
        a blocked read with ENETDOWN/ENODEV when the interface goes away,
        which raises and triggers a reconnect.
        """
        stats = STATE.n2k_can
        with socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as sock:
            sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                            acceptance_filter(N2K_CAN_SOURCE))
            sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMP, 1)
            sock.bind((N2K_CAN_INTERFACE,))
            log.info("N2K CAN: listening on %s for PGN %d%s", N2K_CAN_INTERFACE,
                     PGN_SYSTEM_TIME,
                     f" from address {N2K_CAN_SOURCE}" if N2K_CAN_SOURCE is not None else "")
            stats.connected = True
            stats.last_error = None
            anc_size = socket.CMSG_SPACE(_TIMEVAL.size)
            while True:
                frame, ancdata, _, _ = sock.recvmsg(_CAN_FRAME.size, anc_size)
                recv_time = kernel_timestamp(ancdata) or time.time()
                self.handle_frame(frame, recv_time)

    def handle_frame(self, frame: bytes, recv_time: float):
        stats = STATE.n2k_can
        can_id, data = parse_frame(frame)
        src = source_of(can_id)
        line = candump_line(can_id, data)
        log.debug("N2K CAN: %s", line)
        stats.record(line)

        parsed = parse_system_time(data)
        if parsed is None:
            return
        time_source, true_time = parsed
        if time_source not in GNSS_TIME_SOURCES:
            if (src, time_source) not in self._ignored_sources:
                self._ignored_sources.add((src, time_source))
                log.info("N2K CAN: ignoring PGN %d from address %d: "
                         "time source %d is not GNSS", PGN_SYSTEM_TIME, src, time_source)
            return

        STATE.n2k_can_gps_address = src
        STATE.n2k_can_time_source = GNSS_TIME_SOURCES[time_source]
        stats.has_fix = true_time is not None
        if true_time is not None:
            self.feed.submit(recv_time, true_time)
