from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_ledger() -> bool:
    print("source ledger:")
    import json
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    config = _resource_module("config")
    ledger_module = _resource_module("ledger")
    model = _resource_module("source_model")
    fixture_config = config.ScoutConfig(
        schema_version=1,
        ledger=config.LedgerConfig(lock_wait_seconds=1, lock_poll_milliseconds=10),
        fetch=config.FetchConfig(request_timeout_seconds=1, max_redirects=0, max_response_bytes=1),
        repo_files=config.RepoFileConfig(publishable_paths=()),
    )
    record = model.SourceRecord(
        declaration=model.parse_declaration(
            {
                "name": "fixture-source",
                "origin": {"kind": "repo-file", "path": "README.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            }
        ),
        snapshot=None,
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ledger = ledger_module.Ledger(root, fixture_config)
        ok = _check("empty ledger loads", ledger.read().records == {})
        ok &= _check("direct add is atomic", ledger.add_if_absent(record))
        ok &= _check("duplicate direct add is refused", not ledger.add_if_absent(record))
        ok &= _check(
            "committed record reads",
            set(ledger.read().records) == {"fixture-source"},
        )

        journal = ledger.begin({"kind": "fixture", "rows": []})
        with ledger.claim_recovery() as lease:
            ok &= _check(
                "journal claim is durable",
                lease.journal.operation_id == journal.operation_id and lease.claim_id is not None,
            )
            contender = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; from config import ScoutConfig, LedgerConfig, FetchConfig, RepoFileConfig; from diagnostics import ScoutDiagnosticsError; from ledger import Ledger; "
                    "c=ScoutConfig(1, LedgerConfig(1,10), FetchConfig(1,0,1), RepoFileConfig(())); "
                    "\ntry:\n l=Ledger(Path(__import__('sys').argv[1]),c); x=l.claim_recovery(); print('CLAIMED' if x else 'NONE')\nexcept ScoutDiagnosticsError as e:\n print(e.diagnostics[0].code.value)",
                    str(root),
                ],
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "resources")},
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            ok &= _check(
                "independent recovery claimant is busy",
                contender.returncode == 0 and contender.stdout.strip() == "LEDGER_BUSY",
                contender.stdout.strip(),
            )
            lease.update("staged", candidate={"digest": "fixture"})
            lease.update("published", publication_id="publication-fixture")
            lease.complete({"fixture-source": record}, "publication-fixture")
        ok &= _check("completed journal disappears", not ledger.journal_path.exists())
        ok &= _check("completed journal preserves ledger", set(ledger.read().records) == {"fixture-source"})

        with ledger.begin_and_claim({"kind": "atomic", "rows": []}) as atomic_lease:
            ok &= _check(
                "new journal is atomically claimed",
                atomic_lease.journal.phase == "claimed" and atomic_lease.claim_id is not None,
            )
            atomic_lease.update("published", publication_id="publication-atomic")
            atomic_lease.complete({"fixture-source": record}, "publication-atomic")

        takeover_journal = ledger.begin({"kind": "takeover", "rows": []})
        abandoned = ledger.claim_recovery()
        assert abandoned is not None
        first_claim = abandoned.claim_id
        abandoned.update("staged", candidate={"digest": "reuse"})
        abandoned.__exit__(None, None, None)
        with ledger.claim_recovery() as takeover:
            ok &= _check(
                "released claimant can be taken over",
                takeover.claim_id != first_claim
                and takeover.journal.operation_id == takeover_journal.operation_id
                and takeover.journal.candidate == {"digest": "reuse"},
            )
            takeover.update("published", publication_id="publication-takeover")
            takeover.complete({"fixture-source": record}, "publication-takeover")

        ledger.journal_path.write_text("{", encoding="utf-8")
        try:
            ledger.read()
        except ledger_module.ScoutDiagnosticsError as exc:
            malformed = {diagnostic.code.value for diagnostic in exc.diagnostics}
        else:
            malformed = set()
        ok &= _check("malformed journal is typed", malformed == {"LEDGER_JOURNAL_MALFORMED"})
        ledger.journal_path.unlink()

        legacy = root / "legacy"
        legacy.mkdir()
        (legacy / ".scout-ledger.json").write_text(json.dumps({"old": {}}), encoding="utf-8")
        legacy_ledger = ledger_module.Ledger(legacy, fixture_config)
        try:
            legacy_ledger.read()
        except ledger_module.ScoutDiagnosticsError as exc:
            legacy_codes = {diagnostic.code.value for diagnostic in exc.diagnostics}
        else:
            legacy_codes = set()
        ok &= _check("legacy ledger requires migration", legacy_codes == {"LEGACY_MIGRATION_REQUIRED"})
    return ok
