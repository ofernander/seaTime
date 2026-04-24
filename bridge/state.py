"""Shared in-memory state for the status endpoint."""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from os import environ
from pathlib import Path
from typing import Optional

from bridge.config import (
    SIGNALK_URL, SIGNALK_PATH, SOCK_PATH, STALE_THRESHOLD_SEC,
    STARLINK_NTP_IP, VERSION, HOMEPAGE,
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

    def snapshot(self) -> dict:
        now = time.time()
        last_delta_iso = (
            datetime.fromtimestamp(self.last_delta_at, tz=timezone.utc)
                .isoformat(timespec="milliseconds").replace("+00:00", "Z")
            if self.last_delta_at else None
        )
        seconds_since = (
            round(now - self.last_delta_at, 3) if self.last_delta_at else None
        )
        return {
            "signalk": {
                "url": SIGNALK_URL,
                "path": SIGNALK_PATH,
                "connected": self.signalk_connected,
                "auth_mode": self.signalk_auth_mode,
                "needs_access": self.signalk_needs_access,
                "last_delta_received_at": last_delta_iso,
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
            "sources": {
                "starlink_ip": STARLINK_NTP_IP or None,
            },
            "app": {
                "version": VERSION,
                "homepage": HOMEPAGE,
            },
            "host_time": (
                datetime.fromtimestamp(now, tz=timezone.utc)
                    .isoformat(timespec="milliseconds").replace("+00:00", "Z")
            ),
            "host_tz": _host_tz(),
            "uptime_seconds": round(now - self.started_at, 1),
        }


# Module-level singleton. Other modules import and mutate this directly.
STATE = State()
