"""Platform-neutral capture error contract shared by all connectors.

Stable error codes cross the parser -> mapper -> sync -> MCP boundary and end
up in logs and ``source.json``; they must never carry response bodies or
credentials, only a code plus a short redacted message.
"""

LOGIN_REQUIRED = "login_required"
PAGE_CHANGED = "page_changed"
SOURCE_UNAVAILABLE = "source_unavailable"
SUBTITLE_UNAVAILABLE = "subtitle_unavailable"
MALFORMED_SUBTITLE = "malformed_subtitle"
RATE_LIMITED = "rate_limited"

CAPTURE_ERROR_CODES = frozenset(
    {
        LOGIN_REQUIRED,
        PAGE_CHANGED,
        SOURCE_UNAVAILABLE,
        SUBTITLE_UNAVAILABLE,
        MALFORMED_SUBTITLE,
        RATE_LIMITED,
    }
)


# Browser/MCP transport failures (design §9). These describe why a browser
# capture session stopped, not why one item failed to parse, so they stay out of
# CAPTURE_ERROR_CODES and never widen parser-level validation.
EXTENSION_MISSING = "extension_missing"
EXTENSION_VERSION_MISMATCH = "extension_version_mismatch"
BROWSER_UNAVAILABLE = "browser_unavailable"
MCP_UNAVAILABLE = "mcp_unavailable"
CAPTCHA_REQUIRED = "captcha_required"
MESSAGE_TOO_LARGE = "message_too_large"
STORAGE_ERROR = "storage_error"
INVALID_MESSAGE = "invalid_message"
CANCELLED_BY_USER = "cancelled_by_user"

BROWSER_ERROR_CODES = frozenset(
    {
        EXTENSION_MISSING,
        EXTENSION_VERSION_MISMATCH,
        BROWSER_UNAVAILABLE,
        MCP_UNAVAILABLE,
        LOGIN_REQUIRED,
        CAPTCHA_REQUIRED,
        RATE_LIMITED,
        PAGE_CHANGED,
        MESSAGE_TOO_LARGE,
        STORAGE_ERROR,
        INVALID_MESSAGE,
        CANCELLED_BY_USER,
    }
)


class CaptureError(Exception):
    """A typed, redacted parsing failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        if code not in CAPTURE_ERROR_CODES:
            raise ValueError(f"unknown capture error code: {code}")
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


__all__ = [
    "BROWSER_ERROR_CODES",
    "BROWSER_UNAVAILABLE",
    "CANCELLED_BY_USER",
    "CAPTCHA_REQUIRED",
    "CAPTURE_ERROR_CODES",
    "EXTENSION_MISSING",
    "EXTENSION_VERSION_MISMATCH",
    "INVALID_MESSAGE",
    "LOGIN_REQUIRED",
    "MALFORMED_SUBTITLE",
    "MCP_UNAVAILABLE",
    "MESSAGE_TOO_LARGE",
    "PAGE_CHANGED",
    "RATE_LIMITED",
    "SOURCE_UNAVAILABLE",
    "STORAGE_ERROR",
    "SUBTITLE_UNAVAILABLE",
    "CaptureError",
]
