import pytest

from parking_guard.config import AckConfig, GuardConfig, LadderStep, NotifyConfig
from parking_guard.contacts import Contact
from parking_guard.dispatcher import Dispatcher
from parking_guard.messaging.base import AUDIENCE_BOTH, AUDIENCE_OWNER, Channel, ChannelError
from parking_guard.store import (
    STATUS_ACKED,
    STATUS_ESCALATED,
    STATUS_EXHAUSTED,
    STATUS_NOTIFIED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    Store,
)

NOW = 1_700_000_000.0


class FakeChannel(Channel):
    def __init__(self, name, audience=AUDIENCE_BOTH, fails=False):
        self.name = name
        self.audience = audience
        self.fails = fails
        self.sent = []

    def available_for(self, contact):
        # Owner channels carry their own destination, like the real ntfy/Pushover ones.
        if self.audience == AUDIENCE_OWNER:
            return True
        return bool(contact.endpoint(self.name))

    def send(self, contact, message):
        if self.fails:
            raise ChannelError(f"{self.name} is down")
        self.sent.append((contact, message))
        return f"{self.name}-1"


def _config(ladder=("whatsapp", "telegram", "email", "sms"), **notify_overrides):
    notify_kwargs = dict(
        client_ladder=tuple(LadderStep(channel=name, timeout_seconds=180.0) for name in ladder),
        owner_channels=("ntfy", "pushover", "telegram"),
        ack_grace_seconds=600.0,
        escalate_after_seconds=900.0,
        max_rounds=1,
        site_name="the driveway",
    )
    notify_kwargs.update(notify_overrides)
    return GuardConfig(
        state_dir="/tmp",
        db_path="/tmp/db",
        csv_import_path="/tmp/csv",
        mqtt_host="127.0.0.1",
        mqtt_port=1883,
        mqtt_user="parking-guard",
        mqtt_password_file="/tmp/pass",
        mqtt_topic_lpr="frigate/tracked_object_update",
        mqtt_topic_events="frigate/events",
        mqtt_topic_prefix="parking-guard",
        cameras=("c1",),
        min_score=0.8,
        grace_seconds=300.0,
        cooldown_seconds=1800.0,
        resident_plates=(),
        nvr_domain="nvr.example.com",
        notify=NotifyConfig(**notify_kwargs),
        ack=AckConfig(topic="acktopic"),
    )


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "test.db")
    store.upsert_contact(
        plate="AB12 CDE",
        booking_ref="b1",
        name="Sam Driver",
        phone="+447700900123",
        whatsapp="+447700900123",
        email="sam@example.com",
        source="justpark",
        expires_ts=NOW + 86400,
        now=NOW,
    )
    yield store
    store.close()


def _dispatcher(store, channels, config=None, published=None):
    return Dispatcher(
        store=store,
        channels=channels,
        config=config or _config(),
        owner=Contact(telegram_chat_id="999", source="owner"),
        publish=(lambda suffix, payload: published.append((suffix, payload))) if published is not None else None,
        clock=lambda: NOW,
    )


def _incident(store, kind="not_pulled_forward"):
    incident_id, _ = store.open_incident(
        plate="AB12 CDE", object_id="obj1", kind=kind, slot="front_left", booking_ref="b1", now=NOW
    )
    return incident_id


def test_first_rung_wins_and_sms_is_never_touched(store):
    channels = {
        "whatsapp": FakeChannel("whatsapp"),
        "telegram": FakeChannel("telegram"),
        "sms": FakeChannel("sms"),
    }
    dispatcher = _dispatcher(store, channels)
    incident_id = _incident(store)

    assert dispatcher.notify(incident_id, now=NOW)
    assert len(channels["whatsapp"].sent) == 1
    assert channels["sms"].sent == []

    incident = store.get_incident(incident_id)
    assert incident["status"] == STATUS_NOTIFIED
    assert incident["ladder_index"] == 1
    assert incident["next_action_ts"] == NOW + 180.0


def test_rung_without_an_endpoint_is_skipped_without_waiting(store):
    # No telegram chat id on file, so telegram is unavailable and email is used.
    channels = {
        "whatsapp": FakeChannel("whatsapp", fails=True),
        "telegram": FakeChannel("telegram"),
        "email": FakeChannel("email"),
        "sms": FakeChannel("sms"),
    }
    dispatcher = _dispatcher(store, channels)
    incident_id = _incident(store)

    assert dispatcher.notify(incident_id, now=NOW)
    assert channels["telegram"].sent == []
    assert len(channels["email"].sent) == 1
    assert channels["sms"].sent == []
    assert store.get_incident(incident_id)["ladder_index"] == 3


def test_sms_is_reached_only_after_everything_else(store):
    channels = {
        "whatsapp": FakeChannel("whatsapp", fails=True),
        "email": FakeChannel("email", fails=True),
        "sms": FakeChannel("sms"),
    }
    dispatcher = _dispatcher(store, channels)
    incident_id = _incident(store)

    assert dispatcher.notify(incident_id, now=NOW)
    assert len(channels["sms"].sent) == 1
    failures = [n for n in store.notifications_for_incident(incident_id) if n["status"] == "failed"]
    assert {n["channel"] for n in failures} == {"whatsapp", "email"}


def test_timeout_advances_to_the_next_rung_on_tick(store):
    channels = {"whatsapp": FakeChannel("whatsapp"), "email": FakeChannel("email")}
    dispatcher = _dispatcher(store, channels)
    incident_id = _incident(store)

    dispatcher.notify(incident_id, now=NOW)
    dispatcher.tick(now=NOW + 60)
    assert len(channels["email"].sent) == 0

    dispatcher.tick(now=NOW + 200)
    assert len(channels["email"].sent) == 1


def test_owner_is_told_when_the_client_has_no_contact_details(store, tmp_path):
    empty = Store(tmp_path / "empty.db")
    owner_channel = FakeChannel("ntfy", audience=AUDIENCE_OWNER)
    dispatcher = _dispatcher(empty, {"ntfy": owner_channel, "whatsapp": FakeChannel("whatsapp")})
    incident_id = _incident(empty)

    assert not dispatcher.notify(incident_id, now=NOW)
    assert len(owner_channel.sent) == 1
    assert "no contact details" in owner_channel.sent[0][1].body
    assert empty.get_incident(incident_id)["status"] == STATUS_ESCALATED
    empty.close()


def test_exhausted_ladder_escalates_then_stops(store):
    published = []
    channels = {"whatsapp": FakeChannel("whatsapp"), "ntfy": FakeChannel("ntfy", audience=AUDIENCE_OWNER)}
    dispatcher = _dispatcher(store, channels, published=published)
    incident_id = _incident(store)

    dispatcher.notify(incident_id, now=NOW)
    dispatcher.tick(now=NOW + 200)

    incident = store.get_incident(incident_id)
    assert incident["status"] == STATUS_ESCALATED
    assert len(channels["ntfy"].sent) == 1
    assert any(suffix == "escalation" for suffix, _ in published)

    dispatcher.tick(now=NOW + 200 + 901)
    assert store.get_incident(incident_id)["status"] == STATUS_EXHAUSTED


def test_second_round_gives_the_client_another_chance(store):
    config = _config(max_rounds=2)
    channels = {"whatsapp": FakeChannel("whatsapp"), "ntfy": FakeChannel("ntfy", audience=AUDIENCE_OWNER)}
    dispatcher = _dispatcher(store, channels, config=config)
    incident_id = _incident(store)

    dispatcher.notify(incident_id, now=NOW)
    dispatcher.tick(now=NOW + 200)

    incident = store.get_incident(incident_id)
    assert incident["status"] == STATUS_OPEN
    assert incident["ladder_index"] == 0

    dispatcher.tick(now=NOW + 200 + 901)
    assert len(channels["whatsapp"].sent) == 2


def test_ack_pauses_the_ladder_and_is_single_use(store):
    published = []
    channels = {"whatsapp": FakeChannel("whatsapp")}
    dispatcher = _dispatcher(store, channels, published=published)
    incident_id = _incident(store)
    dispatcher.notify(incident_id, now=NOW)

    token = store.notifications_for_incident(incident_id)[0]["token"]
    assert dispatcher.handle_ack(token, "whatsapp", now=NOW + 30) == incident_id

    incident = store.get_incident(incident_id)
    assert incident["status"] == STATUS_ACKED
    assert incident["next_action_ts"] == NOW + 30 + 600.0
    assert any(suffix == "incident/ack" for suffix, _ in published)

    assert dispatcher.handle_ack(token, "whatsapp", now=NOW + 40) is None
    assert dispatcher.handle_ack("not-a-token", "ntfy", now=NOW + 40) is None


def test_ack_without_moving_the_car_escalates_after_the_grace(store):
    channels = {"whatsapp": FakeChannel("whatsapp"), "ntfy": FakeChannel("ntfy", audience=AUDIENCE_OWNER)}
    dispatcher = _dispatcher(store, channels)
    incident_id = _incident(store)
    dispatcher.notify(incident_id, now=NOW)
    token = store.notifications_for_incident(incident_id)[0]["token"]
    dispatcher.handle_ack(token, "whatsapp", now=NOW + 30)

    dispatcher.tick(now=NOW + 30 + 601)
    assert "has not moved" in channels["ntfy"].sent[0][1].body


def test_resolving_the_object_closes_the_incident(store):
    published = []
    dispatcher = _dispatcher(store, {"whatsapp": FakeChannel("whatsapp")}, published=published)
    incident_id = _incident(store)
    dispatcher.notify(incident_id, now=NOW)

    assert dispatcher.resolve_object("obj1", now=NOW + 120) == [incident_id]
    assert store.get_incident(incident_id)["status"] == STATUS_RESOLVED
    assert any(suffix == "incident/resolved" for suffix, _ in published)

    dispatcher.tick(now=NOW + 900)
    assert store.get_incident(incident_id)["status"] == STATUS_RESOLVED


def test_telegram_invite_is_offered_only_when_no_chat_is_linked(store):
    from parking_guard.messaging.telegram import TelegramChannel

    telegram = TelegramChannel(bot_token="bot", bot_username="parkbot")
    dispatcher = _dispatcher(store, {"whatsapp": FakeChannel("whatsapp"), "telegram": telegram})
    incident_id = _incident(store)
    dispatcher.notify(incident_id, now=NOW)

    _, message = dispatcher.channels["whatsapp"].sent[0]
    assert message.telegram_link.startswith("https://t.me/parkbot?start=")

    store.set_telegram_chat_id("AB12 CDE", "5551234", now=NOW)
    store.update_incident(incident_id, ladder_index=0, status=STATUS_OPEN, now=NOW)
    dispatcher.notify(incident_id, now=NOW + 10)
    _, second = dispatcher.channels["whatsapp"].sent[1]
    assert second.telegram_link == ""


def test_a_second_notify_while_the_rung_is_live_sends_nothing(store):
    # The MQTT thread raises the incident and notifies; the dispatcher thread's tick
    # can reach the same incident before the first send has been recorded.
    channels = {"whatsapp": FakeChannel("whatsapp"), "email": FakeChannel("email")}
    dispatcher = _dispatcher(store, channels)
    incident_id = _incident(store)

    assert dispatcher.notify(incident_id, now=NOW)
    assert not dispatcher.notify(incident_id, now=NOW)
    dispatcher.tick(now=NOW + 1)

    assert len(channels["whatsapp"].sent) == 1
    assert channels["email"].sent == []


def test_a_failed_send_leaves_no_usable_token(store):
    channels = {"whatsapp": FakeChannel("whatsapp", fails=True), "email": FakeChannel("email")}
    dispatcher = _dispatcher(store, channels)
    incident_id = _incident(store)
    dispatcher.notify(incident_id, now=NOW)

    failed = [n for n in store.notifications_for_incident(incident_id) if n["status"] == "failed"]
    assert failed and failed[0]["token"] is None
    assert len(channels["email"].sent) == 1


def test_a_late_ack_does_not_restart_a_finished_ladder(store):
    channels = {"whatsapp": FakeChannel("whatsapp"), "ntfy": FakeChannel("ntfy", audience=AUDIENCE_OWNER)}
    dispatcher = _dispatcher(store, channels)
    incident_id = _incident(store)
    dispatcher.notify(incident_id, now=NOW)
    token = store.notifications_for_incident(incident_id)[0]["token"]

    dispatcher.tick(now=NOW + 200)
    assert store.get_incident(incident_id)["status"] == STATUS_ESCALATED

    assert dispatcher.handle_ack(token, "whatsapp", now=NOW + 300) == incident_id
    incident = store.get_incident(incident_id)
    assert incident["status"] == STATUS_ESCALATED
    assert incident["acked_ts"] == NOW + 300

    dispatcher.tick(now=NOW + 1200)
    assert store.get_incident(incident_id)["status"] == STATUS_EXHAUSTED
    assert len(channels["whatsapp"].sent) == 1


def test_notification_targets_are_masked_on_disk(store):
    dispatcher = _dispatcher(store, {"whatsapp": FakeChannel("whatsapp")})
    incident_id = _incident(store)
    dispatcher.notify(incident_id, now=NOW)

    target = store.notifications_for_incident(incident_id)[0]["target"]
    assert target == "***0123"
