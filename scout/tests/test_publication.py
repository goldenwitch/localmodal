from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_publication() -> bool:
    print("source publication:")
    import copy
    from dataclasses import replace
    import tempfile
    from pathlib import Path

    config = _resource_module("config")
    materializer_module = _resource_module("materializer")
    model = _resource_module("source_model")
    publication_module = _resource_module("publication")
    source_index = _resource_module("source_index")
    fixture_config = config.ScoutConfig(
        schema_version=1,
        ledger=config.LedgerConfig(lock_wait_seconds=1, lock_poll_milliseconds=10),
        fetch=config.FetchConfig(request_timeout_seconds=1, max_redirects=0, max_response_bytes=1024),
        repo_files=config.RepoFileConfig(publishable_paths=("fixture.md", "second.md", "plan.vine")),
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        repository = root / "repo"
        repository.mkdir()
        (repository / "fixture.md").write_text("publication fixture text", encoding="utf-8")
        (repository / "second.md").write_text("second publication fixture text", encoding="utf-8")
        (repository / "plan.vine").write_text(
            "vine 1.2.0\n---\n[root] Publication VINE fixture (planning)\ntext\n",
            encoding="utf-8",
        )
        declaration = model.parse_declaration(
            {
                "name": "publication-fixture",
                "origin": {"kind": "repo-file", "path": "fixture.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            }
        )
        materializer = materializer_module.Materializer(root, repository, fixture_config)
        staged = materializer.materialize(declaration)
        materializer_module.commit_candidate(staged, root)
        record = model.SourceRecord(declaration=declaration, snapshot=staged.snapshot)
        second_declaration = model.parse_declaration(
            {
                "name": "second-publication-fixture",
                "origin": {"kind": "repo-file", "path": "second.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            }
        )
        second_staged = materializer.materialize(second_declaration)
        materializer_module.commit_candidate(second_staged, root)
        second_record = model.SourceRecord(declaration=second_declaration, snapshot=second_staged.snapshot)
        records = {declaration.name: record, second_declaration.name: second_record}
        generation = source_index.build_generation(root, records)
        store = publication_module.PublicationStore(root, fixture_config)
        candidate = store.create_candidate(records, generation, parent_id=None)
        ok = _check("publication candidate stays private", store.current_id() is None)
        ok &= _check(
            "master pointer activates candidate",
            store.activate(candidate, expected_parent=None) and store.activation_path.is_file(),
        )
        loaded = store.validate_current()
        ok &= _check(
            "publication binds source and index",
            loaded.records[declaration.name].snapshot.snapshot_id == staged.snapshot.snapshot_id
            and loaded.index.generation_id == generation.generation_id,
        )
        revoked_config = replace(
            fixture_config,
            repo_files=config.RepoFileConfig(publishable_paths=("second.md", "plan.vine")),
        )
        revoked_store = publication_module.PublicationStore(root, revoked_config)
        try:
            revoked_store.validate_current()
        except publication_module.ScoutDiagnosticsError as exc:
            revoked_sources = {
                diagnostic.evidence["source"]
                for diagnostic in exc.diagnostics
                if diagnostic.code.value == "SOURCE_BINDING_FAILED"
            }
        else:
            revoked_sources = set()
        try:
            store._validate_records(
                {
                    declaration.name: model.SourceRecord(
                        declaration,
                        replace(staged.snapshot, observed_mime="text/plain"),
                    )
                }
            )
        except publication_module.ScoutDiagnosticsError as exc:
            mime_sources = {
                diagnostic.evidence["source"]
                for diagnostic in exc.diagnostics
                if diagnostic.code.value == "SOURCE_BINDING_FAILED"
            }
        else:
            mime_sources = set()
        ok &= _check(
            "current validation binds allowlist and observed MIME",
            revoked_sources == {declaration.name} and mime_sources == {declaration.name},
        )
        stale = store.create_candidate(records, generation, parent_id=candidate.publication_id)
        ok &= _check(
            "stale publication cannot activate",
            not store.activate(stale, expected_parent="different-parent"),
        )
        store.current_path.write_text("../../escaped\n", encoding="utf-8")
        try:
            store.validate_current()
        except publication_module.ScoutDiagnosticsError as exc:
            escaped_current_codes = {diagnostic.code.value for diagnostic in exc.diagnostics}
        else:
            escaped_current_codes = set()
        try:
            store.load("../../escaped")
        except publication_module.ScoutDiagnosticsError as exc:
            escaped_load_codes = {diagnostic.code.value for diagnostic in exc.diagnostics}
        else:
            escaped_load_codes = set()
        store.current_path.write_text(candidate.publication_id + "\n", encoding="utf-8")
        ok &= _check(
            "escaped publication pointer is malformed",
            escaped_current_codes == {"PUBLICATION_MALFORMED"}
            and escaped_load_codes == {"PUBLICATION_MALFORMED"},
        )
        invalid_parent_payload = copy.deepcopy(store._to_json(candidate))
        invalid_parent_payload["parent_id"] = "not-a-publication-id"
        invalid_index_payload = copy.deepcopy(store._to_json(candidate))
        invalid_index_payload["index"]["relative_path"] = "\\outside-index"
        try:
            store._from_json(invalid_parent_payload, root / "invalid-parent.json", candidate.publication_id)
        except publication_module.ScoutDiagnosticsError as exc:
            invalid_parent_codes = {diagnostic.code.value for diagnostic in exc.diagnostics}
        else:
            invalid_parent_codes = set()
        try:
            store._from_json(invalid_index_payload, root / "invalid-index.json", candidate.publication_id)
        except publication_module.ScoutDiagnosticsError as exc:
            invalid_index_codes = {diagnostic.code.value for diagnostic in exc.diagnostics}
        else:
            invalid_index_codes = set()
        escaped_generation = source_index.IndexGeneration(
            generation_id=generation.generation_id,
            relative_path="\\outside-index",
            sha256=generation.sha256,
            chunk_count=generation.chunk_count,
        )
        try:
            source_index.validate_generation(root, escaped_generation)
        except source_index.ScoutDiagnosticsError as exc:
            escaped_index_codes = {diagnostic.code.value for diagnostic in exc.diagnostics}
        else:
            escaped_index_codes = set()
        ok &= _check(
            "publication parent and index paths are canonical",
            invalid_parent_codes == {"PUBLICATION_MALFORMED"}
            and invalid_index_codes == {"PUBLICATION_MALFORMED"}
            and escaped_index_codes == {"INDEX_INTEGRITY_FAILED"},
        )
        vine_declaration_one = model.parse_declaration(
            {
                "name": "publication-vine-one",
                "origin": {"kind": "repo-file", "path": "plan.vine"},
                "mime": "text/plain",
                "ttl_days": None,
            }
        )
        vine_declaration_two = model.parse_declaration(
            {
                "name": "publication-vine-two",
                "origin": {"kind": "repo-file", "path": "plan.vine"},
                "mime": "text/plain",
                "ttl_days": None,
            }
        )
        vine_staged_one = materializer.materialize(vine_declaration_one)
        vine_staged_two = materializer.materialize(vine_declaration_two)
        materializer_module.commit_candidate(vine_staged_one, root)
        materializer_module.commit_candidate(vine_staged_two, root)
        try:
            store._validate_records(
                {
                    vine_declaration_one.name: model.SourceRecord(vine_declaration_one, vine_staged_one.snapshot),
                    vine_declaration_two.name: model.SourceRecord(vine_declaration_two, vine_staged_two.snapshot),
                }
            )
        except publication_module.ScoutDiagnosticsError as exc:
            duplicate_vine_sources = {
                diagnostic.evidence["source"]
                for diagnostic in exc.diagnostics
                if diagnostic.code.value == "SOURCE_BINDING_FAILED"
            }
        else:
            duplicate_vine_sources = set()
        ok &= _check(
            "duplicate live VINE records invalidate publication",
            duplicate_vine_sources == {vine_declaration_one.name, vine_declaration_two.name},
        )
        content = root / staged.snapshot.artifact_path
        content.write_text("tampered", encoding="utf-8")
        second_content = root / second_staged.snapshot.artifact_path
        second_content.write_text("also tampered", encoding="utf-8")
        (root / generation.relative_path / "manifest.json").write_text("{}\n", encoding="utf-8")
        try:
            store.validate_current()
        except publication_module.ScoutDiagnosticsError as exc:
            diagnostics = exc.diagnostics
        else:
            diagnostics = ()
        binding_sources = {
            diagnostic.evidence["source"]
            for diagnostic in diagnostics
            if diagnostic.code.value == "SOURCE_BINDING_FAILED"
        }
        codes = {diagnostic.code.value for diagnostic in diagnostics}
        ok &= _check(
            "publication aggregates independent integrity faults",
            binding_sources == {declaration.name, second_declaration.name}
            and "INDEX_INTEGRITY_FAILED" in codes,
        )
    return ok
