from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_dpapi() -> bool:
    print("dpapi:")
    if sys.platform != "win32":
        return _check("skipped (not Windows; GEMINI_API_KEY is the path here)", True)
    from .creds import _dpapi
    secret = b"correct horse battery staple \xf0\x9f\x90\x8e"
    blob = _dpapi(secret, protect=True)
    ok = _check("blob is opaque", secret not in blob)
    ok &= _check("round-trip", _dpapi(blob, protect=False) == secret)
    return ok
