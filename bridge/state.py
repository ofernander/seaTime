"""Shared in-memory state for the status endpoint."""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from os import environ
from pathlib import Path
from typing import Optional

from bridge.config import (
    SIGNALK_URL, SIGNALK_ENABLED, SIGNALK_PATH, SOCK_PATH, STALE_THRESHOLD_SEC,
    STARLINK_NTP_IP, USB_GPS_DEVICE, USB_GPS_BAUD, USB_GPS_OFFSET,
    USB_GPS_SOCK_PATH, VERSION, HOMEPAGE,
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
    # USB GPS puck (mutated from the usb-gps thread)
    usb_gps_connected: bool = False
    usb_gps_baud: Optional[int] = None
    usb_gps_has_fix: bool = False
    usb_gps_satellites: Optional[int] = None
    usb_gps_last_sample_at: Optional[float] = None
    usb_gps_last_datetime_value: Optional[str] = None
    usb_gps_samples_sent: int = 0
    usb_gps_last_sample_offset: Optional[float] = None
    usb_gps_last_error: Optional[str] = None

    def snapshot(self) -> dict:
        now = time.time()
        seconds_since = (
            round(now - self.last_delta_at, 3) if self.last_delta_at else None
        )
        usb_seconds_since = (
            round(now - self.usb_gps_last_sample_at, 3)
            if self.usb_gps_last_sample_at else None
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
                "connected": self.usb_gps_connected,
                "has_fix": self.usb_gps_has_fix,
                "satellites": self.usb_gps_satellites,
                "last_sample_received_at": _iso(self.usb_gps_last_sample_at),
                "last_datetime_value": self.usb_gps_last_datetime_value,
                "seconds_since_last_sample": usb_seconds_since,
                "is_stale": (
                    self.usb_gps_last_sample_at is None
                    or (now - self.usb_gps_last_sample_at) > STALE_THRESHOLD_SEC
                ),
                "samples_sent": self.usb_gps_samples_sent,
                "last_sample_offset_seconds": self.usb_gps_last_sample_offset,
                "offset_compensation_seconds": USB_GPS_OFFSET,
                "sock_path": USB_GPS_SOCK_PATH,
                "last_error": self.usb_gps_last_error,
            },
            "sources": {
                "starlink_ip": STARLINK_NTP_IP or None,
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
