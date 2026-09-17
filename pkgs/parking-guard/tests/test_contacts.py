import json

from parking_guard.contacts import (
    Contact,
    contact_from_booking,
    load_overrides,
    normalize_phone,
    resolve,
    store_booking_contacts,
)
from parking_guard.justpark.client import sessions_to_bookings
from parking_guard.store import Booking, Store


def _booking(**overrides):
    values = dict(
        plate="AB12 CDE",
        plate_normalized="AB12CDE",
        start_ts=1_700_000_000.0,
        end_ts=1_700_003_600.0,
        booking_ref="b1",
        source="justpark-api",
        name="Sam Driver",
        phone="07700 900123",
        email="sam@example.com",
    )
    values.update(overrides)
    return Booking(**values)


def test_normalize_phone_handles_uk_formats():
    assert normalize_phone("07700 900123") == "+447700900123"
    assert normalize_phone("+44 7700 900123") == "+447700900123"
    assert normalize_phone("00447700900123") == "+447700900123"
    assert normalize_phone("") == ""


def test_contact_from_booking_defaults_whatsapp_to_the_phone_number():
    contact = contact_from_booking(_booking())
    assert contact.whatsapp == "+447700900123"
    assert contact.reachable_on("whatsapp")
    assert contact.reachable_on("sms")
    assert not contact.reachable_on("telegram")
    assert contact.greeting == "Sam"


def test_contact_without_endpoints_is_not_reachable():
    assert not Contact(plate="AB12 CDE", name="Sam").reachable


def test_sessions_to_bookings_extracts_nested_contact_details():
    sessions = [
        {
            "id": "sess-1",
            "vehicle": {"licensePlate": "AB12 CDE"},
            "actualStart": "2026-09-17T10:00:00Z",
            "actualEnd": "2026-09-17T14:00:00Z",
            "customer": {"name": "Sam Driver", "phone": "07700 900123"},
        }
    ]
    booking = sessions_to_bookings(sessions)[0]
    assert booking.plate == "AB12 CDE"
    assert booking.name == "Sam Driver"
    assert booking.phone == "07700 900123"
    assert booking.email == ""


def test_sessions_to_bookings_reads_apds_credential_shape():
    sessions = [
        {
            "id": "sess-2",
            "identifiedCredentials": [{"type": "licensePlate", "identifier": {"id": "XY34ZZZ"}}],
        }
    ]
    assert sessions_to_bookings(sessions)[0].plate == "XY34ZZZ"


def test_sessions_without_a_plate_are_skipped():
    assert sessions_to_bookings([{"id": "sess-3", "customer": {"phone": "07700 900123"}}]) == []


def test_load_overrides_accepts_list_and_mapping_forms(tmp_path):
    as_list = tmp_path / "contacts.json"
    as_list.write_text(
        json.dumps(
            {
                "contacts": [
                    {"plate": "AB12 CDE", "name": "Sam", "phone": "07700 900123", "telegram_chat_id": "42"}
                ]
            }
        ),
        encoding="utf-8",
    )
    overrides = load_overrides(as_list)
    assert overrides["AB12CDE"].telegram_chat_id == "42"
    assert overrides["AB12CDE"].whatsapp == "+447700900123"

    as_mapping = tmp_path / "mapping.json"
    as_mapping.write_text(json.dumps({"XY34 ZZZ": {"email": "x@example.com"}}), encoding="utf-8")
    assert load_overrides(as_mapping)["XY34ZZZ"].email == "x@example.com"


def test_load_overrides_survives_a_missing_or_broken_file(tmp_path):
    assert load_overrides(tmp_path / "nope.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_overrides(broken) == {}


def test_resolve_prefers_override_fields_but_keeps_booking_values(tmp_path):
    store = Store(tmp_path / "test.db")
    now = 1_700_000_000.0
    store_booking_contacts(store, [_booking()], retention_days=7, now=now)

    overrides = {"AB12CDE": Contact(plate="AB12 CDE", telegram_chat_id="42", source="override")}
    contact = resolve(store, "AB12CDE", "b1", overrides)

    assert contact.name == "Sam Driver"
    assert contact.phone == "+447700900123"
    assert contact.telegram_chat_id == "42"
    assert contact.endpoint("email") == "sam@example.com"
    store.close()


def test_resolve_returns_none_for_an_unknown_plate(tmp_path):
    store = Store(tmp_path / "test.db")
    assert resolve(store, "ZZ99 ZZZ") is None
    store.close()


def test_store_booking_contacts_skips_bookings_without_details(tmp_path):
    store = Store(tmp_path / "test.db")
    bookings = [_booking(), _booking(plate="XY34 ZZZ", booking_ref="b2", name="", phone="", email="")]
    assert store_booking_contacts(store, bookings, retention_days=7, now=1_700_000_000.0) == 1
    store.close()
