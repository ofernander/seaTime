"""SignalK authentication.

Three startup modes:
  1. No auth (readonly access or security disabled): leave env vars blank.
  2. Username/password: SIGNALK_USERNAME + SIGNALK_PASSWORD.
  3. Device access request: SIGNALK_REQUEST_ACCESS=true. First-run approval
     in SignalK UI; token persisted to /data/signalk-token.

Plus an async on-demand access-request flow driven by the web UI button:
  - `start_access_request()` posts the request, updates STATE to pending.
  - `_poll_access_request()` runs as a background task, updates STATE when
     approved/denied, persists token on approval.
"""

import asyncio
import logging
import time
import uuid
from typing import Optional
from urllib.parse import urlparse, urlunparse

import requests

from bridge.config import (
    SIGNALK_URL,
    SIGNALK_PATH,
    SIGNALK_USERNAME,
    SIGNALK_PASSWORD,
    SIGNALK_REQUEST_ACCESS,
    TOKEN_PATH,
    CLIENT_ID_PATH,
    DATA_DIR,
    ACCESS_REQUEST_TIMEOUT_SEC,
)
from bridge.state import STATE


log = logging.getLogger(__name__)


# ---- Utilities -------------------------------------------------------------

def http_base() -> str:
    """Return http(s) base URL (scheme + netloc only) from SIGNALK_URL."""
    parsed = urlparse(SIGNALK_URL)
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def admin_access_requests_url() -> str:
    """SignalK admin UI page where a user approves access requests.

    NOTE: SignalK's admin UI uses hash-based routing. This path is based on
    current SignalK server versions. If the SignalK admin UI route changes,
    clicking the UI's link will land on the admin home and the user can
    navigate to Security -> Access Requests manually.
    """
    return f"{http_base()}/admin/#/security/access-requests"


async def probe_readonly_access() -> str:
    """Probe whether SignalK grants readonly access to our configured path.

    Returns:
        "ok"           - path is readable without auth, no button needed
        "auth_needed"  - got 401/403, button should appear
        "error"        - something else went wrong (network, 404, 500)

    Uses the REST endpoint derived from SIGNALK_PATH, e.g.
      navigation.datetime -> /signalk/v1/api/vessels/self/navigation/datetime
    """
    rest_path = SIGNALK_PATH.replace(".", "/")
    url = f"{http_base()}/signalk/v1/api/vessels/self/{rest_path}"
    try:
        r = await asyncio.to_thread(requests.get, url, timeout=10)
    except Exception as e:
        log.warning("Readonly probe error: %s", e)
        return "error"

    if r.status_code in (401, 403):
        log.info("Readonly probe: auth needed (HTTP %d)", r.status_code)
        return "auth_needed"
    if r.status_code == 200:
        log.info("Readonly probe: ok")
        return "ok"
    log.warning("Readonly probe unexpected status: HTTP %d", r.status_code)
    return "error"


def load_or_create_client_id() -> str:
    """Stable v4 UUID for this bridge instance (persisted to /data)."""
    if CLIENT_ID_PATH.exists():
        return CLIENT_ID_PATH.read_text().strip()
    cid = str(uuid.uuid4())
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CLIENT_ID_PATH.write_text(cid)
    return cid


def persist_token(token: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token)


def load_persisted_token() -> Optional[str]:
    if not TOKEN_PATH.exists():
        return None
    t = TOKEN_PATH.read_text().strip()
    return t or None


# ---- Startup-time auth (synchronous) ---------------------------------------

def _auth_password() -> str:
    url = f"{http_base()}/signalk/v1/auth/login"
    log.info("Authenticating with username/password at %s", url)
    r = requests.post(
        url,
        json={"username": SIGNALK_USERNAME, "password": SIGNALK_PASSWORD},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    token = body.get("token") or body.get("login", {}).get("token")
    if not token:
        raise RuntimeError(f"No token in login response: {body}")
    return token


def _auth_device_request_blocking() -> str:
    """Startup-time device access request - blocks up to ACCESS_REQUEST_TIMEOUT_SEC."""
    existing = load_persisted_token()
    if existing:
        log.info("Reusing persisted device-request token from %s", TOKEN_PATH)
        return existing

    client_id = load_or_create_client_id()
    url = f"{http_base()}/signalk/v1/access/requests"
    log.info("Requesting device access at %s (clientId=%s)", url, client_id)
    r = requests.post(
        url,
        json={
            "clientId": client_id,
            "description": "signalk-time-server NTP bridge",
        },
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    href = body.get("href")
    if not href:
        raise RuntimeError(f"No href in access request response: {body}")
    poll_url = f"{http_base()}{href}"
    log.info("Waiting for admin approval in SignalK UI (Security > Access Requests)")
    log.info("Polling %s (timeout %.0fs)", poll_url, ACCESS_REQUEST_TIMEOUT_SEC)

    deadline = time.time() + ACCESS_REQUEST_TIMEOUT_SEC
    while time.time() < deadline:
        time.sleep(5)
        rr = requests.get(poll_url, timeout=10)
        rr.raise_for_status()
        resp = rr.json()
        state = resp.get("state")
        if state == "COMPLETED":
            access = resp.get("accessRequest", {})
            permission = access.get("permission")
            if permission == "APPROVED":
                token = access.get("token")
                if not token:
                    raise RuntimeError("APPROVED but no token in response")
                persist_token(token)
                log.info("Device access approved; token persisted to %s", TOKEN_PATH)
                return token
            raise RuntimeError(f"Device access {permission}")

    raise TimeoutError("Timed out waiting for device access approval")


def authenticate() -> tuple[Optional[str], str]:
    """Startup-time entry point. Returns (token, auth_mode)."""
    # Always try a persisted token first - if user approved via the UI button
    # earlier and restarted the container, reuse that token.
    existing = load_persisted_token()
    if existing:
        log.info("Reusing persisted token from %s", TOKEN_PATH)
        return existing, "device-request"

    if SIGNALK_USERNAME and SIGNALK_PASSWORD:
        return _auth_password(), "password"
    if SIGNALK_REQUEST_ACCESS:
        return _auth_device_request_blocking(), "device-request"
    return None, "none"


# ---- On-demand access-request flow (async, driven by UI) -------------------

_access_request_task: Optional[asyncio.Task] = None


def access_request_in_progress() -> bool:
    return _access_request_task is not None and not _access_request_task.done()


async def start_access_request() -> dict:
    """Send the access request. Kicks off background polling.

    Returns a dict summarizing what happened:
      {"ok": True,  "state": "pending"}
      {"ok": False, "state": "error", "error": "..."}
    Called from the HTTP POST handler.
    """
    global _access_request_task

    if access_request_in_progress():
        return {"ok": False, "state": "pending", "error": "request already in progress"}

    client_id = load_or_create_client_id()
    url = f"{http_base()}/signalk/v1/access/requests"
    log.info("Starting access request (clientId=%s)", client_id)

    try:
        r = await asyncio.to_thread(
            requests.post,
            url,
            json={
                "clientId": client_id,
                "description": "signalk-time-server NTP bridge",
            },
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        log.error("Access request POST failed: %s", msg)
        STATE.access_request_state = "idle"
        STATE.access_request_error = msg
        return {"ok": False, "state": "error", "error": msg}

    href = body.get("href")
    if not href:
        msg = f"No href in response: {body}"
        STATE.access_request_state = "idle"
        STATE.access_request_error = msg
        return {"ok": False, "state": "error", "error": msg}

    poll_url = f"{http_base()}{href}"
    STATE.access_request_state = "pending"
    STATE.access_request_error = None
    STATE.access_request_approve_url = admin_access_requests_url()
    log.info("Access request pending; polling %s", poll_url)

    _access_request_task = asyncio.create_task(_poll_access_request(poll_url))
    return {"ok": True, "state": "pending"}


async def _poll_access_request(poll_url: str):
    """Background poll. Updates STATE when approved/denied, writes token on approval."""
    deadline = time.time() + ACCESS_REQUEST_TIMEOUT_SEC
    try:
        while time.time() < deadline:
            await asyncio.sleep(5)
            try:
                rr = await asyncio.to_thread(requests.get, poll_url, timeout=10)
                rr.raise_for_status()
                resp = rr.json()
            except Exception as e:
                log.warning("Access poll error (will retry): %s", e)
                continue

            if resp.get("state") != "COMPLETED":
                continue

            access = resp.get("accessRequest", {})
            permission = access.get("permission")
            if permission == "APPROVED":
                token = access.get("token")
                if not token:
                    STATE.access_request_state = "denied"
                    STATE.access_request_error = "APPROVED but no token in response"
                    return
                persist_token(token)
                STATE.access_request_state = "approved"
                STATE.access_request_error = None
                STATE.signalk_needs_access = False
                log.info("Access approved; token persisted")
                return
            else:
                STATE.access_request_state = "denied"
                STATE.access_request_error = f"permission={permission}"
                log.info("Access %s", permission)
                return

        STATE.access_request_state = "denied"
        STATE.access_request_error = "timed out waiting for admin approval"
        log.warning("Access request timed out")
    except asyncio.CancelledError:
        raise
