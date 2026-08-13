"""Application error hierarchy and the single HTTP error envelope - PRINCIPAL-OWNED.

Every non-2xx response uses one envelope (docs/planning/03-api-contract.md §3):

    {"error": {"code", "message", "status", "details", "request_id", "timestamp"}}

``message`` must be safe for display: no SQL, no stack traces, no file paths, and
never another organization's data. Cross-organization access is always
``not_found`` (404), never 403 - existence must not leak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "validation_error"  # 422
    UNAUTHENTICATED = "unauthenticated"  # 401
    CSRF_FAILED = "csrf_failed"  # 403
    FORBIDDEN_ROLE = "forbidden_role"  # 403
    NOT_FOUND = "not_found"  # 404 (absent OR other-org)
    CONFLICT_STATE = "conflict_state"  # 409
    CONFLICT_VERSION = "conflict_version"  # 409 (If-Match mismatch)
    CONFLICT_DUPLICATE = "conflict_duplicate"  # 409
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"  # 415
    PAYLOAD_TOO_LARGE = "payload_too_large"  # 413
    RATE_LIMITED = "rate_limited"  # 429
    PROVIDER_UNAVAILABLE = "provider_unavailable"  # 502
    INTERNAL_ERROR = "internal_error"  # 500


_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.CSRF_FAILED: 403,
    ErrorCode.FORBIDDEN_ROLE: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT_STATE: 409,
    ErrorCode.CONFLICT_VERSION: 409,
    ErrorCode.CONFLICT_DUPLICATE: 409,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PROVIDER_UNAVAILABLE: 502,
    ErrorCode.INTERNAL_ERROR: 500,
}

_VALUE_HINT_MAX = 64
_SECRETISH = ("password", "token", "secret", "key", "authorization", "cookie")


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    field: str | None = None
    issue: str = ""
    value_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"issue": self.issue}
        if self.field is not None:
            out["field"] = self.field
            lowered = self.field.lower()
            if self.value_hint is not None and not any(s in lowered for s in _SECRETISH):
                out["value_hint"] = self.value_hint[:_VALUE_HINT_MAX]
        elif self.value_hint is not None:
            out["value_hint"] = self.value_hint[:_VALUE_HINT_MAX]
        return out


class AppError(Exception):
    """Base for all deliberate application errors."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        details: list[ErrorDetail] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []

    @property
    def status(self) -> int:
        return _STATUS[self.code]

    def envelope(self, request_id: str, timestamp: str) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "status": self.status,
                "details": [d.to_dict() for d in self.details],
                "request_id": request_id,
                "timestamp": timestamp,
            }
        }


class ValidationAppError(AppError):
    code = ErrorCode.VALIDATION_ERROR


class UnauthenticatedError(AppError):
    code = ErrorCode.UNAUTHENTICATED

    def __init__(self, message: str = "Authentication required.") -> None:
        super().__init__(message)


class CsrfError(AppError):
    code = ErrorCode.CSRF_FAILED

    def __init__(self, message: str = "CSRF verification failed.") -> None:
        super().__init__(message)


class ForbiddenRoleError(AppError):
    code = ErrorCode.FORBIDDEN_ROLE

    def __init__(self, message: str = "Your role does not permit this action.") -> None:
        super().__init__(message)


class NotFoundError(AppError):
    """Used for both genuinely absent and other-organization resources."""

    code = ErrorCode.NOT_FOUND

    def __init__(self, resource: str = "Resource") -> None:
        super().__init__(f"{resource} not found.")


class ConflictStateError(AppError):
    code = ErrorCode.CONFLICT_STATE


class ConflictVersionError(AppError):
    code = ErrorCode.CONFLICT_VERSION

    def __init__(self, message: str = "The resource was modified by someone else.") -> None:
        super().__init__(message)


class ConflictDuplicateError(AppError):
    code = ErrorCode.CONFLICT_DUPLICATE


class UnsupportedMediaTypeError(AppError):
    code = ErrorCode.UNSUPPORTED_MEDIA_TYPE


class PayloadTooLargeError(AppError):
    code = ErrorCode.PAYLOAD_TOO_LARGE


class RateLimitedError(AppError):
    code = ErrorCode.RATE_LIMITED

    def __init__(self, message: str = "Too many requests; slow down.") -> None:
        super().__init__(message)


class ProviderUnavailableError(AppError):
    code = ErrorCode.PROVIDER_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class SafeInternalError:
    """Rendered for unexpected exceptions: generic message + request id only."""

    request_id: str
    timestamp: str
    details: list[ErrorDetail] = field(default_factory=list)

    def envelope(self) -> dict[str, Any]:
        return {
            "error": {
                "code": ErrorCode.INTERNAL_ERROR.value,
                "message": "An unexpected error occurred.",
                "status": 500,
                "details": [],
                "request_id": self.request_id,
                "timestamp": self.timestamp,
            }
        }
