# Home Assistant integration

Parking guard publishes MQTT messages that Home Assistant can consume. The server
already runs Mosquitto and Home Assistant with the MQTT integration.

## MQTT sensors

Add to **Settings → Devices & services → MQTT → Configure → Publish**:

Or paste into `configuration.yaml` (if not using UI-only config):

```yaml
mqtt:
  sensor:
    - name: "Parking allowlist count"
      unique_id: parking_guard_allowlist_count
      state_topic: "parking-guard/allowlist/count"
      icon: mdi:car-multiple

  binary_sensor:
    - name: "Unauthorized parking"
      unique_id: parking_guard_unauthorized
      state_topic: "parking-guard/alert"
      value_template: "{{ 'ON' if value_json.plate else 'OFF' }}"
      json_attributes_topic: "parking-guard/alert"
      json_attributes_template: "{{ value_json | tojson }}"
      device_class: problem
```

## Automation — mobile push + persistent notification

```yaml
automation:
  - id: parking_guard_unauthorized_alert
    alias: "Parking — unauthorized vehicle"
    mode: single
    trigger:
      - platform: mqtt
        topic: parking-guard/alert
    variables:
      plate: "{{ trigger.payload_json.plate }}"
      camera: "{{ trigger.payload_json.camera | default('unknown') }}"
      score: "{{ trigger.payload_json.score | default('n/a') }}"
      frigate_id: "{{ trigger.payload_json.frigate_id | default('') }}"
      review_url: >-
        {% if frigate_id %}
        https://nvr.{{ states('sensor.home_domain') | default('home.example.com') }}/review?id={{ frigate_id }}
        {% else %}
        https://nvr.{{ states('sensor.home_domain') | default('home.example.com') }}
        {% endif %}
    action:
      - service: persistent_notification.create
        data:
          title: "Unauthorized parking"
          message: >-
            Plate {{ plate }} on camera {{ camera }} (score {{ score }}).
            {% if frigate_id %} Open Frigate review for clip.{% endif %}
          notification_id: "parking_guard_{{ plate | replace(' ', '_') }}"
      - service: notify.mobile_app
        data:
          title: "Unauthorized parking"
          message: "{{ plate }} detected on {{ camera }}"
          url: "{{ review_url }}"
```

Replace `sensor.home_domain` with your domain entity or hard-code `nvr.<your-domain>`.

## Optional — log authorized events

```yaml
automation:
  - id: parking_guard_authorized_log
    alias: "Parking — authorized vehicle (logbook)"
    trigger:
      - platform: mqtt
        topic: parking-guard/event
    condition:
      - condition: template
        value_template: "{{ trigger.payload_json.authorized }}"
    action:
      - service: logbook.log
        data:
          name: Parking guard
          message: >-
            Authorized {{ trigger.payload_json.plate }}
            ({{ trigger.payload_json.reason }})
```

## Misparking — bay occupancy and incidents

```yaml
mqtt:
  sensor:
    - name: "Parking bays free"
      unique_id: parking_guard_bays_free
      state_topic: "parking-guard/occupancy"
      value_template: "{{ value_json.free }}"
      json_attributes_topic: "parking-guard/occupancy"
      icon: mdi:parking

  binary_sensor:
    - name: "Parking misparked vehicle"
      unique_id: parking_guard_misparked
      state_topic: "parking-guard/violation"
      value_template: "{{ 'ON' if value_json.incident_id else 'OFF' }}"
      json_attributes_topic: "parking-guard/violation"
      device_class: problem
```

The retained `parking-guard/occupancy` payload carries a `slots` map, so a per-bay
template sensor is a one-liner:

```yaml
template:
  - sensor:
      - name: "Front left bay"
        state: >-
          {% set slot = state_attr('sensor.parking_bays_free', 'slots')['front_left'] %}
          {{ 'free' if not slot.occupied else ('ok' if slot.compliant else 'misparked') }}
```

## Match-day enforcement switch

Nobody is messaged unless enough bookings are live, or you force enforcement on:

```yaml
mqtt:
  switch:
    - name: "Parking enforcement"
      unique_id: parking_guard_enforce
      command_topic: "parking-guard/command/enforce"
      state_topic: "parking-guard/enforce"
      payload_on: "on"
      payload_off: "off"
      state_on: "on"
      state_off: "off"
      icon: mdi:shield-car
```

Publish `auto` to that command topic to hand control back to the booking count. A match-day
calendar automation is the natural fit:

```yaml
automation:
  - id: parking_guard_matchday
    alias: "Parking — enforcement on for match days"
    trigger:
      - platform: calendar
        event: start
        entity_id: calendar.home_matches
    action:
      - service: mqtt.publish
        data:
          topic: parking-guard/command/enforce
          payload: "on"
```

## Following an incident

```yaml
automation:
  - id: parking_guard_incident_log
    alias: "Parking — incident lifecycle"
    mode: parallel
    trigger:
      - platform: mqtt
        topic: parking-guard/notification
        id: notified
      - platform: mqtt
        topic: parking-guard/incident/ack
        id: acked
      - platform: mqtt
        topic: parking-guard/escalation
        id: escalated
    action:
      - choose:
          - conditions: "{{ trigger.id == 'notified' }}"
            sequence:
              - service: logbook.log
                data:
                  name: Parking guard
                  message: >-
                    Messaged the client over {{ trigger.payload_json.channel }}
                    about {{ trigger.payload_json.kind }}
          - conditions: "{{ trigger.id == 'acked' }}"
            sequence:
              - service: logbook.log
                data:
                  name: Parking guard
                  message: "Client acknowledged via {{ trigger.payload_json.channel }}"
          - conditions: "{{ trigger.id == 'escalated' }}"
            sequence:
              - service: notify.mobile_app
                data:
                  title: "Parking needs you"
                  message: >-
                    {{ trigger.payload_json.plate }} — {{ trigger.payload_json.reason }}
```

`parking-guard/escalation` is the one worth a push notification: it only fires when the
client could not be reached or did not move the car.

## Frigate companion

The Frigate custom component (already enabled in lanbat) provides review clips and
sensors. Link automations to `binary_sensor.frigate_*` if you prefer Frigate-native
events; parking-guard adds the **JustPark booking** layer on top.
