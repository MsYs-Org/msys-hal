from __future__ import annotations

from typing import Any


class HalError(RuntimeError):
    """A typed error safe to return over mIPC."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ValidationError(HalError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("HAL_BAD_PAYLOAD", message, details=details)


class UnavailableError(HalError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("HAL_UNAVAILABLE", message, details=details)


class ReadOnlyError(HalError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("HAL_READ_ONLY", message, details=details)


class ProviderError(HalError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("HAL_PROVIDER_ERROR", message, details=details)


class PersistenceError(HalError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("HAL_PERSISTENCE_ERROR", message, details=details)


class ConflictError(HalError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("HAL_CONFLICT", message, details=details)
