import json

import pytest

from favhub.browser_capture import BROWSER_PROTOCOL_VERSION
from favhub.browser_protocol import (
    ALLOWED_MESSAGE_TYPES,
    FORBIDDEN_KEYS,
    MAX_BROWSER_MESSAGE_BYTES,
    MAX_RESPONSE_CHUNK_BYTES,
    BrowserMessage,
    BrowserProtocolError,
    decode_message,
    encode_response,
)


def envelope(**overrides: object) -> dict[str, object]:
    message: dict[str, object] = {
        "protocolVersion": BROWSER_PROTOCOL_VERSION,
        "requestId": "r-000001",
        "type": "session.claim",
        "payload": {"platform": "x", "extensionVersion": "0.1.0"},
    }
    message.update(overrides)
    return message


def raw(**overrides: object) -> bytes:
    return json.dumps(envelope(**overrides)).encode("utf-8")


def test_a_well_formed_message_decodes_to_a_typed_envelope() -> None:
    message = decode_message(raw())
    assert isinstance(message, BrowserMessage)
    assert message.protocol_version == BROWSER_PROTOCOL_VERSION
    assert message.request_id == "r-000001"
    assert message.type == "session.claim"
    assert message.payload == {"platform": "x", "extensionVersion": "0.1.0"}


def test_allowed_types_are_exactly_the_designed_set() -> None:
    assert (
        frozenset(
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
        == ALLOWED_MESSAGE_TYPES
    )


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "invalid_message"),
        (b"not json", "invalid_message"),
        (b"[]", "invalid_message"),
        (b'"a string"', "invalid_message"),
        (b"123", "invalid_message"),
    ],
)
def test_malformed_json_is_rejected(payload: bytes, code: str) -> None:
    with pytest.raises(BrowserProtocolError) as error:
        decode_message(payload)
    assert error.value.code == code


def test_non_standard_json_numbers_are_rejected() -> None:
    for literal in (b'{"protocolVersion": NaN}', b'{"protocolVersion": Infinity}'):
        with pytest.raises(BrowserProtocolError) as error:
            decode_message(literal)
        assert error.value.code == "invalid_message"


def test_a_mismatched_protocol_version_is_reported_separately() -> None:
    with pytest.raises(BrowserProtocolError) as error:
        decode_message(raw(protocolVersion=BROWSER_PROTOCOL_VERSION + 1))
    assert error.value.code == "protocol_mismatch"


def test_a_missing_or_non_integer_version_is_invalid() -> None:
    for value in ("1", None, True, 1.0):
        with pytest.raises(BrowserProtocolError) as error:
            decode_message(raw(protocolVersion=value))
        assert error.value.code in {"invalid_message", "protocol_mismatch"}


def test_unknown_message_types_are_rejected() -> None:
    with pytest.raises(BrowserProtocolError) as error:
        decode_message(raw(type="session.destroy"))
    assert error.value.code == "invalid_message"


def test_unknown_envelope_keys_are_rejected() -> None:
    message = envelope()
    message["extra"] = "nope"
    with pytest.raises(BrowserProtocolError) as error:
        decode_message(json.dumps(message).encode("utf-8"))
    assert error.value.code == "invalid_message"


@pytest.mark.parametrize("request_id", ["", "   ", None, 7, "r" * 200])
def test_blank_or_oversized_request_ids_are_rejected(request_id: object) -> None:
    with pytest.raises(BrowserProtocolError) as error:
        decode_message(raw(requestId=request_id))
    assert error.value.code == "invalid_message"


def test_a_non_object_payload_is_rejected() -> None:
    for payload in ([], "x", 1, None):
        with pytest.raises(BrowserProtocolError) as error:
            decode_message(raw(payload=payload))
        assert error.value.code == "invalid_message"


def test_messages_above_the_size_cap_are_rejected_before_parsing() -> None:
    oversized = b"{" + b"x" * MAX_BROWSER_MESSAGE_BYTES
    with pytest.raises(BrowserProtocolError) as error:
        decode_message(oversized)
    assert error.value.code == "message_too_large"


@pytest.mark.parametrize("key", sorted(FORBIDDEN_KEYS))
def test_credential_shaped_keys_are_rejected_at_any_depth(key: str) -> None:
    nested = envelope(payload={"platform": "x", "bundle": {"nested": [{key: "value"}]}})
    with pytest.raises(BrowserProtocolError) as error:
        decode_message(json.dumps(nested).encode("utf-8"))
    assert error.value.code == "credential_field_rejected"


def test_credential_key_matching_ignores_case_and_surrounding_punctuation() -> None:
    for key in ("Cookie", "AUTHORIZATION", "Set-Cookie", "x_csrf_token"):
        nested = envelope(payload={"platform": "x", "headers_like": {key: "v"}})
        with pytest.raises(BrowserProtocolError) as error:
            decode_message(json.dumps(nested).encode("utf-8"))
        assert error.value.code == "credential_field_rejected"


def test_a_response_chunk_above_its_own_cap_is_rejected() -> None:
    body = "x" * (MAX_RESPONSE_CHUNK_BYTES + 1)
    message = envelope(type="capture.response", payload={"platform": "x", "body": body})
    with pytest.raises(BrowserProtocolError) as error:
        decode_message(json.dumps(message).encode("utf-8"))
    assert error.value.code == "message_too_large"


def test_a_response_chunk_at_the_cap_is_accepted() -> None:
    body = "x" * MAX_RESPONSE_CHUNK_BYTES
    message = envelope(type="capture.response", payload={"platform": "x", "body": body})
    decoded = decode_message(json.dumps(message).encode("utf-8"))
    assert decoded.type == "capture.response"


def test_encode_response_round_trips_and_stays_closed() -> None:
    encoded = encode_response("r-000001", {"status": "ok"})
    payload = json.loads(encoded)
    assert set(payload) == {"protocolVersion", "requestId", "type", "payload"}
    assert payload["type"] == "result"
    assert payload["payload"] == {"status": "ok"}


def test_encode_error_uses_a_stable_code() -> None:
    encoded = encode_response("r-000001", None, error=BrowserProtocolError("invalid_message", "x"))
    payload = json.loads(encoded)
    assert payload["type"] == "error"
    assert payload["payload"]["code"] == "invalid_message"
    assert "x" not in json.dumps(payload["payload"]["message"])


def test_an_unknown_error_code_cannot_be_constructed() -> None:
    with pytest.raises(ValueError):
        BrowserProtocolError("something_bad", "detail")
