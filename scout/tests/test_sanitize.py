from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
from .. import sanitize

def test_sanitize() -> bool:
    print("sanitize:")
    dirty = (
        "A\u200bB\u202egood\u202c text"            # zero-width + bidi override
        "\n![beacon](https://evil.example/x.png)"   # image exfil beacon
        "\n<script>alert(1)</script>"               # html
        "\n[click](https://ok.example/a) and [bad](javascript:alert(1))"
        "\n\n\n\n\nend"
    )
    out = sanitize.clean(dirty)
    ok = True
    ok &= _check("zero-width stripped", "\u200b" not in out)
    ok &= _check("bidi stripped", "\u202e" not in out and "\u202c" not in out)
    ok &= _check("image removed", "beacon" not in out and "evil.example" not in out)
    ok &= _check("html removed", "<script>" not in out)
    ok &= _check("https link demoted", "click (https://ok.example/a)" in out)
    ok &= _check("non-https target dropped", "javascript:" not in out and "bad" in out)
    ok &= _check("blank lines collapsed", "\n\n\n" not in out)
    capped = sanitize.clean("x" * 9000)
    ok &= _check("length capped", capped.endswith("[truncated]") and len(capped) < 8100)
    wrapped = sanitize.wrap("T", "body")
    ok &= _check("wrapper labels untrusted", "UNTRUSTED" in wrapped and wrapped.endswith("<<<END T>>>"))
    return ok
