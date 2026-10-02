from __future__ import annotations

import copy

from scripts.configure_waha_webhook import _merge_env_headers, merge_session_config

HOOK_URL = "http://172.29.247.1:18081/webhooks/waha"
NEW_MARKER = "new-secret-marker"
OLD_MARKER = "old-secret-marker"


def test_merge_adds_hook_and_preserves_existing_session_config() -> None:
    existing = {
        "metadata": {"client": "operator", "attempts": 4},
        "webhooks": [
            {
                "url": "http://other.internal/hook",
                "events": ["session.status"],
                "customHeaders": [{"name": "X-Other", "value": "keep"}],
            }
        ],
    }
    before = copy.deepcopy(existing)

    result = merge_session_config(existing, webhook_url=HOOK_URL, secret=NEW_MARKER)

    assert existing == before
    assert result["metadata"] == existing["metadata"]
    assert result["webhooks"][0] == existing["webhooks"][0]
    assert result["webhooks"][1] == {
        "url": HOOK_URL,
        "events": ["message"],
        "customHeaders": [{"name": "X-Webhook-Secret", "value": NEW_MARKER}],
    }


def test_merge_is_idempotent() -> None:
    first = merge_session_config({}, webhook_url=HOOK_URL, secret=NEW_MARKER)

    second = merge_session_config(first, webhook_url=HOOK_URL, secret=NEW_MARKER)

    assert second == first


def test_merge_updates_existing_secret_and_adds_message_event() -> None:
    existing = {
        "webhooks": [
            {
                "url": HOOK_URL,
                "events": ["session.status"],
                "customHeaders": [
                    {"name": "X-Webhook-Secret", "value": OLD_MARKER},
                    {"name": "X-Trace", "value": "preserve-marker"},
                ],
                "retries": {"attempts": 3},
            }
        ],
        "otherSetting": True,
    }

    result = merge_session_config(existing, webhook_url=HOOK_URL, secret=NEW_MARKER)

    hook = result["webhooks"][0]
    assert hook["events"] == ["session.status", "message"]
    assert hook["customHeaders"] == [
        {"name": "X-Webhook-Secret", "value": NEW_MARKER},
        {"name": "X-Trace", "value": "preserve-marker"},
    ]
    assert hook["retries"] == {"attempts": 3}
    assert result["otherSetting"] is True


def test_env_header_merge_replaces_secret_and_preserves_other_headers() -> None:
    result = _merge_env_headers(
        "Authorization:Bearer marker;X-Webhook-Secret:old-marker;X-Trace:preserve",
        NEW_MARKER,
    )

    assert result == f"Authorization:Bearer marker;X-Webhook-Secret:{NEW_MARKER};X-Trace:preserve"
