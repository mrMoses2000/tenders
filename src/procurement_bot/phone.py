from __future__ import annotations

import re


def normalize_phone(value: str, *, default_country_code: str = "7") -> str:
    """Canonicalise a phone to E.164-like form for the Kazakhstan deployment."""

    raw = value.strip().removesuffix("@c.us")
    if not re.fullmatch(r"[+\d\s()\-]+", raw):
        raise ValueError("phone must be an international phone number")
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("8") and default_country_code == "7":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = default_country_code + digits
    if not 10 <= len(digits) <= 15 or digits.startswith("0"):
        raise ValueError("phone must be an international phone number with 10 to 15 digits")
    return f"+{digits}"
