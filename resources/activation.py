"""One-way source-control activation marker shared by compatibility guards."""
from __future__ import annotations

import os
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path


MARKER_NAME = "ACTIVATED"
LOCK_NAME = "TRANSITION.LOCK"
_LOCK_STATES: dict[str, "_TransitionState"] = {}
_LOCK_STATES_GUARD = threading.Lock()


def activation_path(resources_root: Path) -> Path:
    return resources_root / ".scout-publications" / MARKER_NAME


def is_source_control_active(resources_root: Path) -> bool:
    return activation_path(resources_root).is_file()


@dataclass
class _TransitionState:
    mutex: threading.RLock
    depth: int = 0
    file: object | None = None


class TransitionLock(AbstractContextManager["TransitionLock"]):
    """Cross-process barrier between legacy compatibility work and activation."""

    def __init__(self, resources_root: Path) -> None:
        self.path = resources_root / ".scout-transition.lock"
        self._state: _TransitionState | None = None

    def __enter__(self) -> "TransitionLock":
        key = str(self.path.resolve())
        with _LOCK_STATES_GUARD:
            state = _LOCK_STATES.setdefault(key, _TransitionState(threading.RLock()))
        state.mutex.acquire()
        try:
            if state.depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                file = self.path.open("a+b")
                file.seek(0, os.SEEK_END)
                if file.tell() == 0:
                    file.write(b"\0")
                    file.flush()
                    os.fsync(file.fileno())
                file.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(file.fileno(), fcntl.LOCK_EX)
                state.file = file
            state.depth += 1
            self._state = state
        except Exception:
            state.mutex.release()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._state is None:
            return
        state = self._state
        try:
            state.depth -= 1
            if state.depth == 0:
                file = state.file
                if file is not None:
                    file.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
                    file.close()
                    state.file = None
        finally:
            self._state = None
            state.mutex.release()


def transition_lock(resources_root: Path) -> TransitionLock:
    return TransitionLock(resources_root)
