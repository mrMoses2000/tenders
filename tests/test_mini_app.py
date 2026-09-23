from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from procurement_bot.mini_app import validate_public_url, verify_init_data


def _signed_data(token: str, *, auth_date: int, user_id: int) -> str:
    values = {"auth_date": str(auth_date), "user": json.dumps({"id": user_id})}
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_signed_fresh_telegram_init_data_is_accepted() -> None:
    assert verify_init_data(_signed_data("test-token", auth_date=1000, user_id=42),
                            "test-token", now=1200) == 42


def test_tampered_or_expired_init_data_is_rejected() -> None:
    valid = _signed_data("test-token", auth_date=1000, user_id=42)
    assert verify_init_data(valid, "other-token", now=1200) is None
    assert verify_init_data(valid.replace("42", "43"), "test-token", now=1200) is None
    assert verify_init_data(valid, "test-token", now=1000 + 24 * 3600 + 1) is None
    assert verify_init_data(valid + "&hash=duplicate", "test-token", now=1200) is None
    assert verify_init_data("", "test-token", now=1200) is None


def test_public_url_requires_https_and_normalizes_path() -> None:
    assert validate_public_url("https://example.com/miniapp") == "https://example.com/miniapp/"
    with pytest.raises(ValueError):
        validate_public_url("http://example.com/miniapp")
    with pytest.raises(ValueError):
        validate_public_url("https://example.com/miniapp?token=unsafe")
