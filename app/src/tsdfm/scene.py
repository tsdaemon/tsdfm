"""Social scene choices only; never controls or observes audio playback."""

MOVES = ("headbang", "jumping", "hands", "seated")
DRINKS = ("", "beer", "ipa", "stout", "whisky", "rum", "gin", "vodka", "tequila", "coffee", "water")
BAR_SEATS = 3


def choice(value=None):
    value = value if isinstance(value, dict) else {}
    return {
        "move": value.get("move") if value.get("move") in MOVES else "hands",
        "drink": value.get("drink") if value.get("drink") in DRINKS else "",
    }


def valid_bpm(value):
    # Metadata is optional. Invalid tags must never affect playback or animation.
    if isinstance(value, bool):
        return None
    try:
        bpm = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return bpm if 20 <= bpm <= 300 else None
