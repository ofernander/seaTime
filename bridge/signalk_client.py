"""SignalK WebSocket client.

Connects, subscribes to the configured path, reads deltas, and feeds samples
into chrony via the provided ChronySock. Reconnects on failure with exponential
backoff.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, urlunparse

import websockets

from bridge.config import (
    SIGNALK_PATH,
    RECONNECT_MIN_SEC,
    RECONNECT_MAX_SEC,
)
from bridge.chrony_sock import ChronySock
from bridge.signalk_auth import http_base, load_persisted_token, probe_readonly_access
from bridge.state import STATE


log = logging.getLogger(__name__)


def _ws_url() -> str:
    """Derive the SignalK stream WebSocket URL from the HTTP base.

    Subscribe=none lets us send our own subscribe message after connect.
    """
    parsed = urlparse(http_base())
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    path = "/signalk/v1/stream?subscribe=none"
    return urlunparse((ws_scheme, parsed.netloc, path, "", "", ""))


def _parse_iso_to_epoch(s: str) -> float:
    """Parse an ISO 8601 UTC string to epoch seconds."""
    normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _handle_message(raw: str, chrony: ChronySock):
    """Parse a SignalK delta, send chrony sample if it has our target path."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(msg, dict):
        log.debug("Ignoring non-dict WS message: %r", msg)
        return

    updates = msg.get("updates")
    if not isinstance(updates, list):
        return
    for update in updates:
        if not isinstance(update, dict):
            continue
        values = update.get("values")
        if not isinstance(values, list):
            continue
        for v in values:
            if not isinstance(v, dict):
                continue
            if v.get("path") != SIGNALK_PATH:
                continue
            datetime_str = v.get("value")
            if not isinstance(datetime_str, str):
                continue
            system_time = time.time()  # Tight to recv, before any parsing.
            try:
                true_time = _parse_iso_to_epoch(datetime_str)
            except ValueError as e:
                log.debug("Bad datetime %r: %s", datetime_str, e)
                continue
            try:
                offset = chrony.send_sample(system_time, true_time)
            except OSError:
                return  # ChronySock logged; retry on next delta.
            STATE.samples_sent += 1
            STATE.last_sample_offset = offset
            STATE.last_delta_at = system_time
            STATE.last_datetime_value = datetime_str


async def run(chrony: ChronySock, initial_token: Optional[str]):
    """Run the SignalK client forever, reconnecting as needed."""
    ws_url = _ws_url()

    backoff = RECONNECT_MIN_SEC
    while True:
        # Re-read token from disk on every connect attempt. If the user
        # approved an access request via the UI since the last attempt,
        # pick up the new token without needing a container restart.
        token = load_persisted_token() or initial_token
        headers = {"Authorization": f"Bearer {token}"} if token else None

        # If we have no token, probe to see if SignalK allows readonly access.
        # This tells us whether the UI should surface the "request access" button.
        if not token:
            probe_result = await probe_readonly_access()
            if probe_result == "auth_needed":
                STATE.signalk_needs_access = True
                STATE.last_error = "SignalK requires authentication"
                log.info("Probe says auth needed; waiting %.1fs before retry", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SEC)
                continue
            elif probe_result == "ok":
                STATE.signalk_needs_access = False

        try:
            log.info("Connecting to SignalK: %s", ws_url)
            async with websockets.connect(
                ws_url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                STATE.signalk_connected = True
                STATE.signalk_needs_access = False
                STATE.last_error = None
                backoff = RECONNECT_MIN_SEC

                sub = {
                    "context": "vessels.self",
                    "subscribe": [{
                        "path": SIGNALK_PATH,
                        "period": 1000,
                        "format": "delta",
                        "policy": "instant",
                    }],
                }
                await ws.send(json.dumps(sub))
                log.info("Subscribed to %s", SIGNALK_PATH)

                async for raw in ws:
                    try:
                        _handle_message(raw, chrony)
                    except Exception as e:
                        log.warning("Error handling SignalK message (continuing): %s", e)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            STATE.signalk_connected = False
            STATE.last_error = f"{type(e).__name__}: {e}"
            log.warning("SignalK connection error (will retry in %.1fs): %s", backoff, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SEC)
