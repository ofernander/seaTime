# boat-time

Dockerized NTP server that sources time datum from SignalK or Starlink


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
                            │        ┌────────────────────────┐
Starlink dish ──NTP─────────┼──────► │ Python bridge            │
 (192.168.100.1)            │        │  ├─ subscribes SignalK   │
                            │        │  ├─ feeds chrony SOCK    │
Optional pool ──NTP─────────┘        │  └─ serves web UI :8123  │
                                     │           │              │
                                     │           ▼              │
                                     │    chronyd               │
                                     │     ├─ refclock (SIGK)   │
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
# edit .env — at minimum set SIGNALK_URL and NTP_ALLOW_SUBNETS
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
| `SIGNALK_URL` | (required) | SignalK server URL, e.g. `http://192.168.1.50:3000` |
| `SIGNALK_PATH` | `navigation.datetime` | SignalK path providing GPS datetime |
| `SIGNALK_USERNAME` | (blank) | Optional. Pair with `SIGNALK_PASSWORD` for password auth |
| `SIGNALK_PASSWORD` | (blank) | Optional. Pair with `SIGNALK_USERNAME` for password auth |
| `SIGNALK_REQUEST_ACCESS` | `false` | Optional. Set to `true` to auto-trigger the device access request flow at startup. Mutually exclusive with `SIGNALK_USERNAME`/`SIGNALK_PASSWORD` |
| `NTP_ALLOW_SUBNETS` | `192.168.0.0/16,10.0.0.0/8` | CIDRs allowed to query NTP |
| `NTP_FALLBACK_POOL` | `pool.ntp.org` | Pool used when GPS is unavailable. Blank = disabled |
| `STARLINK_NTP_IP` | (blank) | Starlink dish IP (`192.168.100.1`) to use its stratum-1 NTP server |
| `SET_HOST_CLOCK` | `false` | Let chrony discipline the host clock |
| `HOST_TZ_FILE` | `/etc/timezone` | Path inside the container to a file with the host's IANA zone. Bind-mounted from the host in `docker-compose.yml` |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Time sources

The container can pull GPS time from two sources:

**NTP Pools** - Traditional NTP pools from the internet set with `NTP_FALLBACK_POOL` default is `pool.ntp.org`

**SignalK** - Subscribes to `navigation.datetime` on your SignalK server over WebSocket.

**Starlink** - Starlink user terminals run a stratum-1
NTP server on their LAN (port 123, typically `192.168.100.1`). Enabled by setting `STARLINK_NTP_IP` in `.env`.


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

**Bridge shows `auth_failed` / access-request button doesn't disappear after
approval:** check that `/data/signalk-token` was written inside the container.
The next WebSocket reconnect picks up the new token automatically (no restart
needed).

## License

GPL-3.0
