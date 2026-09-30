"""Chrony SOCK refclock client.

Packs and sends sock_sample structs over a Unix datagram socket to chronyd.

Protocol reference:
    struct sock_sample {
        struct timeval tv;   // (long sec, long usec) on 64-bit Linux
        double offset;       // true_time - system_time, seconds
        int pulse;           // 0 for non-PPS sources
        int leap;            // 0 normal, 1 insert, 2 delete
        int _pad;            // ignored
        int magic;           // 0x534f434b ("SOCK")
    };
    // Total: 40 bytes on 64-bit Linux, native alignment.
"""

import logging
import socket
import struct
from typing import Optional


log = logging.getLogger(__name__)


SOCK_MAGIC = 0x534F434B  # "SOCK"
_SOCK_STRUCT = struct.Struct("@qqdiiii")
assert _SOCK_STRUCT.size == 40, f"unexpected sock_sample size: {_SOCK_STRUCT.size}"


class ChronySock:
    def __init__(self, path: str):
        self.path = path
        self._sock: Optional[socket.socket] = None

    def _ensure(self):
        if self._sock is None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.connect(self.path)
            self._sock = s

    def send_sample(self, system_time: float, true_time: float, leap: int = 0) -> float:
        """Send one sample to chrony. Returns the offset sent.

        system_time: time.time() at the moment we observed the truth.
        true_time:   GPS-derived UTC seconds since epoch.
        leap:        0 normal, 1 insert leap second, 2 delete leap second.
        """
        self._ensure()
        tv_sec = int(system_time)
        tv_usec = int((system_time - tv_sec) * 1_000_000)
        offset = true_time - system_time
        pkt = _SOCK_STRUCT.pack(
            tv_sec, tv_usec,
            offset,
            0,          # pulse
            leap,
            0,          # _pad
            SOCK_MAGIC,
        )
        try:
            assert self._sock is not None
            self._sock.send(pkt)
        except (OSError, BrokenPipeError) as e:
            log.warning("chrony SOCK send failed, will reconnect: %s", e)
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
            raise
        return offset
