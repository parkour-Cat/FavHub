import os
from pathlib import Path
from typing import BinaryIO


class DataRootBusyError(RuntimeError):
    """Another process already holds this data root.

    Distinguished from every other startup failure because it is the one that
    is not a fault: a second FavHub window on the same root is an ordinary
    thing to do, and the caller can carry on and say so rather than die.
    """


class DataRootLock:
    """Hold an exclusive, non-blocking OS lock for one FavHub data root."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: BinaryIO | None = None

    @classmethod
    def acquire(cls, path: Path) -> "DataRootLock":
        lock = cls(path)
        lock._acquire()
        return lock

    def _acquire(self) -> None:
        if self._file is not None:
            raise RuntimeError(f"FavHub data root is already locked by this object: {self.path}")

        try:
            lock_file = self.path.open("a+b")
        except OSError as exc:
            raise RuntimeError(f"unable to open FavHub data root lock: {self.path}") from exc

        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            _lock_file(lock_file)
        except OSError as exc:
            lock_file.close()
            raise DataRootBusyError(
                f"FavHub data root is already in use (lock: {self.path})"
            ) from exc

        self._file = lock_file

    def release(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return

        self._file = None
        try:
            lock_file.seek(0)
            _unlock_file(lock_file)
        finally:
            lock_file.close()


if os.name == "nt":
    import msvcrt

    def _lock_file(lock_file: BinaryIO) -> None:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(lock_file: BinaryIO) -> None:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(lock_file: BinaryIO) -> None:
        fcntl.flock(  # type: ignore[attr-defined]
            lock_file.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
        )

    def _unlock_file(lock_file: BinaryIO) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


__all__ = ["DataRootBusyError", "DataRootLock"]
