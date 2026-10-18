from __future__ import annotations

import json
import logging

import pytest

from procurement_bot.logging import JsonFormatter


def test_deferred_reason_is_visible_without_private_provider_context() -> None:
    record = logging.LogRecord("procurement_bot.outbox", logging.INFO, __file__, 1,
                               "WhatsApp outbox reconciliation deferred", (), None)
    record.reason = "provider_acceptance_unknown"
    record.recipient = "+77000000003"
    record.text = "private supplier text"
    record.payload = {"token": "private-credential"}
    output = json.loads(JsonFormatter().format(record))
    assert output["reason"] == "provider_acceptance_unknown"
    assert set(output) == {"time", "level", "logger", "message", "reason"}


@pytest.mark.parametrize("reason", [None, 3, "", "private text", "+77000000003",
                                     "token=private-credential", "a" * 129])
def test_arbitrary_reason_is_not_copied_to_json_logs(reason: object) -> None:
    record = logging.LogRecord("provider", logging.INFO, __file__, 1, "deferred", (), None)
    record.reason = reason
    assert "reason" not in json.loads(JsonFormatter().format(record))
