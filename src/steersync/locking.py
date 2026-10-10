"""Cross process exclusive file locks.

Several steersync processes can share one cache clone (two pushes in different
repositories, a Kiro session hook and a git hook at the same time). Every
operation on a cache clone holds an exclusive lock on a sibling lock file.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from steersync.errors import LockTimeoutError

log = logging.getLogger(__name__)

if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _try_lock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(path: Path, *, timeout: float, poll_interval: float = 0.2) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` for the duration of the block.

    The lock is released automatically by the operating system if the process
    dies, so a crashed run never leaves a stale lock behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + timeout
    with path.open("a+b") as handle:
        announced = False
        while True:
            try:
                _try_lock(handle)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    msg = f"timed out after {timeout:.0f}s waiting for lock {path}"
                    raise LockTimeoutError(msg) from exc
                if not announced:
                    log.info("waiting for another steersync process (%s)", path)
                    announced = True
                time.sleep(poll_interval)
        try:
            yield
        finally:
            _unlock(handle)
