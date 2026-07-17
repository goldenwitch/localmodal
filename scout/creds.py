#!/usr/bin/env python3
"""DPAPI-backed credential store for scout. Windows-only by design.

The Gemini API key never lives in the repo. It is encrypted with the Windows
Data Protection API bound to the *current user account* (plus app-specific
entropy) and stored under %LOCALAPPDATA%/localmodal-scout/. Decryption only
works on this machine as this user.

Escape hatch (and the non-Windows path): the GEMINI_API_KEY environment
variable, which always takes precedence when set.

Usage:
    python -m scout.creds set      # prompts (hidden input), encrypts, stores
    python -m scout.creds check    # decrypts and reports a fingerprint
    python -m scout.creds clear    # deletes the stored credential
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

STORE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "localmodal-scout" / "gemini_api_key.dpapi"
_ENTROPY = b"localmodal-scout-v1"
_UI_FORBIDDEN = 0x1  # CRYPTPROTECT_UI_FORBIDDEN: never pop a UI prompt


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Round data through CryptProtectData / CryptUnprotectData (user scope)."""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.restype = wintypes.BOOL
    # 64-bit pointers overflow ctypes' default c_int argtypes — declare them.
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    in_buf = ctypes.create_string_buffer(data, len(data))
    ent_buf = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    blob_in = _BLOB(len(data), ctypes.cast(in_buf, ctypes.c_void_p))
    blob_ent = _BLOB(len(_ENTROPY), ctypes.cast(ent_buf, ctypes.c_void_p))
    blob_out = _BLOB()

    ok = fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None,
            _UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        verb = "protect" if protect else "unprotect"
        raise OSError(f"DPAPI {verb} failed (WinError {ctypes.get_last_error()}); "
                      "wrong user account, or the blob was written elsewhere?")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def load_key() -> str | None:
    """Resolve the API key: env var first, then the DPAPI store. None if absent."""
    env = os.environ.get("GEMINI_API_KEY", "").strip()
    if env:
        return env
    if sys.platform != "win32" or not STORE.exists():
        return None
    return _dpapi(STORE.read_bytes(), protect=False).decode("utf-8")


def _fingerprint(key: str) -> str:
    """Safe-to-print identity check: ends + length, never the middle."""
    return f"{key[:4]}…{key[-4:]} ({len(key)} chars)"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    cmd = args[0] if args else "check"

    if sys.platform != "win32":
        print("DPAPI store is Windows-only; set GEMINI_API_KEY instead.", file=sys.stderr)
        return 1

    if cmd == "set":
        import getpass
        key = getpass.getpass("Gemini API key (input hidden): ").strip()
        # Hidden-prompt paste is the #1 failure mode: some consoles (the
        # PowerShell Extension console especially) deliver Ctrl+V as a literal
        # \x16 instead of the clipboard. Filter unprintables and sanity-check
        # length so a failed paste is refused instead of silently stored.
        key = "".join(ch for ch in key if ch.isprintable())
        if len(key) < 20:
            print(f"refusing: got {len(key)} printable chars — the paste almost "
                  "certainly failed. Use a regular pwsh terminal (not the "
                  "PowerShell Extension console) and try again.", file=sys.stderr)
            return 1
        if not key.startswith("AIza"):
            print("warning: key does not start with 'AIza' (AI Studio keys do); "
                  "storing anyway.", file=sys.stderr)
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_bytes(_dpapi(key.encode("utf-8"), protect=True))
        print(f"stored (DPAPI, user scope) -> {STORE}")
        print(f"fingerprint: {_fingerprint(key)}")
        return 0

    if cmd == "check":
        if os.environ.get("GEMINI_API_KEY"):
            print("GEMINI_API_KEY env var is set (takes precedence over the store)")
        if not STORE.exists():
            print(f"no stored credential at {STORE}")
            return 0 if os.environ.get("GEMINI_API_KEY") else 1
        key = _dpapi(STORE.read_bytes(), protect=False).decode("utf-8")
        print(f"store decrypts OK: {_fingerprint(key)}")
        return 0

    if cmd == "clear":
        if STORE.exists():
            STORE.unlink()
            print("cleared")
        else:
            print("nothing to clear")
        return 0

    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
