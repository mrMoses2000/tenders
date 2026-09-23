class ProcurementBotError(RuntimeError):
    """Base application error."""


class AuthorizationError(ProcurementBotError):
    pass


class ValidationBlocked(ProcurementBotError):
    pass


class RetryableProviderError(ProcurementBotError):
    pass


class PermanentProviderError(ProcurementBotError):
    pass


class LostJobLease(ProcurementBotError):
    pass
