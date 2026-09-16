import time

from parking_guard.store import Booking, Store


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
