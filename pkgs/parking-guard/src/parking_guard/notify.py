"""Wording for the messages parking-guard sends.

Client messages are written to be read by somebody who is late for a match and
half way down the street: say what is wrong, say what to do, and offer one tap to
confirm. Owner messages carry the plate and the review link instead.
"""

from __future__ import annotations

from typing import Any

from parking_guard.messaging.base import Message
from parking_guard.slots import (
    BLOCKING_AISLE,
    FORBIDDEN_AREA,
    NOT_PULLED_FORWARD,
    OUTSIDE_BAY,
    STRADDLING,
)

CLIENT_PROBLEM = {
    OUTSIDE_BAY: "your car isn't inside one of the marked bays",
    STRADDLING: "your car is sitting across two bays",
    NOT_PULLED_FORWARD: "there's still room in front of your car",
    BLOCKING_AISLE: "your car is blocking the way through",
    FORBIDDEN_AREA: "your car is on a neighbour's property rather than the booked space",
}

CLIENT_ACTION = {
    OUTSIDE_BAY: "Could you move it into a marked bay?",
    STRADDLING: "Could you shuffle it into a single bay so the next car can get in?",
    NOT_PULLED_FORWARD: "Could you pull right up towards the house so another car fits behind you?",
    BLOCKING_AISLE: "Could you move it into a marked bay so others can get past?",
    FORBIDDEN_AREA: "Could you move it onto the driveway you booked?",
}

OWNER_SUMMARY = {
    OUTSIDE_BAY: "parked outside the marked bays",
    STRADDLING: "straddling two bays",
    NOT_PULLED_FORWARD: "not pulled forward",
    BLOCKING_AISLE: "blocking the aisle",
    FORBIDDEN_AREA: "on a neighbour's property",
}

ACK_PROMPT = "Tap to confirm you're moving it"


def build_client_message(
    incident: dict[str, Any],
    *,
    site_name: str,
    ack_url: str = "",
    ack_token: str = "",
    telegram_link: str = "",
    greeting: str = "Hi",
) -> Message:
    kind = str(incident.get("kind", ""))
    problem = CLIENT_PROBLEM.get(kind, "your car is parked awkwardly")
    action = CLIENT_ACTION.get(kind, "Could you reposition it?")
    detail = str(incident.get("detail", "") or "")

    lines = [f"{greeting}, a quick one about your parking at {site_name}."]
    if kind == FORBIDDEN_AREA and detail:
        lines.append(f"Our camera shows {problem} — it looks like {detail}. {action}")
    elif kind == STRADDLING and detail:
        lines.append(f"Our camera shows {problem}, which also blocks {detail}. {action}")
    else:
        lines.append(f"Our camera shows {problem}. {action}")
    lines.append("The driveway takes four cars on match days, so a few feet really does make the difference. Thanks!")

    return Message(
        title="About your parking space",
        body="\n\n".join(lines),
        ack_url=ack_url,
        ack_token=ack_token,
        ack_prompt=ACK_PROMPT,
        incident_id=int(incident.get("id", 0) or 0),
        telegram_link=telegram_link,
    )


def build_owner_message(
    incident: dict[str, Any],
    *,
    nvr_domain: str = "",
    reason: str = "",
) -> Message:
    kind = str(incident.get("kind", ""))
    plate = str(incident.get("plate_display", "") or "unknown plate")
    slot = str(incident.get("slot", "") or "")
    detail = str(incident.get("detail", "") or "")

    lines = [f"{plate}: {OWNER_SUMMARY.get(kind, kind)}"]
    if slot:
        lines.append(f"Bay: {slot}")
    if detail:
        lines.append(f"Detail: {detail}")
    if reason:
        lines.append(reason)

    review_url = _review_url(str(incident.get("frigate_id", "") or ""), nvr_domain)
    if review_url:
        lines.append(f"Review: {review_url}")

    return Message(
        title="Parking needs a look",
        body="\n".join(lines),
        incident_id=int(incident.get("id", 0) or 0),
        review_url=review_url,
    )


def format_alert_message(event: dict[str, Any], nvr_domain: str) -> str:
    plate = event.get("plate", "?")
    camera = event.get("camera", "?")
    score = event.get("score")
    frigate_id = event.get("frigate_id")
    lines = [f"Plate: {plate}", f"Camera: {camera}"]
    if score is not None:
        lines.append(f"Score: {score}")
    review_url = _review_url(str(frigate_id or ""), nvr_domain)
    if review_url:
        lines.append(f"Review: {review_url}")
    return "\n".join(lines)


def _review_url(frigate_id: str, nvr_domain: str) -> str:
    if not frigate_id or not nvr_domain:
        return ""
    return f"https://{nvr_domain}/review?id={frigate_id}"
