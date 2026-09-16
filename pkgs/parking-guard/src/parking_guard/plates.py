"""UK plate normalization — mirrors Frigate LPR replace_rules in lanbat frigate.nix."""

import re

UK_FORMAT = re.compile(r"^[A-Z]{2}[0-9]{2} ?[A-Z]{3}$")

REPLACE_RULES = (
    ("O", "0"),
    ("I", "1"),
)


def normalize_plate(plate: str) -> str:
    if not plate:
        return ""
    value = plate.upper().strip()
    for pattern, replacement in REPLACE_RULES:
        value = value.replace(pattern, replacement)
    return re.sub(r"\s+", "", value)


def plates_match(a: str, b: str) -> bool:
    return normalize_plate(a) == normalize_plate(b)
