"""Entry point for `python3 -m bridge`.

Initializes logging, starts the USB GPS and NMEA TCP readers (if configured),
authenticates to SignalK (if configured), starts the HTTP server, and runs
forever.
"""

import asyncio
import logging
import sys

from aiohttp import web

from bridge.config import (
    SIGNALK_URL, SIGNALK_ENABLED, SIGNALK_PATH, SOCK_PATH, HTTP_PORT, LOG_LEVEL,
    USB_GPS_DEVICE, USB_GPS_SOCK_PATH,
    NMEA_TCP_HOST, NMEA_TCP_PORT, NMEA_TCP_SOCK_PATH,
)
from bridge.state import STATE
from bridge.chrony_sock import ChronySock
from bridge.signalk_auth import authenticate
from bridge.signalk_client import run as run_signalk
from bridge.http_server import build_app
from bridge.usb_gps import UsbGps
from bridge.nmea_tcp import NmeaTcp


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bridge")


async def main():
    log.info("boat-time bridge starting")
    log.info("HTTP port:    %d", HTTP_PORT)

    # USB GPS and NMEA TCP run in their own threads; start them first so they
    # aren't held up by the (possibly minutes-long) SignalK auth below.
    if USB_GPS_DEVICE:
        log.info("USB GPS:      %s -> %s", USB_GPS_DEVICE, USB_GPS_SOCK_PATH)
        UsbGps(ChronySock(USB_GPS_SOCK_PATH)).start()
    else:
        log.info("USB GPS:      disabled (USB_GPS_DEVICE is blank)")

    if NMEA_TCP_HOST:
        log.info("NMEA TCP:     %s:%d -> %s", NMEA_TCP_HOST, NMEA_TCP_PORT, NMEA_TCP_SOCK_PATH)
        NmeaTcp(ChronySock(NMEA_TCP_SOCK_PATH)).start()
    else:
        log.info("NMEA TCP:     disabled (NMEA_TCP_HOST is blank)")

    token = None
    if SIGNALK_ENABLED:
        log.info("SignalK URL:  %s", SIGNALK_URL)
        log.info("SignalK path: %s", SIGNALK_PATH)
        log.info("Chrony SOCK:  %s", SOCK_PATH)
        # Authenticate synchronously (may block for up to 5 min on first-run
        # device-request flow). HTTP server will not come up until this finishes.
        try:
            token, mode = authenticate()
            STATE.signalk_auth_mode = mode
            log.info("Auth mode: %s (token=%s)", mode, "yes" if token else "no")
        except Exception as e:
            log.error("Auth failed: %s", e)
            STATE.last_error = f"auth: {e}"
            token = None
            STATE.signalk_auth_mode = "failed"
            STATE.signalk_needs_access = True
    else:
        log.info("SignalK:      disabled (SIGNALK_URL is blank)")

    # HTTP server.
    app = build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    log.info("HTTP server listening on :%d", HTTP_PORT)

    try:
        if SIGNALK_ENABLED:
            # Always start the WS loop. If auth failed, the loop's probe will
            # detect that and surface the button; user can then request
            # access via UI.
            await run_signalk(ChronySock(SOCK_PATH), token)
        else:
            await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
