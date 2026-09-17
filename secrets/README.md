# Plugin secrets

When this plugin is enabled, add these files to your **lanbat** repo `secrets/` directory
(not this plugin repo):

| File | Purpose |
|---|---|
| `justpark-api-env.age` | JustPark API credentials (optional until partner access) |
| `parking-guard-mqtt-pass.age` | Mosquitto password for `parking-guard` user |
| `parking-guard-notify-env.age` | Owner channels: Pushover, ntfy, Telegram bot + owner chat id |
| `parking-guard-messaging-env.age` | Client channels: WhatsApp, Telegram bot username, SMTP, SMS |

Both notify files are read directly from disk by the service (the unit points at them with
`PARKING_GUARD_NOTIFY_ENV` and `PARKING_GUARD_MESSAGING_ENV`) rather than through
`EnvironmentFile`, so tokens containing quotes or `#` are safe.

Keys are documented in the [plugin README](../README.md#secrets-agenix). Two worth calling
out:

- `NTFY_TOPIC` is the **owner's** notification topic. It is never used to contact a client.
- `deployment.parkingGuard.ack.topic` is a separate, unguessable topic used only as an
  inbound acknowledgement relay. Treat it as a secret: anyone who learns it can see ack
  tokens go past, though tokens are single-use and carry no personal data.

See the main [lanbat secrets inventory](../../nixos/secrets/secrets.nix.example).
