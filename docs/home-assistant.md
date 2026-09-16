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

## Frigate companion

The Frigate custom component (already enabled in lanbat) provides review clips and
sensors. Link automations to `binary_sensor.frigate_*` if you prefer Frigate-native
events; parking-guard adds the **JustPark booking** layer on top.
