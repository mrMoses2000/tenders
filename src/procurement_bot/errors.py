from datetime import UTC, datetime, timedelta


class ProcurementBotError(RuntimeError):
    """Base application error."""


class AuthorizationError(ProcurementBotError):
    pass


class ValidationBlocked(ProcurementBotError):
    pass


class RetryableProviderError(ProcurementBotError):
    pass


class ProviderAuthenticationDeferred(RetryableProviderError):
    """Unavailable AGY authentication suspends work without exhausting attempts."""

    diagnostic_code = "cli_auth_failed"
    reason = "provider_auth_missing"
    _REASONS = frozenset({"provider_auth_missing", "provider_auth_failed"})

    def __init__(
        self,
        available_at: datetime | None = None,
        *,
        reason: str = "provider_auth_missing",
    ) -> None:
        if reason not in self._REASONS:
            raise ValueError("unsupported provider authentication wait reason")
        self.available_at = available_at or datetime.now(UTC) + timedelta(minutes=30)
        self.reason = reason
        super().__init__(reason)


class PermanentProviderError(ProcurementBotError):
    pass


class LostJobLease(ProcurementBotError):
    pass
