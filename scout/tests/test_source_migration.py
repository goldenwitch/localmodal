from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_source_migration() -> bool:
    print("source migration:")
    import contextlib
    import io
    import json
    import tempfile
    from pathlib import Path

    config = _resource_module("config")
    migration = _resource_module("source_migration")
    control_module = _resource_module("source_control")
    fixture_config = config.ScoutConfig(
        schema_version=1,
        ledger=config.LedgerConfig(lock_wait_seconds=1, lock_poll_milliseconds=10),
        fetch=config.FetchConfig(request_timeout_seconds=1, max_redirects=0, max_response_bytes=1024),
        repo_files=config.RepoFileConfig(publishable_paths=()),
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        repository = root / "repo"
        repository.mkdir()
        legacy = repository / "legacy.md"
        legacy.write_text("migrated legacy text", encoding="utf-8")
        manifest_path = repository / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "rows": [
                        {
                            "op": "add",
                            "name": "migrated-source",
                            "origin": {"kind": "https", "url": "https://example.test/migrated.md"},
                            "mime": "text/markdown",
                            "ttl_days": 30,
                        }
                    ],
                    "imports": {"0": "legacy.md"},
                }
            ),
            encoding="utf-8",
        )
        manifest = migration.load_manifest(manifest_path, repository)
        generated = migration.generate_manifest()
        vscode_rows = [
            row
            for row in generated["rows"]
            if isinstance(row, dict)
            and isinstance(row.get("origin"), dict)
            and row["origin"].get("url") in migration.VSCODE_URLS.values()
        ]
        ok = _check(
            "raw GitHub VS Code declarations use text/plain",
            len(vscode_rows) == len(migration.VSCODE_URLS)
            and all(row["mime"] == "text/plain" for row in vscode_rows),
        )
        missing_import_path = repository / "missing-import.json"
        missing_import_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "rows": manifest.rows,
                    "imports": {"0": "mirror-is-absent.md"},
                }
            ),
            encoding="utf-8",
        )
        missing_import = migration.load_manifest(missing_import_path, repository)
        ok = _check("missing legacy import falls back to origin", missing_import.imports == {})
        escaping_import_path = repository / "escaping-import.json"
        escaping_import_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "rows": manifest.rows,
                    "imports": {"0": "../legacy.md"},
                }
            ),
            encoding="utf-8",
        )
        try:
            migration.load_manifest(escaping_import_path, repository)
        except migration.ScoutDiagnosticsError as exc:
            escaping_codes = {diagnostic.code.value for diagnostic in exc.diagnostics}
        else:
            escaping_codes = set()
        ok &= _check("escaping legacy import is rejected", escaping_codes == {"LEGACY_MIGRATION_REQUIRED"})
        control = control_module.SourceControl(root, repository, fixture_config)
        original_transition_lock = control_module.transition_lock
        original_import_file = control.materializer.import_file
        transition_depth = {"value": 0}
        import_held = {"value": False}

        @contextlib.contextmanager
        def tracked_transition_lock(_resources_root):
            transition_depth["value"] += 1
            try:
                yield
            finally:
                transition_depth["value"] -= 1

        def import_while_locked(*args, **kwargs):
            import_held["value"] = transition_depth["value"] > 0
            return original_import_file(*args, **kwargs)

        control_module.transition_lock = tracked_transition_lock
        control.materializer.import_file = import_while_locked
        try:
            result = control.bootstrap_import(manifest.rows, manifest.imports)
        finally:
            control_module.transition_lock = original_transition_lock
            control.materializer.import_file = original_import_file
        publication = control.publications.validate_current()
        snapshot = publication.records["migrated-source"].snapshot
        ok = _check(
            "legacy import activates declared remote source",
            result.publication_id is not None
            and snapshot is not None
            and snapshot.origin_evidence["kind"] == "legacy-import"
            and snapshot.origin_evidence["declared_origin"] == {
                "kind": "https",
                "url": "https://example.test/migrated.md",
            }
            and import_held["value"],
        )
        failed_result = control_module.BatchResult(
            outcomes=(control_module.RowOutcome(row=0, status="failed"),)
        )
        original_bootstrap = migration.bootstrap
        migration.bootstrap = lambda _path: failed_result
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                failed_exit = migration.main(["bootstrap", "--manifest", str(manifest_path)])
        finally:
            migration.bootstrap = original_bootstrap
        ok &= _check("failed migration exits nonzero", failed_exit == 1)
    return ok
