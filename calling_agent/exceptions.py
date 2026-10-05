from __future__ import annotations


class CallingAgentError(Exception):
    """Base exception for calling-agent operations."""


class CallingConfigurationError(CallingAgentError):
    """Raised when required provider configuration is missing."""


class CallAuthorizationError(CallingAgentError):
    """Raised when a user cannot act on a lead or call."""


class CallConflictError(CallingAgentError):
    """Raised when an idempotency or state conflict occurs."""


class LeadNotCallableError(CallingAgentError):
    """Raised when a lead cannot be called manually (DNC or blocked status)."""


class InvalidPhoneNumberError(CallingAgentError):
    """Raised when a lead phone number cannot be normalized to E.164."""


class ToolContextUnavailableError(CallingAgentError):
    """Raised when a call no longer has valid read-tool context."""


class ToolServiceUnavailableError(CallingAgentError):
    """Raised when a read-tool dependency cannot complete safely."""


class ToolActionRejectedError(CallingAgentError):
    """Raised when a valid tool action violates domain policy."""


class ElevenLabsError(CallingAgentError):
    """Base exception for ElevenLabs API failures."""


class ElevenLabsRequestError(ElevenLabsError):
    """Raised when ElevenLabs rejects a request."""


class ElevenLabsTransientError(ElevenLabsError):
    """Raised when provider acceptance is unknown after a transport failure."""


class ElevenLabsResponseError(ElevenLabsError):
    """Raised when ElevenLabs returns an invalid response."""


class WebhookVerificationError(CallingAgentError):
    """Raised when an ElevenLabs webhook signature is invalid."""


class WebhookProcessingError(CallingAgentError):
    """Raised when a verified webhook event cannot be processed."""
