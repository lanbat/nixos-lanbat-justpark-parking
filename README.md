# nixos-lanbat-justpark-parking

External [lanbat](https://github.com/lanbat/nixos) server plugin that:

1. Syncs **JustPark** bookings (API or Luna CSV) and resident plates into a local SQLite allowlist
2. Subscribes to **Frigate LPR** events on MQTT (`frigate/tracked_object_update`)
3. Publishes `parking-guard/alert` when an unrecognized plate appears during an active booking window
4. Watches `frigate/events` to check **how** each car parked, and messages the booking's client
   until they acknowledge or move the car
5. Integrates with **Home Assistant** via MQTT

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
  cameras = [ "driveway" ];
  graceMinutes = 5;
  cooldownMinutes = 30;
  residentPlates = [ "AB12 CDE" ];  # optional permanent allowlist
};
```

## Misparking detection

The driveway holds four cars on a match day: two rows of two, every car pulled up as
close to the house (and the camera) as it can get. Four rules run whenever a car has
been stationary for `settleFrames`:

| Violation | Meaning |
|---|---|
| `outside_bay` | The car is not inside any marked bay |
| `straddling` | The car covers two bays, so a neighbour's space is unusable |
| `not_pulled_forward` | The car stopped short of the house, or sits in a back bay while the one in front is empty |
| `blocking_aisle` | The car is stopped in the aisle or gateway |
| `forbidden_area` | The car is on a neighbour's property — always reported, even on a quiet day |

### Marking the bays

Draw the areas in Frigate's zone editor, then paste the same coordinates here.
Frigate decides zone membership from the **bottom-centre** of the bounding box, which
is the car's ground position nearest the camera. That single point is enough to say
"which bay" and "did they pull far enough forward" (draw `forwardZone` over just the
near-house end of the bay), but it cannot see a car straddling two bays — for that,
`polygon` is compared against the full bounding box.

```nix
deployment.parkingGuard = {
  frameWidth = 2560;      # the camera's detect resolution; `box` arrives in pixels
  frameHeight = 1440;
  evaluationMode = "hybrid";   # "zones" (Frigate only), "geometry" (polygons only), or both

  slots = [
    {
      name = "front_left";
      label = "the left-hand space nearest the house";
      row = 0;
      zone = "bay_front_left";                 # optional Frigate zone
      forwardZone = "bay_front_left_forward";  # optional: pulled-up-far-enough zone
      polygon = "0.20,0.60,0.55,0.60,0.55,1.00,0.20,1.00";
      forwardLine = 0.95;  # the car's near-house edge must reach this
    }
    { name = "front_right"; row = 0; polygon = "..."; forwardLine = 0.95; }
    { name = "rear_left";  row = 1; polygon = "..."; forwardLine = 0.55; pair = "front_left"; }
    { name = "rear_right"; row = 1; polygon = "..."; forwardLine = 0.55; pair = "front_right"; }
  ];

  areas = [
    { name = "neighbour_left"; label = "next door's drive"; polygon = "..."; }
    { name = "aisle"; kind = "aisle"; polygon = "..."; }
  ];
};
```

`pair` names the bay directly in front, so a car tucked in the back row while the space
ahead is empty is treated the same as one that stopped short.

If the camera is mounted so that "closer to the house" means **higher** in the frame,
set `forwardAxis = "up"`.

### When enforcement is active

Nobody gets messaged over an empty driveway. Incidents are only raised when at least
`enforcement.whenBookingsAtLeast` bookings are live (default 2), or when match day is
forced on from Home Assistant:

```yaml
service: mqtt.publish
data:
  topic: parking-guard/command/enforce
  payload: "on"     # "off" to silence, "auto" to go back to counting bookings
```

Parking on a neighbour's property ignores that gate entirely.

## Notifying the client

Channels are tried in order, and a channel with no endpoint for this booking is skipped
instantly rather than waiting out its timeout. **SMS sits last because it is the only
rung billed per message.**

| Rung | Channel | Audience | Notes |
|---|---|---|---|
| 1 | WhatsApp | client + owner | Meta Cloud API with an approved template, or a self-hosted WAHA session |
| 2 | Telegram | client + owner | Needs the client to have opened the bot once; earlier messages carry the invite link |
| 3 | Email | client + owner | Only if JustPark gave you an address |
| 4 | SMS | client + owner | Cloud API or an Android handset on the LAN |
| — | ntfy, Pushover | **owner only** | Never used to contact a client |

When the ladder is exhausted, or the booking has no contact details at all, the owner is
told instead. `notify.maxRounds` (default 2) gives the client a second lap before the
incident is closed as exhausted.

### Where contact details come from

JustPark's [owner agreement](https://www.justpark.com/owner-agreement) says shared booking
data "may include a Driver's name, telephone number and car registration" — an email address
is not promised, and the partner sessions API documents neither. So parking-guard reads
whatever the booking source provides, and you can fill the gaps by hand in
`/var/lib/parking-guard/contacts.yaml`:

```yaml
contacts:
  - plate: "AB12 CDE"
    name: Sam
    phone: "07700 900123"     # used for SMS, and for WhatsApp unless overridden
    email: sam@example.com
    telegram_chat_id: "5551234"   # filled in automatically once they tap the invite
```

The file is re-read within a minute of changing. Contact rows expire
`contacts.retentionDays` after the booking ends (default 7) and are purged on every sync.
Phone numbers are stored in full in the database but **masked** in the notification audit
trail, and never published to MQTT or ntfy.

### Acknowledgements without opening a port

Nothing reaches this host from the internet, so every message carries a one-tap link to
an unguessable ntfy topic that the guard subscribes to outbound:

```
https://ntfy.sh/<ack-topic>/trigger?message=<token>
```

Tokens are random and single-use, so a stranger who guessed the topic can only replay a
dead token. Telegram acknowledgements arrive through `getUpdates` long-polling (inline
button or `/start` deep link), and WAHA and the LAN SMS handset are polled for free-text
replies like "ok" or "moving it". An acknowledgement pauses the ladder for
`notify.ackGraceMinutes`; if the car still has not moved after that, the owner hears about it.

```nix
deployment.parkingGuard.ack.topic = "pg-ack-7f3a9c2b";  # keep this secret
deployment.parkingGuard.notify = {
  clientLadder = [ "whatsapp" "telegram" "email" "sms" ];
  ownerChannels = [ "ntfy" "pushover" ];
  ackGraceMinutes = 10;
  siteName = "21 Example Road";
};
```

## Secrets (agenix)

Add to your lanbat `secrets/secrets.nix` and create encrypted files:

| Secret | Format |
|---|---|
| `justpark-api-env.age` | `JUSTPARK_API_KEY=…`, `JUSTPARK_USERNAME=…`, `JUSTPARK_PASSWORD=…`, `JUSTPARK_BASE_URL=https://partners.sandbox.parkhub.com` (optional until API access) |
| `parking-guard-mqtt-pass.age` | One-line plaintext MQTT password for user `parking-guard` |
| `parking-guard-notify-env.age` | Owner channels: `PUSHOVER_TOKEN`, `PUSHOVER_USER`, `NTFY_TOPIC`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| `parking-guard-messaging-env.age` | Client channels — see below |

```sh
# parking-guard-messaging-env
WHATSAPP_TOKEN=EAAG...
WHATSAPP_PHONE_NUMBER_ID=123456789
WHATSAPP_TEMPLATE=parking_misparked
WHATSAPP_TEMPLATE_LANGUAGE=en_GB
TELEGRAM_BOT_USERNAME=my_parking_bot
SMTP_HOST=smtp.example.com
SMTP_USERNAME=parking@example.com
SMTP_PASSWORD=...
SMTP_FROM=parking@example.com
SMS_API_URL=https://api.example.com/v1/messages
SMS_API_AUTH_HEADER=Authorization
SMS_API_AUTH_VALUE=Bearer ...
SMS_API_FROM=Driveway
# or a handset on the LAN instead of a paid API
ANDROID_SMS_URL=http://192.168.1.50:8080
ANDROID_SMS_USERNAME=sms
ANDROID_SMS_PASSWORD=...
```

`SMS_API_BODY` describes any provider's request shape, so no new code is needed to switch:

```sh
SMS_API_BODY={"to": "{to}", "body": "{text}", "from": "{from}"}
```

### The WhatsApp template

Meta will not deliver a business-initiated message without an approved template, and it
only delivers replies to a public webhook, which this deployment does not have. So create
a **utility** template with two body variables and one **dynamic URL button**:

- Body: `{{1}}, {{2}}`
- Button URL: `https://ntfy.sh/<ack-topic>/trigger?message={{1}}`

parking-guard fills the button suffix with the ack token. Set `WAHA_URL` instead if you
would rather run a self-hosted WhatsApp Web session — two-way and free-form, but
unofficial and the number can be banned.

## Without JustPark API credentials

Sync still works via:

- **CSV**: drop `bookings.csv` at `/var/lib/parking-guard/import/bookings.csv`
  (columns: `plate`, `start`, `end`, `booking_ref`, and optionally `name`, `phone`, `email`)
- **Residents**: `deployment.parkingGuard.residentPlates`

See [docs/home-assistant.md](docs/home-assistant.md) for MQTT automations.

## MQTT topics

| Topic | Payload |
|---|---|
| `parking-guard/allowlist/count` | Integer — active bookings |
| `parking-guard/event` | Every LPR decision `{ plate, authorized, reason, camera, frigate_id, score, ts }` |
| `parking-guard/alert` | Unauthorized only — same JSON shape |
| `parking-guard/occupancy` | Retained bay-by-bay state `{ slots, occupied, free, violations, enforcing }` |
| `parking-guard/violation` | A new misparking incident `{ incident_id, plate, kind, slot, detail }` |
| `parking-guard/notification` | A message went out `{ incident_id, channel, kind }` |
| `parking-guard/incident/ack` | The client acknowledged `{ incident_id, channel }` |
| `parking-guard/incident/resolved` | The car is parked correctly now |
| `parking-guard/escalation` | Nobody responded, the owner was told |
| `parking-guard/enforce` | Retained `on` / `off` / `auto` |
| `parking-guard/command/enforce` | **Subscribed** — set match-day enforcement |
| `parking-guard/command/resolve` | **Subscribed** — close incidents for a tracked object id |

## Development

```bash
nix flake check
nix build .#packages.x86_64-linux.parking-guard
```

## JustPark API

Partner API access is provisioned by JustPark (sandbox → certification → production).
See [ParkHub docs](https://docs.parkhub.com/). The client calls `GET /sessions?siteId=…` and
normalizes common response shapes; adjust `sessions_path` in code if your contract differs.
