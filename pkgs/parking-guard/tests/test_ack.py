import json

import pytest

from parking_guard.ack import (
    NtfyAckListener,
    ReplyAckListener,
    TelegramAckListener,
    extract_token,
    looks_like_ack,
)
from parking_guard.config import AckConfig
from parking_guard.messaging.telegram import TelegramChannel
from parking_guard.store import STATUS_ACKED

from test_dispatcher import NOW, FakeChannel, _config, _dispatcher, _incident, store  # noqa: F401


@pytest.fixture
def dispatcher_with_notification(store):  # noqa: F811
    dispatcher = _dispatcher(store, {"whatsapp": FakeChannel("whatsapp"), "sms": FakeChannel("sms")})
    incident_id = _incident(store)
    dispatcher.notify(incident_id, now=NOW)
    token = store.notifications_for_incident(incident_id)[0]["token"]
    return dispatcher, incident_id, token


def test_looks_like_ack_reads_human_replies():
    assert looks_like_ack("OK")
    assert looks_like_ack("ok!")
    assert looks_like_ack("moving it now")
    assert looks_like_ack("Sorry, on my way")
    assert not looks_like_ack("")
    assert not looks_like_ack("who is this?")


def test_extract_token_from_bare_value_and_url():
    assert extract_token("abcdefgh1234") == "abcdefgh1234"
    assert extract_token("https://ntfy.sh/topic/trigger?message=abcdefgh1234") == "abcdefgh1234"
    assert extract_token("https://ntfy.sh/t/trigger?message=abcdefgh1234&priority=5") == "abcdefgh1234"
    assert extract_token("hi") == ""
    assert extract_token("") == ""


def test_ntfy_stream_line_acknowledges_the_incident(dispatcher_with_notification):
    dispatcher, incident_id, token = dispatcher_with_notification
    listener = NtfyAckListener(config=AckConfig(topic="acktopic"), dispatcher=dispatcher)

    line = json.dumps({"id": "x", "event": "message", "topic": "acktopic", "message": token})
    assert listener.handle_line(line) == incident_id
    assert dispatcher.store.get_incident(incident_id)["status"] == STATUS_ACKED

    # Replays and noise on the public topic are ignored.
    assert listener.handle_line(line) is None
    assert listener.handle_line(json.dumps({"event": "open", "topic": "acktopic"})) is None
    assert listener.handle_line("not json") is None


def test_telegram_button_callback_acknowledges(dispatcher_with_notification, monkeypatch):
    dispatcher, incident_id, token = dispatcher_with_notification
    channel = TelegramChannel(bot_token="bot", bot_username="parkbot")
    answered = []
    monkeypatch.setattr(channel, "answer_callback", lambda cid, text: answered.append(text))

    listener = TelegramAckListener(channel=channel, dispatcher=dispatcher)
    listener.handle_update({"callback_query": {"id": "cb1", "data": f"ack:{token}"}})

    assert dispatcher.store.get_incident(incident_id)["status"] == STATUS_ACKED
    assert answered and "Thanks" in answered[0]


def test_telegram_start_deep_link_links_the_chat(dispatcher_with_notification, monkeypatch):
    dispatcher, incident_id, token = dispatcher_with_notification
    channel = TelegramChannel(bot_token="bot", bot_username="parkbot")
    monkeypatch.setattr(channel, "send_plain", lambda chat_id, text: None)

    listener = TelegramAckListener(channel=channel, dispatcher=dispatcher)
    listener.handle_update(
        {"update_id": 5, "message": {"chat": {"id": 5551234}, "text": f"/start {token}"}}
    )

    assert dispatcher.store.get_contact("AB12 CDE")["telegram_chat_id"] == "5551234"
    assert dispatcher.store.get_incident(incident_id)["status"] == STATUS_ACKED


def test_sms_reply_is_matched_back_to_the_masked_number(store):  # noqa: F811
    sms = FakeChannel("sms")
    dispatcher = _dispatcher(store, {"sms": sms}, config=_config(ladder=("sms",)))
    incident_id = _incident(store)
    dispatcher.notify(incident_id, now=NOW)

    listener = ReplyAckListener(fetch=lambda: [], dispatcher=dispatcher, channel_name="sms")
    claimed = listener.handle_reply({"id": "m1", "from": "07700900123", "text": "ok moving it"})

    assert claimed == incident_id
    assert store.get_incident(incident_id)["status"] == STATUS_ACKED


def test_reply_listener_ignores_duplicates_and_unrelated_numbers(store):  # noqa: F811
    sms = FakeChannel("sms")
    dispatcher = _dispatcher(store, {"sms": sms}, config=_config(ladder=("sms",)))
    incident_id = _incident(store)
    dispatcher.notify(incident_id, now=NOW)

    listener = ReplyAckListener(fetch=lambda: [], dispatcher=dispatcher, channel_name="sms")
    assert listener.handle_reply({"id": "m1", "from": "07700900999", "text": "ok"}) is None
    assert listener.handle_reply({"id": "m2", "from": "07700900123", "text": "what?"}) is None

    assert listener.handle_reply({"id": "m3", "from": "07700900123", "text": "ok"}) == incident_id
    assert listener.handle_reply({"id": "m3", "from": "07700900123", "text": "ok"}) is None
