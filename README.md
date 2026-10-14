# parking-guard

[![check](https://github.com/lanbat/nixos-lanbat-justpark-parking/actions/workflows/check.yml/badge.svg)](https://github.com/lanbat/nixos-lanbat-justpark-parking/actions/workflows/check.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![IRC: #lanbat on Libera.Chat](https://img.shields.io/badge/IRC-%23lanbat%20on%20Libera.Chat-2f4bff.svg)](https://web.libera.chat/#lanbat)

**Know the moment an unbooked car takes a paid parking space.**

parking-guard watches your car park through [Frigate](https://frigate.video/)'s licence-plate
recognition, checks every plate against live [JustPark](https://www.justpark.com/) bookings and a
resident allowlist, and alerts you when a car turns up that should not be there. It ships as a
NixOS flake plugin for [lanbat/nixconf](https://github.com/lanbat/nixconf): one flake input and one
config block.

Part of [LAN BAT](https://lanbat.github.io/), open home infrastructure.

## How it works

```text
JustPark API / bookings.csv ─┐
resident plates ─────────────┴─▶ SQLite allowlist
                                       │
Frigate LPR ── MQTT ─▶ parking-guard ──┤ authorised? within booking + grace?
                                       │
                                       ├─▶ parking-guard/event   every decision
                                       └─▶ parking-guard/alert   unauthorised only ─▶ Home Assistant, Pushover, Telegram
```

1. Syncs **JustPark** bookings (partner API, or a CSV export) and resident plates into a local SQLite allowlist.
2. Subscribes to **Frigate LPR** events on MQTT (`frigate/tracked_object_update`).
3. Publishes `parking-guard/alert` when an unrecognised plate appears, with a grace period and a per-plate cooldown.
4. Optionally notifies via **Pushover** or **Telegram**, and plugs into **Home Assistant** automations over MQTT.

Requires `lanbatPlugins.services` on the same host (Frigate, Mosquitto, Home Assistant).

## Enable in your deployment

```nix
# flake.nix
inputs.lanbat-justpark-parking.url = "github:lanbat/nixos-lanbat-justpark-parking";
inputs.lanbat-justpark-parking.inputs.nixpkgs.follows = "nixpkgs";
```

```nix
# deployments/homelab/deploy.nix
hosts.server.plugins = [
  inputs.self.lanbatPlugins.services
  inputs.lanbat-justpark-parking.lanbatPlugin
];

deployment.parkingGuard = {
  siteId = "YOUR_JUSTPARK_SITE_ID";
  cameras = [ "c1" ];
  graceMinutes = 5;
  cooldownMinutes = 30;
  residentPlates = [ "AB12 CDE" ];  # optional permanent allowlist
};
```

## Secrets (agenix)

Add to your lanbat `secrets/secrets.nix` and create encrypted files:

| Secret | Format |
|---|---|
| `justpark-api-env.age` | `JUSTPARK_API_KEY=…`, `JUSTPARK_USERNAME=…`, `JUSTPARK_PASSWORD=…`, `JUSTPARK_BASE_URL=https://partners.sandbox.parkhub.com` (optional until API access) |
| `parking-guard-mqtt-pass.age` | One-line plaintext MQTT password for user `parking-guard` |
| `parking-guard-notify-env.age` | Optional `PUSHOVER_TOKEN`, `PUSHOVER_USER`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

Without JustPark API credentials, sync still works via:

- **CSV**: drop `bookings.csv` at `/var/lib/parking-guard/import/bookings.csv` (columns: `plate`, `start`, `end`, `booking_ref`)
- **Residents**: `deployment.parkingGuard.residentPlates`

See [docs/home-assistant.md](docs/home-assistant.md) for MQTT automations.

## MQTT topics

| Topic | Payload |
|---|---|
| `parking-guard/allowlist/count` | Integer — active bookings |
| `parking-guard/event` | Every LPR decision `{ plate, authorized, reason, camera, frigate_id, score, ts }` |
| `parking-guard/alert` | Unauthorized only — same JSON shape |

## Development

```bash
nix flake check
nix build .#packages.x86_64-linux.parking-guard
```

## JustPark API

Partner API access is provisioned by JustPark (sandbox → certification → production).
See [ParkHub docs](https://docs.parkhub.com/). The client calls `GET /sessions?siteId=…` and
normalizes common response shapes; adjust `sessions_path` in code if your contract differs.
