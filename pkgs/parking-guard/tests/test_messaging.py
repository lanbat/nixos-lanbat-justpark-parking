import json

import pytest
import requests

from parking_guard.contacts import Contact
from parking_guard.messaging import build_channels, owner_contact
from parking_guard.messaging.base import AUDIENCE_CLIENT, AUDIENCE_OWNER, ChannelError, Message
from parking_guard.messaging.ntfy import NtfyChannel
from parking_guard.messaging.sms_android import AndroidSmsChannel
from parking_guard.messaging.sms_cloud import SmsCloudChannel
from parking_guard.messaging.telegram import TelegramChannel
from parking_guard.messaging.whatsapp_cloud import WhatsAppCloudChannel
from parking_guard.notify import build_client_message, build_owner_message
from parking_guard.slots import FORBIDDEN_AREA, NOT_PULLED_FORWARD, STRADDLING

CONTACT = Contact(
    plate="AB12 CDE",
    name="Sam Driver",
    phone="+447700900123",
    whatsapp="+447700900123",
    email="sam@example.com",
    telegram_chat_id="5551234",
)

MESSAGE = Message(
    title="About your parking space",
    body="Please pull forward.",
    ack_url="https://ntfy.sh/acktopic/trigger?message=tok123",
    ack_token="tok123",
)


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse({"id": "provider-1"})

    monkeypatch.setattr(requests, "request", fake_request)
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: fake_request("POST", url, **kwargs))
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: fake_request("GET", url, **kwargs))
    return calls


def test_message_text_includes_ack_and_telegram_links():
    message = Message(
        title="t",
        body="Please pull forward.",
        ack_url="https://ntfy.sh/a/trigger?message=tok",
        telegram_link="https://t.me/bot?start=tok",
    )
    text = message.text()
    assert "Please pull forward." in text
    assert "https://t.me/bot?start=tok" in text
    assert "https://ntfy.sh/a/trigger?message=tok" in text


def test_whatsapp_template_payload_carries_the_ack_token(captured):
    channel = WhatsAppCloudChannel(token="t", phone_number_id="123", template="misparked")
    channel.send(CONTACT, MESSAGE)

    payload = captured[0]["json"]
    assert payload["to"] == "447700900123"
    assert payload["template"]["name"] == "misparked"
    button = payload["template"]["components"][1]
    assert button["sub_type"] == "url"
    assert button["parameters"][0]["text"] == "tok123"


def test_whatsapp_falls_back_to_free_text_without_a_template(captured):
    WhatsAppCloudChannel(token="t", phone_number_id="123").send(CONTACT, MESSAGE)
    assert captured[0]["json"]["type"] == "text"


def test_whatsapp_send_without_a_number_is_a_channel_error():
    channel = WhatsAppCloudChannel(token="t", phone_number_id="123")
    with pytest.raises(ChannelError):
        channel.send(Contact(plate="AB12 CDE"), MESSAGE)


def test_http_failure_becomes_a_channel_error(monkeypatch):
    def boom(url, **kwargs):
        raise requests.ConnectTimeout("nope")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(ChannelError):
        WhatsAppCloudChannel(token="t", phone_number_id="123").send(CONTACT, MESSAGE)


def test_telegram_attaches_an_inline_ack_button(captured):
    channel = TelegramChannel(bot_token="bot", bot_username="parkbot")
    channel.send(CONTACT, MESSAGE)

    payload = captured[0]["json"]
    button = payload["reply_markup"]["inline_keyboard"][0][0]
    assert button["callback_data"] == "ack:tok123"
    assert channel.deep_link("tok123") == "https://t.me/parkbot?start=tok123"


def test_sms_cloud_renders_the_configured_body_template(captured):
    channel = SmsCloudChannel(
        url="https://api.example.com/send",
        body_template=json.dumps({"dest": "{to}", "msg": "{text}", "src": "{from}"}),
        sender="Driveway",
        auth_header="Authorization",
        auth_value="Bearer abc",
    )
    delivery_id = channel.send(CONTACT, MESSAGE)

    call = captured[0]
    assert call["json"]["dest"] == "+447700900123"
    assert "Please pull forward." in call["json"]["msg"]
    assert call["json"]["src"] == "Driveway"
    assert call["headers"]["Authorization"] == "Bearer abc"
    assert delivery_id == "provider-1"


def test_sms_cloud_rejects_a_broken_template():
    channel = SmsCloudChannel(url="https://api.example.com/send", body_template="{not json")
    with pytest.raises(ChannelError):
        channel.send(CONTACT, MESSAGE)


def test_android_gateway_posts_to_the_lan_handset(captured):
    AndroidSmsChannel(base_url="http://192.168.1.50:8080", username="u", password="p").send(CONTACT, MESSAGE)
    call = captured[0]
    assert call["url"] == "http://192.168.1.50:8080/message"
    assert call["json"]["phoneNumbers"] == ["+447700900123"]
    assert call["auth"] == ("u", "p")


def test_ntfy_is_owner_only_and_needs_no_contact():
    channel = NtfyChannel(topic="owner-topic")
    assert channel.audience == AUDIENCE_OWNER
    assert not channel.serves(AUDIENCE_CLIENT)
    assert channel.available_for(Contact())


def test_build_channels_reads_credentials_and_prefers_waha():
    channels = build_channels(
        {
            "WHATSAPP_TOKEN": "t",
            "WHATSAPP_PHONE_NUMBER_ID": "123",
            "TELEGRAM_BOT_TOKEN": "bot",
            "SMTP_HOST": "smtp.example.com",
            "SMS_API_URL": "https://api.example.com/send",
            "NTFY_TOPIC": "owner-topic",
            "PUSHOVER_TOKEN": "pt",
            "PUSHOVER_USER": "pu",
        }
    )
    assert set(channels) == {"whatsapp", "telegram", "email", "sms", "ntfy", "pushover"}
    assert channels["whatsapp"].__class__.__name__ == "WhatsAppCloudChannel"

    with_waha = build_channels({"WHATSAPP_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1", "WAHA_URL": "http://waha"})
    assert with_waha["whatsapp"].__class__.__name__ == "WahaChannel"

    android = build_channels({"SMS_API_URL": "https://api.example.com", "ANDROID_SMS_URL": "http://phone"})
    assert android["sms"].__class__.__name__ == "AndroidSmsChannel"


def test_build_channels_with_no_credentials_yields_nothing():
    assert build_channels({}) == {}


def test_owner_contact_uses_owner_endpoints():
    contact = owner_contact({"OWNER_PHONE": "07700 900999", "TELEGRAM_CHAT_ID": "42"})
    assert contact.phone == "+447700900999"
    assert contact.endpoint("telegram") == "42"


def test_client_message_is_polite_and_specific():
    incident = {"id": 1, "kind": NOT_PULLED_FORWARD, "slot": "front_left", "detail": "Front left is empty"}
    message = build_client_message(
        incident,
        site_name="21 Example Road",
        ack_url="https://ntfy.sh/a/trigger?message=tok",
        ack_token="tok",
        greeting="Sam",
    )
    assert message.body.startswith("Sam,")
    assert "21 Example Road" in message.body
    assert "pull right up towards the house" in message.body
    assert message.ack_token == "tok"


def test_client_message_names_the_blocked_bay_and_the_neighbour():
    straddling = build_client_message(
        {"id": 1, "kind": STRADDLING, "detail": "Front right"}, site_name="the driveway"
    )
    assert "blocks Front right" in straddling.body

    forbidden = build_client_message(
        {"id": 2, "kind": FORBIDDEN_AREA, "detail": "the neighbour's drive"}, site_name="the driveway"
    )
    assert "the neighbour's drive" in forbidden.body


def test_owner_message_carries_plate_and_review_link():
    message = build_owner_message(
        {"id": 3, "kind": STRADDLING, "plate_display": "AB12 CDE", "slot": "front_left", "frigate_id": "abc"},
        nvr_domain="nvr.example.com",
        reason="no contact details on file",
    )
    assert "AB12 CDE" in message.body
    assert "no contact details on file" in message.body
    assert message.review_url == "https://nvr.example.com/review?id=abc"
