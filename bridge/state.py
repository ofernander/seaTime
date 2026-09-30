"""Shared in-memory state for the status endpoint."""

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from os import environ
from pathlib import Path
from typing import Optional

from bridge.config import (
    SIGNALK_URL, SIGNALK_ENABLED, SIGNALK_PATH, SOCK_PATH, STALE_THRESHOLD_SEC,
    STARLINK_NTP_IP, NTP_FALLBACK_POOL, USB_GPS_DEVICE, USB_GPS_BAUD, USB_GPS_OFFSET,
    USB_GPS_SOCK_PATH, NMEA_TCP_HOST, NMEA_TCP_PORT, NMEA_TCP_OFFSET,
    NMEA_TCP_SOCK_PATH, N2K_CAN_INTERFACE, N2K_CAN_OFFSET, N2K_CAN_SOURCE,
    N2K_CAN_SOCK_PATH,
    VERSION, HOMEPAGE,
)


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _host_tz() -> str:
    """Return the host's timezone by reading the file at HOST_TZ_FILE.
    Defaults to /etc/timezone, which is bind-mounted read-only from the
    Docker host in docker-compose.yml. Falls back to UTC if the file is
    missing or unreadable.
    """
    tz_file = Path(environ.get("HOST_TZ_FILE", "/etc/timezone"))
    if tz_file.exists():
        try:
            content = tz_file.read_text().strip()
            if content:
                return content
        except OSError:
            pass
    return "UTC"


@dataclass
class SourceState:
    """Runtime stats for one GNSS time source the bridge feeds to chrony.

    Mutated from that source's reader thread.
    """
    connected: bool = False
    has_fix: bool = False
    satellites: Optional[int] = None
    last_sample_at: Optional[float] = None
    last_datetime_value: Optional[str] = None
    samples_sent: int = 0
    last_sample_offset: Optional[float] = None
    last_error: Optional[str] = None
    sentences_received: int = 0
    recent_sentences: deque = field(default_factory=lambda: deque(maxlen=10))

    def record(self, line: str):
        self.sentences_received += 1
        self.recent_sentences.append(line)

    def snapshot(self, now: float) -> dict:
        return {
            "connected": self.connected,
            "has_fix": self.has_fix,
            "satellites": self.satellites,
            "last_sample_received_at": _iso(self.last_sample_at),
            "last_datetime_value": self.last_datetime_value,
            "seconds_since_last_sample": (
                round(now - self.last_sample_at, 3)
                if self.last_sample_at else None
            ),
            "is_stale": (
                self.last_sample_at is None
                or (now - self.last_sample_at) > STALE_THRESHOLD_SEC
            ),
            "sentences_received": self.sentences_received,
            "recent_sentences": list(self.recent_sentences),
            "samples_sent": self.samples_sent,
            "last_sample_offset_seconds": self.last_sample_offset,
            "last_error": self.last_error,
        }


@dataclass
class State:
    started_at: float = field(default_factory=time.time)
    signalk_connected: bool = False
    signalk_auth_mode: str = "unknown"  # none | password | device-request | failed
    signalk_needs_access: bool = False  # True if SignalK requires auth we don't have
    last_delta_at: Optional[float] = None
    last_datetime_value: Optional[str] = None
    samples_sent: int = 0
    last_sample_offset: Optional[float] = None
    last_error: Optional[str] = None
    # Access request flow state
    access_request_state: str = "idle"  # idle | pending | approved | denied
    access_request_approve_url: Optional[str] = None  # SignalK admin UI URL for user to click
    access_request_error: Optional[str] = None
    # USB GPS puck
    usb_gps: SourceState = field(default_factory=SourceState)
    usb_gps_baud: Optional[int] = None
    # NMEA 0183 over TCP
    nmea_tcp: SourceState = field(default_factory=SourceState)
    # NMEA 2000 over SocketCAN
    n2k_can: SourceState = field(default_factory=SourceState)
    n2k_can_gps_address: Optional[int] = None  # bus address of the GNSS sending 126992
    n2k_can_time_source: Optional[str] = None  # GPS | GLONASS

    def snapshot(self) -> dict:
        now = time.time()
        seconds_since = (
            round(now - self.last_delta_at, 3) if self.last_delta_at else None
        )
        return {
            "signalk": {
                "enabled": SIGNALK_ENABLED,
                "url": SIGNALK_URL,
                "path": SIGNALK_PATH,
                "connected": self.signalk_connected,
                "auth_mode": self.signalk_auth_mode,
                "needs_access": self.signalk_needs_access,
                "last_delta_received_at": _iso(self.last_delta_at),
                "last_datetime_value": self.last_datetime_value,
                "seconds_since_last_delta": seconds_since,
                "is_stale": (
                    self.last_delta_at is None
                    or (now - self.last_delta_at) > STALE_THRESHOLD_SEC
                ),
                "last_error": self.last_error,
                "access_request": {
                    "state": self.access_request_state,
                    "approve_url": self.access_request_approve_url,
                    "error": self.access_request_error,
                },
            },
            "chrony_socket": {
                "path": SOCK_PATH,
                "samples_sent": self.samples_sent,
                "last_sample_offset_seconds": self.last_sample_offset,
            },
            "usb_gps": {
                "enabled": bool(USB_GPS_DEVICE),
                "device": USB_GPS_DEVICE or None,
                "configured_baud": USB_GPS_BAUD or "auto",
                "baud": self.usb_gps_baud,
                **self.usb_gps.snapshot(now),
                "offset_compensation_seconds": USB_GPS_OFFSET,
                "sock_path": USB_GPS_SOCK_PATH,
            },
            "nmea_tcp": {
                "enabled": bool(NMEA_TCP_HOST),
                "host": NMEA_TCP_HOST or None,
                "port": NMEA_TCP_PORT or None,
                **self.nmea_tcp.snapshot(now),
                "offset_compensation_seconds": NMEA_TCP_OFFSET,
                "sock_path": NMEA_TCP_SOCK_PATH,
            },
            "n2k_can": {
                "enabled": bool(N2K_CAN_INTERFACE),
                "interface": N2K_CAN_INTERFACE or None,
                "source_filter": N2K_CAN_SOURCE,
                "gps_address": self.n2k_can_gps_address,
                "time_source": self.n2k_can_time_source,
                **self.n2k_can.snapshot(now),
                "offset_compensation_seconds": N2K_CAN_OFFSET,
                "sock_path": N2K_CAN_SOCK_PATH,
            },
            "sources": {
                "starlink_ip": STARLINK_NTP_IP or None,
                "pool": NTP_FALLBACK_POOL or None,
            },
            "app": {
                "version": VERSION,
                "homepage": HOMEPAGE,
            },
            "host_time": _iso(now),
            "host_tz": _host_tz(),
            "uptime_seconds": round(now - self.started_at, 1),
        }


# Module-level singleton. Other modules import and mutate this directly.
STATE = State()
