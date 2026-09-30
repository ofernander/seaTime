# boat-time

Dockerized NTP server that sources time datum from SignalK, Starlink, or a USB GPS puck


![boat-time](assets/screenshots/screenshot.png)

## Why

If you're on a boat with a GPS on NMEA 2000 feeding a SignalK server, you
already have a GPS-disciplined time source. This project turns it into a real
LAN NTP server — so every device on your boat network stays in sync with UTC,
even when you're away from the internet for weeks.

Runs alongside SignalK as a separate container, so SignalK restarts or upgrades
don't affect your LAN's time service.

## Architecture

```
SignalK server ──WebSocket──┐           boat-time
                            │        ┌──────────────────────────┐
USB GPS puck ──NMEA/serial──┤        │ Python bridge            │
                            │        │  ├─ subscribes SignalK   │
Starlink dish ──NTP─────────┼──────► │  ├─ reads USB GPS NMEA   │
 (192.168.100.1)            │        │  ├─ feeds chrony SOCKs   │
Optional pool ──NTP─────────┘        │  └─ serves web UI :8123  │
                                     │           │              │
                                     │           ▼              │
                                     │    chronyd               │
                                     │     ├─ refclock (SIGK)   │
                                     │     ├─ refclock (NMEA)   │
                                     │     ├─ server (Starlink) │
                                     │     ├─ optional pool     │
                                     │     └─ serves UDP/123    │
                                     └──────────────────────────┘
                                               │
                                               ▼
                                         LAN NTP clients
```

## Ports

| Port | Protocol | Purpose |
|---|---|---|
| 123 | UDP | NTP (LAN clients) |
| 8123 | TCP | Web UI + JSON status API |

## Quick start

```bash
git clone https://github.com/ofernander/boat-time.git
cd boat-time
cp .env.example .env
# edit .env — at minimum set NTP_ALLOW_SUBNETS and one time source
# (SIGNALK_URL, USB_GPS_DEVICE, or STARLINK_NTP_IP)
docker compose up -d
docker compose logs -f
```

Open `http://<host>:8123/` for the status page. From any LAN client:

```bash
sntp -d <host>
# or
chronyc -h <host> tracking
```

## SignalK

Set `SIGNALK_URL` in `.env` to your SignalK server's reachable address — a LAN
IP or hostname works in all cases:

```
SIGNALK_URL=http://signalk-ip/hostname:3000
```

**Optional shortcut** for same-host deployments: add
`extra_hosts: ["host.docker.internal:host-gateway"]` to `docker-compose.yml`,
then use `SIGNALK_URL=http://host.docker.internal:3000`. Saves you from
looking up the host's LAN IP.

## USB GPS puck

Any USB GPS receiver that speaks NMEA 0183 works — u-blox 7/8 pucks
(VK-162, VK-172, GT-U7), GlobalSat BU-353, and similar. The bridge reads
`RMC` sentences, and for each valid fix it sends that fix's UTC time to chrony
as the `NMEA` refclock.

1. Plug the puck into the Docker host and find its stable path:

   ```bash
   ls -l /dev/serial/by-id/
   ```

2. Pass it through in `docker-compose.yml`, mapping it to `/dev/gps0` inside
   the container:

   ```yaml
   devices:
     - /dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00:/dev/gps0
   ```

3. Set `USB_GPS_DEVICE=/dev/gps0` in `.env` and `docker compose up -d`.

The baud rate is auto-detected unless `USB_GPS_BAUD` is set. The puck needs
sky view to get a fix. Until it has one, the web UI shows **no fix** and no
samples are sent.

**Accuracy.** USB pucks don't expose a PPS signal, so time comes from when
each NMEA sentence arrives. That arrival lags the top of the second by a
fairly constant amount (typically 50–200ms). To calibrate it, run with
Starlink or the pool also enabled and let chrony settle for a few minutes.
Then set `USB_GPS_OFFSET` to the USB GPS source's typical **Last sample**
offset in the Sources table (e.g. `+118ms` → `USB_GPS_OFFSET=0.118`).
Calibrated, expect roughly 10–50ms — better than SignalK, worse than
Starlink.

**Hot-plug.** The bridge reconnects automatically if the puck is unplugged
and replugged. Docker's `devices:` mapping is fixed when the container is
created, so if the host enumerates the puck as a different node after
replugging, restart the container.

## SignalK authentication

The bridge supports three ways to authenticate to SignalK. Pick whichever
matches your SignalK security settings:

1. **No auth / readonly access** — leave all auth env vars blank. Works when
   SignalK has "Allow Readonly Access" enabled, or security is disabled. On
   startup the bridge probes the configured path with a plain HTTP GET; if it
   gets a 200, it proceeds without auth.

2. **Username/password** — set `SIGNALK_USERNAME` + `SIGNALK_PASSWORD`. Bridge
   logs in at startup, gets a token, uses it for the WebSocket.

3. **Device access request** — if the probe returns 401/403 and no
   username/password is set, the web UI shows a **Request SignalK access**
   button. Click it, approve the request in SignalK's admin UI (Security →
   Access Requests), and the bridge picks up the new token and reconnects.
   Token is persisted to `/data/signalk-token` so subsequent starts don't
   need re-approval.

   For unattended deployments you can pre-trigger the flow by setting
   `SIGNALK_REQUEST_ACCESS=true` in `.env` — the bridge will post the access
   request at startup and wait for approval.

## Setting the host clock (optional)

There are two ways to get the Docker host's own clock synced to boat-time.
Pick one, not both.

**Approach A: Let the host's existing NTP client point at boat-time** (recommended).

The host's existing NTP client (usually `systemd-timesyncd` on Debian/Ubuntu)
queries boat-time on `127.0.0.1:123` like any other LAN client. Cleanest
separation — the container stays unprivileged relative to the host clock.

```bash
# Edit /etc/systemd/timesyncd.conf on the host:
#   NTP=127.0.0.1
#   FallbackNTP=pool.ntp.org   # optional — used if container is down
sudo systemctl restart systemd-timesyncd

# Verify:
timedatectl timesync-status
# Should show: Server: 127.0.0.1 (127.0.0.1), Stratum: 2+, Packet count: 1+
```

**Approach B: Let the container discipline the host clock directly.**

Set `SET_HOST_CLOCK=true` in `.env`. The container uses its `CAP_SYS_TIME`
capability to step/slew the host's kernel clock directly. You MUST disable
the host's own NTP client first, or two things will fight over the clock:

```bash
sudo systemctl disable --now systemd-timesyncd
```

Default (`SET_HOST_CLOCK=false`) means the container serves NTP to the
LAN but doesn't touch the host clock — suitable for Approach A or for hosts
that don't need their clock disciplined by boat-time at all.

## Env vars

See `.env.example` for the full annotated list. The essentials:

| Variable | Default | Purpose |
|---|---|---|
| `SIGNALK_URL` | (blank) | SignalK server URL, e.g. `http://192.168.1.50:3000`. Blank = SignalK source disabled |
| `SIGNALK_PATH` | `navigation.datetime` | SignalK path providing GPS datetime |
| `SIGNALK_USERNAME` | (blank) | Optional. Pair with `SIGNALK_PASSWORD` for password auth |
| `SIGNALK_PASSWORD` | (blank) | Optional. Pair with `SIGNALK_USERNAME` for password auth |
| `SIGNALK_REQUEST_ACCESS` | `false` | Optional. Set to `true` to auto-trigger the device access request flow at startup. Mutually exclusive with `SIGNALK_USERNAME`/`SIGNALK_PASSWORD` |
| `NTP_ALLOW_SUBNETS` | `192.168.0.0/16,10.0.0.0/8` | CIDRs allowed to query NTP |
| `NTP_FALLBACK_POOL` | `pool.ntp.org` | Pool used when GPS is unavailable. Blank = disabled |
| `USB_GPS_DEVICE` | (blank) | Path inside the container to a USB GPS puck, e.g. `/dev/gps0`. Blank = disabled |
| `USB_GPS_BAUD` | (auto) | Serial baud rate. Blank = auto-detect (9600, 4800, 38400, 115200) |
| `USB_GPS_OFFSET` | `0` | Seconds of NMEA arrival delay to subtract. See [USB GPS puck](#usb-gps-puck) |
| `STARLINK_NTP_IP` | (blank) | Starlink dish IP (`192.168.100.1`) to use its stratum-1 NTP server |
| `SET_HOST_CLOCK` | `false` | Let chrony discipline the host clock |
| `HOST_TZ_FILE` | `/etc/timezone` | Path inside the container to a file with the host's IANA zone. Bind-mounted from the host in `docker-compose.yml` |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Time sources

The container can pull time from these sources:

**NTP Pools** - Traditional NTP pools from the internet set with `NTP_FALLBACK_POOL` default is `pool.ntp.org`

**SignalK** - Subscribes to `navigation.datetime` on your SignalK server over WebSocket.

**Starlink** - Starlink user terminals run a stratum-1
NTP server on their LAN (port 123, typically `192.168.100.1`). Enabled by setting `STARLINK_NTP_IP` in `.env`.

**USB GPS puck** - Reads NMEA directly from a USB GPS receiver passed through to the container. Enabled by setting `USB_GPS_DEVICE` in `.env`. See [USB GPS puck](#usb-gps-puck).


**NEMA2K via CAN adapter in the works**


## Web UI

`http://<host>:8123/` shows a live status page:


## Status API

`GET /api/status` returns a JSON snapshot for scripting:

```json
{
  "signalk": {
    "url": "...", "connected": true, "auth_mode": "device-request",
    "needs_access": false, "last_datetime_value": "...", "is_stale": false,
    "access_request": { "state": "idle", "approve_url": null, "error": null }
  },
  "chrony_socket": { "samples_sent": 1234, "last_sample_offset_seconds": -0.001 },
  "usb_gps": {
    "enabled": true, "device": "/dev/gps0", "baud": 9600, "connected": true,
    "has_fix": true, "satellites": 9, "last_datetime_value": "...",
    "is_stale": false, "samples_sent": 567, "last_error": null
  },
  "chrony": {
    "tracking": { ... }, "sources": [...], "clients": [...]
  },
  "uptime_seconds": 42.0
}
```

## Troubleshooting

- **Container logs:** `docker compose logs -f`
- **chrony tracking:** `docker exec boat-time chronyc tracking`
- **chrony sources:** `docker exec boat-time chronyc sources -v`
- **LAN clients:** `docker exec boat-time chronyc clients`

**SignalK source shows `^x` (falseticker):** expected when Starlink or pool
are reachable — SignalK's WebSocket-delivered time has ~100-200ms systematic
bias, and chrony correctly flags it a falseticker when more accurate sources
disagree. SignalK will still be used as a fallback when the others go away.

**UI shows "chrony unsynced · no working sources":** chrony hasn't committed
to a source yet. Wait 1–2 minutes after container start while reach climbs.

**USB GPS shows "disconnected" / "no NMEA data":** check the device is passed
through (`docker exec boat-time ls -l /dev/gps0`) and that nothing on the host
is holding it open. `gpsd` and `ModemManager` both like to grab USB serial
devices: `sudo systemctl disable --now gpsd.socket gpsd ModemManager`.

**USB GPS reports "implausible date (GPS week rollover?)":** older pucks with
SiRF III chipsets report dates ~19.6 years in the past since the 2019 GPS
week rollover. The receiver needs a firmware update or replacing.

**Bridge shows `auth_failed` / access-request button doesn't disappear after
approval:** check that `/data/signalk-token` was written inside the container.
The next WebSocket reconnect picks up the new token automatically (no restart
needed).

## License

GPL-3.0
