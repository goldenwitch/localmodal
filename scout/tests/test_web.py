from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_web() -> bool:
    print("web_search (live):")
    from .server import web_search
    out = web_search("What is the canonical Hugging Face repo id for the "
                     "Qwen3-Coder-80B-A3B model, if it exists?")
    print("  ---\n" + "\n".join("  " + l for l in out.splitlines()[:12]) + "\n  ---")
    ok = _check("wrapped untrusted", "UNTRUSTED" in out)
    ok &= _check("did not error", "web_search failed" not in out and "unavailable" not in out)
    return ok
