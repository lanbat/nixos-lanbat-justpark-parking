# Plugin secrets

When this plugin is enabled, add these files to your **lanbat** repo `secrets/` directory
(not this plugin repo):

| File | Purpose |
|---|---|
| `justpark-api-env.age` | JustPark API credentials (optional until partner access) |
| `parking-guard-mqtt-pass.age` | Mosquitto password for `parking-guard` user |
| `parking-guard-notify-env.age` | Optional Pushover / Telegram tokens |

See the main [lanbat secrets inventory](../../nixos/secrets/secrets.nix.example) and
[plugin README](../README.md).
