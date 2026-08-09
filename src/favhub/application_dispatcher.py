"""One lock in front of a shared ``Application``.

Two callers reach the same open ``Application`` in the MCP process: the stdio
JSON-RPC loop and the named-pipe listener thread that serves the browser
extension. They share one SQLite connection, one data-root lock, and one set of
in-memory ingest buffers, none of which are safe to touch concurrently.

The connection is therefore opened with ``check_same_thread=False`` — but that
flag only removes Python's thread check, it grants no concurrency. Every entry
point must run inside :meth:`ApplicationDispatcher.operation`, which is what
actually makes the sharing safe.

The lock is reentrant on purpose: a pipe handler may call a gateway helper that
takes the lock again, and that must not deadlock against itself.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class ApplicationDispatcher:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    @contextmanager
    def operation(self) -> Iterator[None]:
        with self._lock:
            yield


__all__ = ["ApplicationDispatcher"]
