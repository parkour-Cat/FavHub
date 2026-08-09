"""The authenticated local pipe the browser relay talks to.

This is a Windows named pipe, not a network port: nothing is bound, nothing is
listening on localhost, and no other machine can reach it. Access needs the
pipe name *and* a per-run auth key, both generated fresh at startup and
published in ``<root>/state/browser-pipe.json`` while the server runs.

Only ``send_bytes``/``recv_bytes`` are used. ``multiprocessing.connection``'s
``send``/``recv`` are pickle-based and would let anything that reaches the pipe
execute code; framing our own JSON keeps the boundary inert.

The server owns no FavHub state. It hands each decoded request to a callback
supplied by the MCP process, which is where the dispatcher lock and the real
work live.
"""

import base64
import json
import os
import secrets
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from types import TracebackType
from typing import Any

from favhub.config import FavHubPaths

DESCRIPTOR_SCHEMA_VERSION = 1
MAX_PIPE_REQUEST_BYTES = 8 * 1024 * 1024
_AUTH_KEY_BYTES = 32
_STOP_TIMEOUT_SECONDS = 5

RequestHandler = Callable[[dict[str, Any]], dict[str, Any]]

_ERROR_MESSAGES = {
    "invalid_message": "The browser request did not match the FavHub protocol.",
    "message_too_large": "The browser request exceeded the FavHub size limit.",
    "storage_error": "FavHub could not complete the browser request.",
}


@dataclass(frozen=True, slots=True)
class PipeDescriptor:
    pipe: str
    auth_key: bytes
    pid: int
    protocol_version: int

    def as_json(self) -> str:
        return json.dumps(
            {
                "schemaVersion": DESCRIPTOR_SCHEMA_VERSION,
                "pipe": self.pipe,
                "authKey": base64.b64encode(self.auth_key).decode("ascii"),
                "pid": self.pid,
                "protocolVersion": self.protocol_version,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _error(code: str) -> bytes:
    return json.dumps(
        {"error": {"code": code, "message": _ERROR_MESSAGES[code]}},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")


class BrowserPipeServer:
    def __init__(
        self,
        paths: FavHubPaths,
        handler: RequestHandler,
        *,
        protocol_version: int = 1,
    ) -> None:
        self._paths = paths
        self._handler = handler
        self._protocol_version = protocol_version
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._descriptor: PipeDescriptor | None = None
        self._stopping = threading.Event()

    @property
    def descriptor(self) -> PipeDescriptor:
        if self._descriptor is None:
            raise RuntimeError("browser pipe server is not running")
        return self._descriptor

    def start(self) -> None:
        if self._listener is not None:
            raise RuntimeError("browser pipe server is already running")
        # A fresh name and key per run: a stale descriptor from a crashed
        # process can never authenticate against a new server.
        name = rf"\\.\pipe\favhub-{secrets.token_hex(16)}"
        auth_key = secrets.token_bytes(_AUTH_KEY_BYTES)
        listener = Listener(name, family="AF_PIPE", authkey=auth_key)
        descriptor = PipeDescriptor(
            pipe=name,
            auth_key=auth_key,
            pid=os.getpid(),
            protocol_version=self._protocol_version,
        )
        self._listener = listener
        self._descriptor = descriptor
        self._stopping.clear()
        self._thread = threading.Thread(target=self._serve, name="favhub-browser-pipe", daemon=True)
        self._thread.start()
        # Publishing last means a relay that finds a descriptor can always
        # connect; the alternative races the listener into existence.
        self._write_descriptor(descriptor)

    def stop(self) -> None:
        if self._listener is None:
            return
        self._stopping.set()
        listener = self._listener
        descriptor = self._descriptor
        self._listener = None

        # Closing an AF_PIPE listener does not interrupt a blocked accept() on
        # Windows, so the serving thread needs one throwaway connection to wake
        # up and notice the stop flag. That connection must happen on its own
        # daemon thread: if the serving thread already left the loop, nothing
        # will ever complete the handshake and a foreground connect would hang
        # stop() forever. Closing the listener below is what frees it instead.
        waker: threading.Thread | None = None
        if descriptor is not None:
            waker = threading.Thread(
                target=self._wake_listener,
                args=(descriptor,),
                name="favhub-browser-pipe-waker",
                daemon=True,
            )
            waker.start()

        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=_STOP_TIMEOUT_SECONDS)
        with suppress(OSError):
            listener.close()
        if waker is not None:
            waker.join(timeout=_STOP_TIMEOUT_SECONDS)
        self._remove_descriptor()
        self._descriptor = None

    @staticmethod
    def _wake_listener(descriptor: PipeDescriptor) -> None:
        try:
            waker = Client(descriptor.pipe, family="AF_PIPE", authkey=descriptor.auth_key)
        except (OSError, EOFError, AuthenticationError):
            # Nothing was waiting on accept, which is equally fine.
            return
        waker.close()

    def __enter__(self) -> "BrowserPipeServer":
        self.start()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.stop()

    # -- listener ------------------------------------------------------------

    def _serve(self) -> None:
        listener = self._listener
        while listener is not None and not self._stopping.is_set():
            try:
                connection = listener.accept()
            except (OSError, EOFError, AuthenticationError):
                # A refused auth key or a closed listener is routine; keep
                # serving unless we are shutting down.
                listener = self._listener
                continue
            except Exception:
                listener = self._listener
                continue
            with connection:
                self._serve_connection(connection)
            listener = self._listener

    def _serve_connection(self, connection: Connection) -> None:
        while not self._stopping.is_set():
            try:
                # The size cap is enforced in _respond rather than through
                # recv_bytes(maxlength=...): a Windows named pipe hands over
                # whole messages, so refusing mid-read is not available anyway,
                # and this way an oversize caller gets a stable code instead of
                # a dropped connection.
                raw = connection.recv_bytes()
            except (OSError, EOFError, ValueError):
                return
            self._reply(connection, self._respond(bytes(raw)))

    def _respond(self, raw: bytes) -> bytes:
        if len(raw) > MAX_PIPE_REQUEST_BYTES:
            return _error("message_too_large")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return _error("invalid_message")
        if not isinstance(parsed, Mapping):
            return _error("invalid_message")
        try:
            result = self._handler(dict(parsed))
        except Exception:
            # Handler internals never cross the boundary: a message could
            # otherwise carry file paths or item content back to the page.
            return _error("storage_error")
        return json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")

    @staticmethod
    def _reply(connection: Connection, payload: bytes) -> None:
        try:
            connection.send_bytes(payload)
        except (OSError, EOFError):
            return

    # -- descriptor ----------------------------------------------------------

    def _write_descriptor(self, descriptor: PipeDescriptor) -> None:
        target = self._paths.browser_pipe_descriptor
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        temporary.write_text(descriptor.as_json(), encoding="utf-8")
        _restrict_to_current_user(temporary)
        os.replace(temporary, target)

    def _remove_descriptor(self) -> None:
        target = self._paths.browser_pipe_descriptor
        try:
            written = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return
        # Another process may have taken over the data root after we did; do not
        # delete a live sibling's descriptor.
        if written.get("pid") != os.getpid():
            return
        with suppress(OSError):
            target.unlink()


def _restrict_to_current_user(path: Path) -> None:
    """Best-effort user-only permissions; never fatal if the OS declines."""
    with suppress(OSError):
        os.chmod(path, 0o600)


__all__ = [
    "DESCRIPTOR_SCHEMA_VERSION",
    "MAX_PIPE_REQUEST_BYTES",
    "BrowserPipeServer",
    "PipeDescriptor",
    "RequestHandler",
]
