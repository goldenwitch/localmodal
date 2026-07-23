"""Durable filesystem primitives for source-control state transitions."""
from __future__ import annotations

import os
import uuid
from pathlib import Path


def fsync_directory(path: Path) -> None:
    """Flush a POSIX directory entry update; Windows uses write-through moves."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        _move_file_ex(source, destination, _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH)
    else:
        os.replace(source, destination)
        fsync_directory(destination.parent)


def unlink(path: Path) -> None:
    if os.name == "nt":
        try:
            tombstone = path.with_name(f".{path.name}.{uuid.uuid4().hex}.deleted")
            _move_file_ex(path, tombstone, _MOVEFILE_WRITE_THROUGH)
        except OSError as exc:
            if getattr(exc, "winerror", None) in (2, 3):
                return
            raise
        try:
            tombstone.unlink(missing_ok=True)
        except OSError as exc:
            pass
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        fsync_directory(path.parent)


def fsync_tree(root: Path) -> None:
    """Flush all private generation files before a publication can reference them."""
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("r+b") as file:
                os.fsync(file.fileno())
    for path in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        fsync_directory(path)
    fsync_directory(root)
    fsync_directory(root.parent)
    fsync_directory(root.parent.parent)


_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8


def _move_file_ex(source: Path, destination: Path | None, flags: int) -> None:
    """Perform a Windows namespace transition with the kernel write-through flag."""
    if os.name != "nt":
        raise RuntimeError("MoveFileEx is only available on Windows")
    import ctypes
    from ctypes import wintypes

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file_ex.restype = wintypes.BOOL
    if not move_file_ex(str(source), None if destination is None else str(destination), flags):
        raise ctypes.WinError(ctypes.get_last_error())
