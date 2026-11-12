"""UK postcode normalisation shared by the map builder and the map API."""
import re

PC_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? \d[A-Z]{2}$")


def norm_pc(raw):
    """Normalise a postcode to 'AA9A 9AA' form, or None if it is not a plausible UK postcode.

    Strips stray punctuation and spacing (', DN22 7SW', 'S173GW'). Anything else, such as
    '00-227' or '(1062 EA)' (overseas entities), returns None.
    """
    s = re.sub(r"[^A-Z0-9]", "", (raw or "").upper())
    if not 5 <= len(s) <= 7:
        return None
    out = f"{s[:-3]} {s[-3:]}"
    return out if PC_RE.match(out) else None
