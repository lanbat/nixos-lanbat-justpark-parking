import time

from parking_guard.store import SCHEMA_VERSION, STATUS_RESOLVED, Booking, Store


def test_allowlist_authorization_with_grace(tmp_path):
    db = tmp_path / "test.db"
    store = Store(db)
    now = time.time()
    store.replace_allowlist(
        [
            Booking(
                plate="AB12 CDE",
                plate_normalized="AB12CDE",
                start_ts=now - 60,
                end_ts=now + 3600,
                booking_ref="b1",
                source="test",
            )
        ]
    )

    ok, reason = store.is_authorized("AB12CDE", now, grace_seconds=300)
    assert ok
    assert reason.startswith("booking:")

    ok, reason = store.is_authorized("ZZ99ZZZ", now, grace_seconds=0)
    assert not ok
    assert reason == "no_active_booking"

    store.close()


def test_cooldown(tmp_path):
    db = tmp_path / "test.db"
    store = Store(db)
    now = 1_700_000_000.0
    assert store.should_alert("AB12CDE", cooldown_seconds=1800, now=now)
    store.mark_alerted("AB12CDE", now=now)
    assert not store.should_alert("AB12CDE", cooldown_seconds=1800, now=now + 60)
    assert store.should_alert("AB12CDE", cooldown_seconds=1800, now=now + 2000)
    store.close()


def test_schema_version_is_stamped(tmp_path):
    store = Store(tmp_path / "test.db")
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
    store.close()


def test_contacts_merge_and_expire(tmp_path):
    store = Store(tmp_path / "test.db")
    now = 1_700_000_000.0

    store.upsert_contact(
        plate="AB12 CDE",
        booking_ref="b1",
        name="Sam",
        phone="+447700900123",
        source="justpark",
        expires_ts=now + 3600,
        now=now,
    )
    # A later sync without an email must not wipe one we already have.
    store.upsert_contact(
        plate="AB12 CDE",
        booking_ref="b1",
        email="sam@example.com",
        source="contacts.yaml",
        expires_ts=now + 3600,
        now=now + 10,
    )

    contact = store.get_contact("AB12CDE", "b1")
    assert contact["name"] == "Sam"
    assert contact["phone"] == "+447700900123"
    assert contact["email"] == "sam@example.com"

    store.set_telegram_chat_id("AB12CDE", "55512345", now=now + 20)
    assert store.get_contact("AB12CDE")["telegram_chat_id"] == "55512345"

    assert store.purge_expired_contacts(now=now + 7200) == 1
    assert store.get_contact("AB12CDE") is None
    store.close()


def test_incident_is_reused_until_resolved(tmp_path):
    store = Store(tmp_path / "test.db")
    now = 1_700_000_000.0

    incident_id, created = store.open_incident(
        plate="AB12 CDE", object_id="obj1", kind="not_pulled_forward", slot="front_left", now=now
    )
    assert created

    again, created_again = store.open_incident(
        plate="AB12 CDE", object_id="obj1", kind="not_pulled_forward", now=now + 30
    )
    assert again == incident_id
    assert not created_again

    assert store.resolve_incidents_for_object("obj1", now=now + 60) == [incident_id]
    assert store.get_incident(incident_id)["status"] == STATUS_RESOLVED

    reopened, created_after_resolve = store.open_incident(
        plate="AB12 CDE", object_id="obj1", kind="not_pulled_forward", now=now + 90
    )
    assert created_after_resolve
    assert reopened != incident_id
    store.close()


def test_incidents_due_respects_next_action_and_status(tmp_path):
    store = Store(tmp_path / "test.db")
    now = 1_700_000_000.0
    incident_id, _ = store.open_incident(plate="AB12 CDE", object_id="obj1", kind="outside_bay", now=now)

    assert [i["id"] for i in store.incidents_due(now=now)] == [incident_id]

    store.update_incident(incident_id, next_action_ts=now + 300, now=now)
    assert store.incidents_due(now=now + 10) == []
    assert [i["id"] for i in store.incidents_due(now=now + 301)] == [incident_id]

    store.update_incident(incident_id, status=STATUS_RESOLVED, now=now)
    assert store.incidents_due(now=now + 301) == []
    store.close()


def test_ack_token_can_only_be_claimed_once(tmp_path):
    store = Store(tmp_path / "test.db")
    now = 1_700_000_000.0
    incident_id, _ = store.open_incident(plate="AB12 CDE", object_id="obj1", kind="straddling", now=now)
    store.record_notification(
        incident_id=incident_id,
        channel="whatsapp",
        audience="client",
        target="+447700900123",
        token="tok123",
        now=now,
    )

    claimed = store.consume_token("tok123")
    assert claimed["incident_id"] == incident_id
    assert store.consume_token("tok123") is None
    assert store.consume_token("never-issued") is None

    store.record_ack(incident_id=incident_id, channel="whatsapp", token="tok123", now=now + 5)
    assert len(store.notifications_for_incident(incident_id)) == 1
    store.close()


def test_slot_state_round_trip(tmp_path):
    store = Store(tmp_path / "test.db")
    store.set_slot_state(slot="front_left", plate="AB12 CDE", object_id="obj1", compliant=False, now=1.0)
    store.set_slot_state(slot="front_left", plate="AB12 CDE", object_id="obj1", compliant=True, now=2.0)
    states = store.slot_states()
    assert len(states) == 1
    assert states[0]["compliant"] == 1
    store.close()
