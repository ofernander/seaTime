"""Entry point for `python3 -m bridge`.

Initializes logging, authenticates to SignalK, starts the HTTP server,
and runs the SignalK client loop forever.
"""

import asyncio
import logging
import sys

from aiohttp import web

from bridge.config import SIGNALK_URL, SIGNALK_PATH, SOCK_PATH, HTTP_PORT, LOG_LEVEL
from bridge.state import STATE
from bridge.chrony_sock import ChronySock
from bridge.signalk_auth import authenticate
from bridge.signalk_client import run as run_signalk
from bridge.http_server import build_app


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bridge")


async def main():
    log.info("boat-time bridge starting")
    log.info("SignalK URL:  %s", SIGNALK_URL)
    log.info("SignalK path: %s", SIGNALK_PATH)
    log.info("Chrony SOCK:  %s", SOCK_PATH)
    log.info("HTTP port:    %d", HTTP_PORT)

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

    chrony = ChronySock(SOCK_PATH)

    # HTTP server.
    app = build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    log.info("HTTP server listening on :%d", HTTP_PORT)

    # SignalK client loop.
    # Always start the WS loop. If auth failed, the loop's probe will detect
    # that and surface the button; user can then request access via UI.
    ws_task = asyncio.create_task(run_signalk(chrony, token))

    try:
        await ws_task
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
