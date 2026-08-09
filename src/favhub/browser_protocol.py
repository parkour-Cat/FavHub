"""The closed message contract between the extension and FavHub.

Everything crossing this boundary is untrusted: a website can forge page
messages, so the extension's own claims about platform, session, or content are
checked here rather than believed. The rules are deliberately narrow — a fixed
envelope, a fixed type list, hard size caps, and an outright ban on
credential-shaped keys at any depth.

Errors carry a stable code and a fixed message. Nothing from the offending
payload is ever echoed back, so a malicious page cannot use the error channel
to exfiltrate what it just sent.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from favhub.browser_capture import BROWSER_PROTOCOL_VERSION

MAX_BROWSER_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_CHUNK_BYTES = 2 * 1024 * 1024
MAX_REQUEST_ID_LENGTH = 64

ALLOWED_MESSAGE_TYPES = frozenset(
    {
        "session.claim",
        "session.heartbeat",
        "session.pause",
        "session.finish",
        "session.cancel",
        "scope.declare",
        "capture.response",
        "capture.bundle",
    }
)

_ENVELOPE_KEYS = frozenset({"protocolVersion", "requestId", "type", "payload"})

# Key names that would mean credentials are crossing the boundary. FavHub never
# needs any of them: the browser already attaches cookies to its own same-origin
# requests, so a message carrying one is either a bug or an attack.
FORBIDDEN_KEYS = frozenset(
    {
        "cookie",
        "cookies",
        "authorization",
        "headers",
        "token",
        "bearer",
        "csrf",
        "ct0",
        "auth_token",
        "sessdata",
        "z_c0",
    }
)

PROTOCOL_ERROR_CODES = frozenset(
    {
        "invalid_message",
        "message_too_large",
        "protocol_mismatch",
        "credential_field_rejected",
    }
)

_PROTOCOL_ERROR_MESSAGES = {
    "invalid_message": "The browser message did not match the FavHub protocol.",
    "message_too_large": "The browser message exceeded the FavHub size limit.",
    "protocol_mismatch": "The extension protocol version does not match FavHub.",
    "credential_field_rejected": "The browser message contained a credential-shaped field.",
}

# Split on anything that is not a letter or digit so "Set-Cookie", "x_csrf_token",
# and "authToken" all reduce to parts that can be compared with FORBIDDEN_KEYS.
_KEY_PARTS = re.compile(r"[^a-z0-9]+")


class BrowserProtocolError(Exception):
    """A rejected message, carrying only a stable code and a fixed message."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in PROTOCOL_ERROR_CODES:
            raise ValueError(f"unknown browser protocol error code: {code}")
        self.code = code
        # ``detail`` stays local for the caller's own logging decision; it is
        # never placed in the wire payload.
        self.detail = detail
        super().__init__(f"{code}: {_PROTOCOL_ERROR_MESSAGES[code]}")

    @property
    def message(self) -> str:
        return _PROTOCOL_ERROR_MESSAGES[self.code]


@dataclass(frozen=True, slots=True)
class BrowserMessage:
    protocol_version: int
    request_id: str
    type: str
    payload: dict[str, Any]


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _key_is_forbidden(key: str) -> bool:
    lowered = key.lower()
    if lowered in FORBIDDEN_KEYS:
        return True
    parts = {part for part in _KEY_PARTS.split(lowered) if part}
    return bool(parts & FORBIDDEN_KEYS)


def _assert_no_credentials(value: Any) -> None:
    """Walk the whole payload; a credential key anywhere fails the message.

    Checking only the top level would be pointless, since the extension nests
    raw platform responses several levels deep.
    """
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and _key_is_forbidden(key):
                raise BrowserProtocolError("credential_field_rejected", key)
            _assert_no_credentials(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_credentials(nested)


def _assert_chunk_sizes(value: Any) -> None:
    """Bound any single string so one page response cannot dominate a message."""
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_RESPONSE_CHUNK_BYTES:
            raise BrowserProtocolError("message_too_large", "response chunk")
    elif isinstance(value, dict):
        for nested in value.values():
            _assert_chunk_sizes(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_chunk_sizes(nested)


def decode_message(raw: bytes) -> BrowserMessage:
    """Parse and validate one message, or raise ``BrowserProtocolError``."""
    if len(raw) > MAX_BROWSER_MESSAGE_BYTES:
        raise BrowserProtocolError("message_too_large", "envelope")
    try:
        parsed = json.loads(raw.decode("utf-8"), parse_constant=_reject_nonstandard_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise BrowserProtocolError("invalid_message", "unparseable") from error
    if not isinstance(parsed, dict):
        raise BrowserProtocolError("invalid_message", "envelope must be an object")

    unknown = set(parsed) - _ENVELOPE_KEYS
    if unknown:
        raise BrowserProtocolError("invalid_message", "unknown envelope key")

    version = parsed.get("protocolVersion")
    if isinstance(version, bool) or not isinstance(version, int):
        raise BrowserProtocolError("invalid_message", "protocolVersion must be an integer")
    if version != BROWSER_PROTOCOL_VERSION:
        raise BrowserProtocolError("protocol_mismatch", "unsupported protocol version")

    request_id = parsed.get("requestId")
    if (
        not isinstance(request_id, str)
        or not request_id.strip()
        or len(request_id) > MAX_REQUEST_ID_LENGTH
    ):
        raise BrowserProtocolError("invalid_message", "requestId must be a bounded string")

    message_type = parsed.get("type")
    if not isinstance(message_type, str) or message_type not in ALLOWED_MESSAGE_TYPES:
        raise BrowserProtocolError("invalid_message", "unknown message type")

    payload = parsed.get("payload")
    if not isinstance(payload, dict):
        raise BrowserProtocolError("invalid_message", "payload must be an object")

    _assert_no_credentials(payload)
    _assert_chunk_sizes(payload)
    return BrowserMessage(
        protocol_version=version,
        request_id=request_id,
        type=message_type,
        payload=payload,
    )


def encode_response(
    request_id: str,
    payload: dict[str, Any] | None,
    *,
    error: BrowserProtocolError | None = None,
) -> bytes:
    """Render one reply. Errors carry the fixed message, never the input."""
    if error is not None:
        body: dict[str, Any] = {"code": error.code, "message": error.message}
        message_type = "error"
    else:
        body = payload or {}
        message_type = "result"
    return json.dumps(
        {
            "protocolVersion": BROWSER_PROTOCOL_VERSION,
            "requestId": request_id,
            "type": message_type,
            "payload": body,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")


__all__ = [
    "ALLOWED_MESSAGE_TYPES",
    "FORBIDDEN_KEYS",
    "MAX_BROWSER_MESSAGE_BYTES",
    "MAX_REQUEST_ID_LENGTH",
    "MAX_RESPONSE_CHUNK_BYTES",
    "PROTOCOL_ERROR_CODES",
    "BrowserMessage",
    "BrowserProtocolError",
    "decode_message",
    "encode_response",
]
