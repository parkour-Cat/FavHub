"""The Chrome Native Messaging relay.

Chrome launches this process on demand and speaks its own framing to it:
four-byte little-endian lengths followed by UTF-8 JSON. All this module does is
translate that framing to and from the FavHub named pipe.

It deliberately owns nothing. It never opens the data root, never touches
SQLite, and never calls a platform parser — the running ``favhub-mcp`` process
already holds the data-root lock, and a second opener would simply be refused.
Keeping the relay stateless also keeps it uninteresting to attack: everything it
forwards is validated on the other side of the pipe.
"""

import argparse
import base64
import json
import struct
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, BinaryIO, Protocol, TextIO

from favhub.browser_pipe import DESCRIPTOR_SCHEMA_VERSION, PipeDescriptor
from favhub.config import FavHubPaths, InstallPaths, persisted_data_root

# Chrome's own ceiling for a message from an extension is 4 MiB; anything larger
# would be refused before it reached us, so matching it keeps the failure local.
MAX_NATIVE_MESSAGE_BYTES = 4 * 1024 * 1024
_HEADER = struct.Struct("<I")

NATIVE_ERROR_CODES = frozenset(
    {
        "invalid_message",
        "message_too_large",
        "mcp_unavailable",
        "extension_version_mismatch",
    }
)

_ERROR_MESSAGES = {
    "invalid_message": "The native message did not match the FavHub framing.",
    "message_too_large": "The native message exceeded the FavHub size limit.",
    "mcp_unavailable": "FavHub is not running for this data root.",
    "extension_version_mismatch": "FavHub and the browser relay disagree on the protocol.",
}


class NativeHostError(Exception):
    """A relay failure carrying a stable code and a fixed message."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in NATIVE_ERROR_CODES:
            raise ValueError(f"unknown native host error code: {code}")
        self.code = code
        # Kept local for stderr; never written to the extension, which would
        # turn the error channel into an echo of whatever the page sent.
        self.detail = detail
        super().__init__(f"{code}: {_ERROR_MESSAGES[code]}")

    @property
    def message(self) -> str:
        return _ERROR_MESSAGES[self.code]


class PipeLike(Protocol):
    def send_bytes(self, payload: bytes) -> None: ...

    def recv_bytes(self) -> bytes: ...

    def close(self) -> None: ...


def iter_native_messages(stream: BinaryIO) -> Iterator[dict[str, Any]]:
    """Yield one decoded message per Chrome frame until clean EOF.

    An oversize frame yields a marker rather than raising: refusing one batch
    must not end the relay. It used to, and the cost was severe — the extension
    had no other channel to FavHub, so losing this process lost the run's
    ability to report anything at all. The session simply froze in `capturing`
    until its lease ran out, with no pause and no code to show the user.
    """
    while True:
        header = stream.read(_HEADER.size)
        if not header:
            return
        if len(header) != _HEADER.size:
            raise NativeHostError("invalid_message", "truncated length header")
        (length,) = _HEADER.unpack(header)
        if length > MAX_NATIVE_MESSAGE_BYTES:
            # Drained in bounded chunks, never held: the point of the cap is to
            # not allocate what an inflated header claims. Draining is what
            # keeps the stream aligned for the next frame.
            _drain(stream, length)
            yield _OVERSIZE
            continue
        body = stream.read(length)
        if len(body) != length:
            raise NativeHostError("invalid_message", "truncated message body")
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise NativeHostError("invalid_message", "unparseable body") from error
        if not isinstance(parsed, dict):
            raise NativeHostError("invalid_message", "message must be an object")
        yield parsed


_DRAIN_CHUNK_BYTES = 256 * 1024

# Yielded in place of a decoded message when the frame was too large to accept.
# A sentinel rather than an exception because the relay carries on afterwards.
_OVERSIZE: dict[str, Any] = {}


def _drain(stream: BinaryIO, length: int) -> None:
    """Discard one frame's body without ever holding it in memory."""
    remaining = length
    while remaining > 0:
        chunk = stream.read(min(_DRAIN_CHUNK_BYTES, remaining))
        if not chunk:
            return
        remaining -= len(chunk)


def write_native_message(stream: BinaryIO, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    stream.write(_HEADER.pack(len(body)))
    stream.write(body)
    stream.flush()


def load_runtime_descriptor(paths: FavHubPaths) -> PipeDescriptor:
    """Read the descriptor the running MCP process publishes for this root."""
    try:
        raw = paths.browser_pipe_descriptor.read_text(encoding="utf-8")
    except OSError as error:
        raise NativeHostError("mcp_unavailable", "descriptor is missing") from error
    try:
        written = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as error:
        raise NativeHostError("mcp_unavailable", "descriptor is unreadable") from error
    if not isinstance(written, dict):
        raise NativeHostError("mcp_unavailable", "descriptor is not an object")
    if written.get("schemaVersion") != DESCRIPTOR_SCHEMA_VERSION:
        raise NativeHostError("extension_version_mismatch", "descriptor schema differs")
    try:
        return PipeDescriptor(
            pipe=str(written["pipe"]),
            auth_key=base64.b64decode(str(written["authKey"]), validate=True),
            pid=int(written["pid"]),
            protocol_version=int(written["protocolVersion"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise NativeHostError("mcp_unavailable", "descriptor is incomplete") from error


def _connect(descriptor: PipeDescriptor) -> PipeLike:
    from multiprocessing.connection import Client

    return Client(descriptor.pipe, family="AF_PIPE", authkey=descriptor.auth_key)


def run_native_host(
    stdin: BinaryIO,
    stdout: BinaryIO,
    stderr: TextIO,
    *,
    connect: Callable[[PipeDescriptor], PipeLike] = _connect,
    descriptor_loader: Callable[[], PipeDescriptor] | None = None,
) -> int:
    """Pump framed messages between Chrome and the FavHub pipe."""
    try:
        descriptor = (
            descriptor_loader() if descriptor_loader is not None else _no_descriptor_loader()
        )
    except NativeHostError as error:
        return _fail(stdout, stderr, error)

    try:
        pipe = connect(descriptor)
    except (OSError, EOFError) as error:
        return _fail(stdout, stderr, NativeHostError("mcp_unavailable", str(error)))

    try:
        for request in iter_native_messages(stdin):
            if request is _OVERSIZE:
                # Reported and survived: the extension learns why this batch was
                # refused and can pause with a real code, on a channel that is
                # still open.
                refusal = NativeHostError("message_too_large", "declared length above the cap")
                print(f"favhub-native-host: {refusal.code}: {refusal.detail}", file=stderr)
                write_native_message(stdout, _error_payload(refusal))
                continue
            pipe.send_bytes(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            try:
                raw = pipe.recv_bytes()
            except (OSError, EOFError) as error:
                return _fail(stdout, stderr, NativeHostError("mcp_unavailable", str(error)))
            write_native_message(stdout, _decode_reply(raw))
    except NativeHostError as error:
        return _fail(stdout, stderr, error)
    finally:
        pipe.close()
    return 0


def _decode_reply(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _error_payload(NativeHostError("mcp_unavailable", "unreadable pipe reply"))
    if not isinstance(parsed, dict):
        return _error_payload(NativeHostError("mcp_unavailable", "pipe reply is not an object"))
    return parsed


def _error_payload(error: NativeHostError) -> dict[str, Any]:
    return {"error": {"code": error.code, "message": error.message}}


def _fail(stdout: BinaryIO, stderr: TextIO, error: NativeHostError) -> int:
    # The detail goes to stderr, which Chrome routes to the extension's log for
    # the user; only the stable code crosses back into the page's reach.
    print(f"favhub-native-host: {error.code}: {error.detail}", file=stderr)
    write_native_message(stdout, _error_payload(error))
    return 1


def _no_descriptor_loader() -> PipeDescriptor:
    raise NativeHostError("mcp_unavailable", "no data root was configured")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="favhub-native-host",
        description="Relay Chrome Native Messaging to the local FavHub process.",
    )
    # Optional, and in practice never supplied: a Native Messaging host manifest
    # names an executable and nothing else, so Chrome has no way to pass a flag.
    # Requiring one here made argparse exit 2 before the relay read a byte, which
    # the extension could only see as an immediate disconnect. The root comes
    # from install.json instead — the same file `favhub setup` already wrote.
    parser.add_argument("--root", type=Path, default=None, help="FavHub data root")
    # Chrome appends the calling extension's origin (and on Windows a window
    # handle) to the command line; accept and ignore them.
    parser.add_argument("extras", nargs="*", help=argparse.SUPPRESS)
    return parser


def resolve_data_root(explicit: Path | None) -> Path:
    """The root to relay for: an explicit one, else the installed one."""
    if explicit is not None:
        return explicit
    installed = persisted_data_root(InstallPaths.from_local_app_data())
    if installed is None:
        raise NativeHostError("mcp_unavailable", "no data root was configured")
    return installed


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    known, _unknown = _build_parser().parse_known_args(argv)

    def descriptor_loader() -> PipeDescriptor:
        return load_runtime_descriptor(FavHubPaths.from_root(resolve_data_root(known.root)))

    return run_native_host(
        stdin or sys.stdin.buffer,
        stdout or sys.stdout.buffer,
        stderr or sys.stderr,
        descriptor_loader=descriptor_loader,
    )


__all__ = [
    "MAX_NATIVE_MESSAGE_BYTES",
    "NATIVE_ERROR_CODES",
    "NativeHostError",
    "iter_native_messages",
    "load_runtime_descriptor",
    "main",
    "resolve_data_root",
    "run_native_host",
    "write_native_message",
]
