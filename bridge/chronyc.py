"""Subprocess wrapper and output parsers for `chronyc`.

Calls are cached for 1 second to avoid hammering chronyd when the UI polls
the status endpoint every 2 seconds.
"""

import logging
import re
import subprocess
import time

from bridge import dns_cache


log = logging.getLogger(__name__)


_CACHE: dict = {"at": 0.0, "data": None}
_CACHE_TTL = 1.0  # seconds


def _run_chronyc(*args: str) -> str:
    try:
        r = subprocess.run(
            ["chronyc", "-n", *args],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        log.debug("chronyc failed: %s", e)
        return ""


def _parse_tracking(text: str) -> dict:
    """`chronyc tracking` -> flat dict of {label: value}."""
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


# `chronyc sources -v` data row format:
#   MS Name/IP address    Stratum Poll Reach LastRx Last sample
#   #*  GPS                 0       4    377   12     -123us[-123us] +/- 500us
#
# Group names used:  ms, source, stratum, poll, reach, last_rx, sample
# (Keep the regex group names below in sync with the dict keys in _parse_sources.)
_SOURCES_RE = re.compile(
    r"^(?P<ms>\S{2})\s+"
    r"(?P<source>\S+)\s+"
    r"(?P<stratum>\d+)\s+"
    r"(?P<poll>\d+)\s+"
    r"(?P<reach>\d+)\s+"
    r"(?P<last_rx>\S+)\s+"
    r"(?P<sample>.+)$"
)


def _parse_sources(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = _SOURCES_RE.match(line)
        if not m:
            continue
        try:
            reach = int(m.group("reach"), 8)  # chronyc prints reach as octal
        except ValueError:
            reach = 0
        rows.append({
            "state": m.group("ms"),
            "name": m.group("source"),
            "stratum": int(m.group("stratum")),
            "poll": int(m.group("poll")),
            "reach": reach,
            "last_rx": m.group("last_rx"),
            "last_sample": m.group("sample").strip(),
        })
    return rows


_CLIENTS_RE = re.compile(
    r"^(?P<host>\S+)\s+"
    r"(?P<ntp>\d+)\s+"
    r"(?P<drop>\d+)\s+"
    r"(?P<int>-?\d+|-)\s+"
    r"(?P<intl>-?\d+|-)\s+"
    r"(?P<last>\S+)"
)


# Hosts to hide from the UI clients table. The bridge itself queries chrony
# on 127.0.0.1 to show served time; that traffic is infrastructure, not a
# real LAN client, so we filter it out.
_HIDDEN_CLIENT_HOSTS = {"localhost", "127.0.0.1", "::1", "ip6-localhost"}


def _parse_clients(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = _CLIENTS_RE.match(line)
        if not m:
            continue
        ip = m.group("host")
        if ip.lower() == "hostname":
            continue
        if ip.lower() in _HIDDEN_CLIENT_HOSTS:
            continue
        # chronyc was called with -n so host is a raw IP. Resolve to a
        # hostname via our own cache (first lookup blocks briefly, repeats
        # are instant).
        host = dns_cache.lookup(ip)
        rows.append({
            "host": host,
            "ntp_requests": int(m.group("ntp")),
            "drops": int(m.group("drop")),
            "last_seen": m.group("last"),
        })
    return rows


def _parse_ntpdata(text: str) -> dict[str, dict]:
    """`chronyc -n ntpdata` -> {server_ip: {label: value}}.

    With no address argument chronyc reports every NTP source, one
    blank-line-separated block each, starting with
    `Remote address  : 192.168.100.1 (C0A86401)`.
    """
    out = {}
    for block in text.split("\n\n"):
        fields = _parse_tracking(block)
        remote = fields.get("Remote address", "").split()
        if remote:
            out[remote[0]] = fields
    return out


def snapshot() -> dict:
    """Combined chronyc output. Cached 1s to protect chronyd."""
    now = time.time()
    if _CACHE["data"] and (now - _CACHE["at"] < _CACHE_TTL):
        return _CACHE["data"]
    data = {
        "tracking": _parse_tracking(_run_chronyc("tracking")),
        "sources": _parse_sources(_run_chronyc("sources", "-v")),
        "ntpdata": _parse_ntpdata(_run_chronyc("ntpdata")),
        "clients": _parse_clients(_run_chronyc("clients")),
    }
    _CACHE["at"] = now
    _CACHE["data"] = data
    return data
