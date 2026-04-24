"""Environment variable configuration for the bridge."""

import json
import os
from pathlib import Path


SIGNALK_URL = os.environ.get("SIGNALK_URL", "http://CHANGE_ME:3000").rstrip("/")
SIGNALK_PATH = os.environ.get("SIGNALK_PATH", "navigation.datetime")
SIGNALK_USERNAME = os.environ.get("SIGNALK_USERNAME", "").strip()
SIGNALK_PASSWORD = os.environ.get("SIGNALK_PASSWORD", "").strip()
SIGNALK_REQUEST_ACCESS = os.environ.get("SIGNALK_REQUEST_ACCESS", "").lower() == "true"

# Starlink NTP source (optional). Empty string = disabled.
STARLINK_NTP_IP = os.environ.get("STARLINK_NTP_IP", "").strip()

SOCK_PATH = "/var/run/chrony.gps.sock"
HTTP_PORT = 8123

DATA_DIR = Path("/data")
TOKEN_PATH = DATA_DIR / "signalk-token"
CLIENT_ID_PATH = DATA_DIR / "signalk-client-id"
STATIC_DIR = Path("/app/bridge/static")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Tunables
STALE_THRESHOLD_SEC = 10.0
RECONNECT_MIN_SEC = 1.0
RECONNECT_MAX_SEC = 60.0
ACCESS_REQUEST_TIMEOUT_SEC = 300.0

# Read version + repo URL from package.json at import time. Fall back
# gracefully if the file is missing or malformed so the bridge still starts.
_PACKAGE_JSON = Path("/app/package.json")
try:
    _pkg = json.loads(_PACKAGE_JSON.read_text())
    VERSION = _pkg.get("version", "?")
    HOMEPAGE = _pkg.get("homepage", "")
except (OSError, json.JSONDecodeError):
    VERSION = "?"
    HOMEPAGE = ""
