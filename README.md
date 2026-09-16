# lanbat-justpark-parking

External [lanbat](https://github.com/lanbat/nixos) server plugin that:

1. Syncs **JustPark** bookings (API or Luna CSV) and resident plates into a local SQLite allowlist
2. Subscribes to **Frigate LPR** events on MQTT (`frigate/tracked_object_update`)
3. Publishes `parking-guard/alert` when an unrecognized plate appears during an active booking window
4. Optionally notifies via **Pushover** or **Telegram**, and integrates with **Home Assistant** via MQTT

Requires `lanbatPlugins.services` on the same host (Frigate, Mosquitto, Home Assistant).

## Enable in your deployment

```nix
# flake.nix
inputs.lanbat-justpark-parking.url = "path:../lanbat-justpark-parking";
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
