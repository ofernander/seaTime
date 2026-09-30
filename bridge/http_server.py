"""HTTP server: serves /api/status (JSON) and / (web UI)."""

import logging

from aiohttp import web

from bridge.config import STATIC_DIR, SIGNALK_ENABLED
from bridge.state import STATE
from bridge import chronyc, ntp_query
from bridge.signalk_auth import start_access_request


log = logging.getLogger(__name__)


async def handle_index(request: web.Request) -> web.Response:
    index = STATIC_DIR / "index.html"
    if index.exists():
        return web.FileResponse(index)
    return web.Response(
        text=(
            "signalk-time-server bridge running.\n\n"
            "Web UI not yet installed (bridge/static/index.html).\n"
            "JSON status: /api/status\n"
        ),
        content_type="text/plain",
    )


async def handle_status(request: web.Request) -> web.Response:
    data = STATE.snapshot()
    data["chrony"] = chronyc.snapshot()
    data["served"] = ntp_query.snapshot()
    return web.json_response(data)


async def handle_request_access(request: web.Request) -> web.Response:
    if not SIGNALK_ENABLED:
        return web.json_response(
            {"ok": False, "state": "error", "error": "SignalK is disabled"},
            status=409,
        )
    result = await start_access_request()
    status = 202 if result.get("ok") else 409
    return web.json_response(result, status=status)


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/request-access", handle_request_access)
    if STATIC_DIR.exists():
        app.router.add_static("/static/", STATIC_DIR, show_index=False)
    return app
