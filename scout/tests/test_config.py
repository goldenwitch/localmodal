from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_config() -> bool:
    print("scout config:")
    import json
    import tempfile
    from pathlib import Path

    config = _resource_module("config")
    loaded = config.load_config()
    ok = _check(
        "checked-in config loads",
        loaded.schema_version == 1
        and loaded.ledger.lock_wait_seconds == 30
        and loaded.ledger.lock_poll_milliseconds == 100
        and loaded.fetch.request_timeout_seconds == 60
        and loaded.fetch.max_redirects == 5
        and loaded.fetch.max_response_bytes == 83_886_080
        and loaded.repo_files.publishable_paths
        == (
            "README.md",
            "localmodal.vine",
            "human-owned-spec/initial-spec.md",
            "proposals/scout-source-management.vine",
            "proposals/scout-vocabulary.md",
            "resources/papers.md",
            "scout/README.md",
        ),
    )
    diagnostic = config.foundation_diagnostic(config.DiagnosticCode.CONFIG_MISSING, path="fixture")
    ok &= _check(
        "foundation diagnostic shape",
        diagnostic.as_dict() == {
            "code": "CONFIG_MISSING",
            "evidence": {"path": "fixture"},
            "repair": "Create the checked-in Scout configuration at the reported path.",
        },
    )

    valid = {
        "schema_version": 1,
        "ledger": {"lock_wait_seconds": 30, "lock_poll_milliseconds": 100},
        "fetch": {
            "request_timeout_seconds": 60,
            "max_redirects": 5,
            "max_response_bytes": 83_886_080,
        },
        "repo_files": {"publishable_paths": ["README.md"]},
    }

    def codes(path: Path) -> set[str]:
        try:
            config.load_config(path)
        except config.ScoutDiagnosticsError as exc:
            return {diagnostic.code.value for diagnostic in exc.diagnostics}
        return set()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ok &= _check(
            "missing config is typed",
            codes(root / "missing.json") == {"CONFIG_MISSING"},
        )

        malformed = root / "malformed.json"
        malformed.write_text("{", encoding="utf-8")
        ok &= _check(
            "malformed config is typed",
            codes(malformed) == {"CONFIG_MALFORMED"},
        )

        unknown = root / "unknown.json"
        unknown_payload = {**valid, "unexpected": True}
        unknown.write_text(json.dumps(unknown_payload), encoding="utf-8")
        ok &= _check(
            "unknown config key is typed",
            codes(unknown) == {"CONFIG_UNKNOWN_KEY"},
        )

        invalid = root / "invalid.json"
        invalid_payload = {
            "schema_version": 2,
            "ledger": {"lock_wait_seconds": 1, "lock_poll_milliseconds": 1_001},
            "fetch": {
                "request_timeout_seconds": False,
                "max_redirects": -1,
                "max_response_bytes": 0,
            },
            "repo_files": {"publishable_paths": [".git/config"]},
        }
        invalid.write_text(json.dumps(invalid_payload), encoding="utf-8")
        invalid_codes = codes(invalid)
        ok &= _check(
            "wrong types and numeric domains aggregate",
            {"CONFIG_WRONG_TYPE", "CONFIG_INVALID_VALUE"}.issubset(invalid_codes),
            ", ".join(sorted(invalid_codes)),
        )
    return ok
