#!/bin/bash
# Render chrony.conf from template based on env vars, then launch chronyd + bridge.
set -eu

# ---- Render chrony config ---------------------------------------------------

# Always allow loopback — the bridge queries chrony's NTP server on 127.0.0.1
# to show LAN clients what time they're actually getting served.
# Also always allow the Docker bridge subnet (172.16.0.0/12 covers all default
# Docker-created bridges). When the Docker host itself queries us via port-
# forwarding (host's systemd-timesyncd pointing at 127.0.0.1:123), chrony
# sees the source address as the docker bridge gateway, not 127.0.0.1.
# Without this, host-originated queries silently fail.
ALLOW_BLOCK="allow 127.0.0.1\nallow 172.16.0.0/12\n"
if [ -n "${NTP_ALLOW_SUBNETS:-}" ]; then
    OLDIFS="$IFS"
    IFS=','
    for subnet in $NTP_ALLOW_SUBNETS; do
        subnet="$(echo "$subnet" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        [ -n "$subnet" ] && ALLOW_BLOCK="${ALLOW_BLOCK}allow ${subnet}\n"
    done
    IFS="$OLDIFS"
fi
ALLOW_BLOCK_RENDERED="$(printf '%b' "$ALLOW_BLOCK")"

POOL_BLOCK=""
if [ -n "${NTP_FALLBACK_POOL:-}" ]; then
    POOL_BLOCK="pool ${NTP_FALLBACK_POOL} iburst maxsources 4"
fi

# ---- Time source directives -----------------------------------------------
# Let chrony's native selection algorithm pick the best source based on
# stratum, dispersion, and majority agreement. No prefer/trust overrides:
# if a source disagrees with the majority beyond its error bounds, chrony
# correctly marks it a falseticker. With three sources (SIGK + Starlink +
# pool), the majority vote handles this cleanly.
if [ -n "${SIGNALK_URL:-}" ]; then
    REFCLOCK_GPS="refclock SOCK /var/run/chrony.gps.sock refid SIGK precision 0.3 delay 0.1 filter 32 poll 3 dpoll -1"
else
    REFCLOCK_GPS="# (SignalK source not configured; set SIGNALK_URL to enable)"
fi

if [ -n "${USB_GPS_DEVICE:-}" ]; then
    REFCLOCK_USB="refclock SOCK /var/run/chrony.usbgps.sock refid NMEA precision 0.1 delay 0.2 filter 16 poll 4 dpoll 0"
else
    REFCLOCK_USB="# (USB GPS source not configured; set USB_GPS_DEVICE to enable)"
fi

if [ -n "${STARLINK_NTP_IP:-}" ]; then
    STARLINK_BLOCK="server ${STARLINK_NTP_IP} iburst minpoll 4 maxpoll 6"
else
    STARLINK_BLOCK="# (Starlink NTP source not configured; set STARLINK_NTP_IP to enable)"
fi

if [ "${SET_HOST_CLOCK:-false}" = "true" ]; then
    MAKESTEP="makestep 1.0 3"
    RTCSYNC="rtcsync"
else
    MAKESTEP="makestep 0 0"
    RTCSYNC=""
fi

# Remove any stale pidfile from a previous run that was SIGKILL'd before cleanup.
rm -f /run/chrony/chronyd.pid

# Ensure /run/chrony exists with correct ownership and mode (0750).
# chrony silently refuses to create its Unix command socket if the parent
# directory mode is not exactly 0750. /run is tmpfs so this needs to happen
# at every container start, not just at image build time.
mkdir -p /run/chrony
chown _chrony:_chrony /run/chrony
chmod 0750 /run/chrony

sed \
    -e "s|@FALLBACK_POOL@|${POOL_BLOCK}|" \
    -e "s|@MAKESTEP@|${MAKESTEP}|" \
    -e "s|@RTCSYNC@|${RTCSYNC}|" \
    -e "s|@REFCLOCK_GPS@|${REFCLOCK_GPS}|" \
    -e "s|@REFCLOCK_USB@|${REFCLOCK_USB}|" \
    -e "s|@STARLINK@|${STARLINK_BLOCK}|" \
    /app/chrony.conf.template > /tmp/chrony.conf.partial

awk -v block="$ALLOW_BLOCK_RENDERED" '
    /@ALLOW_SUBNETS@/ { printf "%s", block; next }
    { print }
' /tmp/chrony.conf.partial > /etc/chrony/chrony.conf

echo "==== Rendered /etc/chrony/chrony.conf ===="
cat /etc/chrony/chrony.conf
echo "=========================================="

# ---- Launch chronyd ---------------------------------------------------------

chronyd -d -f /etc/chrony/chrony.conf &
CHRONY_PID=$!

# Wait for chronyd to create the SOCK files the bridge feeds.
SOCKS=""
[ -n "${SIGNALK_URL:-}" ] && SOCKS="$SOCKS /var/run/chrony.gps.sock"
[ -n "${USB_GPS_DEVICE:-}" ] && SOCKS="$SOCKS /var/run/chrony.usbgps.sock"

for sock in $SOCKS; do
    for i in 1 2 3 4 5 6 7 8 9 10; do
        [ -S "$sock" ] && break
        sleep 0.5
    done

    if [ ! -S "$sock" ]; then
        echo "ERROR: chrony did not create $sock" >&2
        kill $CHRONY_PID 2>/dev/null || true
        exit 1
    fi
done

# ---- Launch bridge ----------------------------------------------------------

python3 -u -m bridge &
BRIDGE_PID=$!

trap 'kill -TERM $CHRONY_PID $BRIDGE_PID 2>/dev/null; wait; exit 0' TERM INT

# If either process dies, exit so the container restarts.
wait -n
EXIT=$?
echo "A child process exited with code $EXIT, shutting down." >&2
kill $CHRONY_PID $BRIDGE_PID 2>/dev/null || true
wait
exit $EXIT
