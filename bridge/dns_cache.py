"""Reverse-DNS cache for chronyc clients display.

Using chronyc without `-n` triggers reverse DNS inside chronyc itself and
blocks the whole subprocess call per-IP. On a boat LAN with spotty DNS, one
slow lookup can make `chronyc clients` take 5-10+ seconds. We do better by
calling chronyc with `-n` (instant) and doing reverse DNS ourselves with a
persistent cache.

Cache semantics:
- First lookup for an IP: synchronous socket call with a short timeout.
- All subsequent lookups for that IP: instant dict read, regardless of
  whether the original resolution succeeded.
- Failed lookups cache the IP itself as the "hostname" — we don't retry
  indefinitely since failing DNS on this LAN is usually a permanent thing
  (device has no PTR record). A bridge restart clears the cache.
"""

import logging
import socket


log = logging.getLogger(__name__)

_LOOKUP_TIMEOUT = 2.0  # seconds
_CACHE: dict[str, str] = {}


def lookup(ip: str) -> str:
    """Return hostname for ip, or ip itself if no PTR record / timeout."""
    if ip in _CACHE:
        return _CACHE[ip]

    # socket.gethostbyaddr() doesn't take a timeout parameter directly;
    # set the default socket timeout for the duration of the call.
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_LOOKUP_TIMEOUT)
    try:
        host, _, _ = socket.gethostbyaddr(ip)
        _CACHE[ip] = host
        log.debug("DNS resolved %s -> %s", ip, host)
        return host
    except (socket.herror, socket.gaierror, socket.timeout, OSError) as e:
        log.debug("DNS lookup for %s failed (%s); caching as IP", ip, e)
        _CACHE[ip] = ip  # cache the failure too — don't retry
        return ip
    finally:
        socket.setdefaulttimeout(old_timeout)
