from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_source_control() -> bool:
    print("source control:")
    from datetime import datetime, timedelta, timezone
    import json
    import tempfile
    from pathlib import Path

    config = _resource_module("config")
    control_module = _resource_module("source_control")
    fixture_config = config.ScoutConfig(
        schema_version=1,
        ledger=config.LedgerConfig(lock_wait_seconds=1, lock_poll_milliseconds=10),
        fetch=config.FetchConfig(request_timeout_seconds=1, max_redirects=0, max_response_bytes=1024),
        repo_files=config.RepoFileConfig(
            publishable_paths=(
                "first.md",
                "second.md",
                "plan.vine",
                "recovery.md",
                "candidate.md",
                "old.md",
                "new.md",
                "rebase-base.md",
                "rebase-a.md",
                "rebase-b.md",
                "prepared.md",
                "prepared-new.md",
                "marker.md",
                "invalid-prepared.md",
                "partial-first.md",
                "partial-second.md",
                "empty.md",
                "cleanup-first.md",
                "cleanup-second.md",
                "staging-failure.md",
                "invalid.vine",
            )
        ),
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        repository = root / "repo"
        repository.mkdir()
        (repository / "first.md").write_text("first publication text", encoding="utf-8")
        (repository / "second.md").write_text("second publication text", encoding="utf-8")
        (repository / "plan.vine").write_text(
            "vine 1.2.0\n---\n[root] Root source task (planning)\nsource bound VINE fixture\n",
            encoding="utf-8",
        )
        control = control_module.SourceControl(root, repository, fixture_config)
        oversized_citation = root / "oversized-citation.txt"
        oversized_citation.write_text("x" * (control_module.SourceControl.CITATION_TEXT_LIMIT + 50), encoding="utf-8")
        capped_citation = control_module.SourceControl._read_citation_text(oversized_citation)
        ok = _check(
            "citation payload is bounded before worker serialization",
            len(capped_citation) == control_module.SourceControl.CITATION_TEXT_LIMIT + len("\n[truncated]")
            and capped_citation.endswith("[truncated]"),
        )
        empty_root = root / "empty-source"
        empty_repository = empty_root / "repo"
        empty_repository.mkdir(parents=True)
        (empty_repository / "empty.md").write_text("", encoding="utf-8")
        empty_control = control_module.SourceControl(empty_root, empty_repository, fixture_config)
        empty_result = empty_control.bootstrap(
            [
                {
                    "op": "add",
                    "name": "empty-source",
                    "origin": {"kind": "repo-file", "path": "empty.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                }
            ]
        )
        empty_search = empty_control.search("anything")
        ok &= _check(
            "empty source publishes a valid zero-hit generation",
            empty_result.succeeded
            and empty_control.publications.validate_current().index.chunk_count == 0
            and empty_search["hits"] == []
            and empty_search["diagnostics"] == []
            and not empty_control.ledger.journal_path.exists(),
        )
        legacy_root = root / "legacy-ledger-bootstrap"
        legacy_repository = legacy_root / "repo"
        legacy_repository.mkdir(parents=True)
        (legacy_repository / "first.md").write_text("legacy migration source", encoding="utf-8")
        legacy_freshness = {"legacy": {"fetched": "2026-01-01"}}
        (legacy_root / "sources.json").write_text(json.dumps(legacy_freshness), encoding="utf-8")
        legacy_control = control_module.SourceControl(legacy_root, legacy_repository, fixture_config)
        legacy_result = legacy_control.bootstrap(
            [
                {
                    "op": "add",
                    "name": "legacy-bootstrap-source",
                    "origin": {"kind": "repo-file", "path": "first.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                }
            ]
        )
        legacy_runtime_ledger = json.loads((legacy_root / ".scout-ledger.json").read_text(encoding="utf-8"))
        preserved_freshness = json.loads((legacy_root / "sources.json").read_text(encoding="utf-8"))
        ok &= _check(
            "initial bootstrap preserves legacy freshness and writes runtime ledger",
            legacy_result.succeeded
            and legacy_runtime_ledger.get("schema_version") == 1
            and "legacy-bootstrap-source" in legacy_runtime_ledger.get("sources", {})
            and preserved_freshness == legacy_freshness,
        )
        io_root = root / "typed-io-failure"
        io_repository = io_root / "repo"
        io_repository.mkdir(parents=True)
        (io_repository / "first.md").write_text("typed I/O source", encoding="utf-8")
        io_control = control_module.SourceControl(io_root, io_repository, fixture_config)
        io_rows = [
            {
                "op": "add",
                "name": "io-source",
                "origin": {"kind": "repo-file", "path": "first.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            }
        ]
        assert io_control.bootstrap(io_rows).succeeded

        class OSErrorMaterializer:
            def materialize(self, _declaration, **_kwargs):
                raise OSError("fixture disk full")

        io_control.materializer = OSErrorMaterializer()
        io_result = io_control.propose(io_rows)
        worker_module = _resource_module("source_worker")
        io_worker_result = worker_module._reply(io_control, {"op": "propose", "rows": io_rows})
        ok &= _check(
            "materialization I/O failure remains typed through worker",
            [outcome.status for outcome in io_result.outcomes] == ["failed"]
            and {item.code.value for item in io_result.outcomes[0].diagnostics} == {"MATERIALIZATION_FAILED"}
            and "error" not in io_worker_result
            and io_worker_result["outcomes"][0]["diagnostics"][0]["code"] == "MATERIALIZATION_FAILED",
        )
        cache_root = root / "resident-index-cache"
        cache_repository = cache_root / "repo"
        cache_repository.mkdir(parents=True)
        (cache_repository / "first.md").write_text("resident query cache source", encoding="utf-8")
        cache_control = control_module.SourceControl(cache_root, cache_repository, fixture_config)
        cache_rows = [
            {
                "op": "add",
                "name": "cache-source",
                "origin": {"kind": "repo-file", "path": "first.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            }
        ]
        assert cache_control.bootstrap(cache_rows).succeeded
        original_open_validated = control_module.open_validated_generation
        opened_generations = {"count": 0}

        def count_opened_generation(*args, **kwargs):
            opened_generations["count"] += 1
            return original_open_validated(*args, **kwargs)

        control_module.open_validated_generation = count_opened_generation
        try:
            cache_first = cache_control.search("resident query cache")
            cache_second = cache_control.search("resident query cache")
        finally:
            control_module.open_validated_generation = original_open_validated
            cache_control.close()
        ok &= _check(
            "resident worker reuses one validated index per publication",
            opened_generations["count"] == 1
            and bool(cache_first["hits"])
            and bool(cache_first["keyword_hits"])
            and bool(cache_second["hits"])
            and bool(cache_second["keyword_hits"]),
        )
        cleanup_root = root / "batch-cleanup"
        cleanup_repository = cleanup_root / "repo"
        cleanup_repository.mkdir(parents=True)
        (cleanup_repository / "cleanup-first.md").write_text("first artifact", encoding="utf-8")
        (cleanup_repository / "cleanup-second.md").write_text("second artifact", encoding="utf-8")
        cleanup_control = control_module.SourceControl(cleanup_root, cleanup_repository, fixture_config)
        cleanup_materializer = cleanup_control.materializer

        class FailingSecondMaterializer:
            def materialize(self, declaration, **kwargs):
                if declaration.name == "cleanup-second":
                    raise control_module.ScoutDiagnosticsError(
                        (
                            control_module.diagnostic(
                                control_module.DiagnosticCode.MATERIALIZATION_FAILED,
                                source=declaration.name,
                                detail="fixture",
                            ),
                        )
                    )
                return cleanup_materializer.materialize(declaration, **kwargs)

        cleanup_control.materializer = FailingSecondMaterializer()
        cleanup_result = cleanup_control.bootstrap(
            [
                {
                    "op": "add",
                    "name": "cleanup-first",
                    "origin": {"kind": "repo-file", "path": "cleanup-first.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                },
                {
                    "op": "add",
                    "name": "cleanup-second",
                    "origin": {"kind": "repo-file", "path": "cleanup-second.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                },
            ]
        )
        cleanup_generations = cleanup_root / "scout-source--cleanup-first" / "generations"
        cleanup_absent = cleanup_control.ledger.read().records.get("cleanup-second")
        ok &= _check(
            "failed batch removes earlier artifact and retains failed source absent",
            [outcome.status for outcome in cleanup_result.outcomes] == ["not_committed", "failed"]
            and cleanup_control.publications.current_id() is None
            and (not cleanup_generations.exists() or not any(cleanup_generations.iterdir()))
            and not cleanup_control.ledger.journal_path.exists()
            and cleanup_absent is not None
            and cleanup_absent.snapshot is None,
        )
        staging_failure_root = root / "staging-failure"
        staging_failure_repository = staging_failure_root / "repo"
        staging_failure_repository.mkdir(parents=True)
        (staging_failure_repository / "staging-failure.md").write_text(
            "staging failure source", encoding="utf-8"
        )
        staging_failure_control = control_module.SourceControl(
            staging_failure_root,
            staging_failure_repository,
            fixture_config,
        )
        original_staging_commit = control_module.commit_candidate

        def fail_staging_commit(_candidate, _resources_root):
            raise OSError("fixture immutable move failure")

        control_module.commit_candidate = fail_staging_commit
        try:
            staging_failure_result = staging_failure_control.bootstrap(
                [
                    {
                        "op": "add",
                        "name": "staging-failure-source",
                        "origin": {"kind": "repo-file", "path": "staging-failure.md"},
                        "mime": "text/markdown",
                        "ttl_days": None,
                    }
                ]
            )
        finally:
            control_module.commit_candidate = original_staging_commit
        staging_payloads = list(
            (staging_failure_root / "scout-source--staging-failure-source" / "staging").glob("**/content")
        )
        ok &= _check(
            "failed immutable move cleans operation staging",
            [outcome.status for outcome in staging_failure_result.outcomes] == ["failed"]
            and not staging_payloads
            and not staging_failure_control.ledger.journal_path.exists(),
        )
        terminal_root = root / "terminal-index-failure"
        terminal_repository = terminal_root / "repo"
        terminal_repository.mkdir(parents=True)
        invalid_vine = terminal_repository / "invalid.vine"
        invalid_vine.write_text("not a VINE file\n", encoding="utf-8")
        terminal_control = control_module.SourceControl(terminal_root, terminal_repository, fixture_config)
        terminal_rows = [
            {
                "op": "add",
                "name": "terminal-vine",
                "origin": {"kind": "repo-file", "path": "invalid.vine"},
                "mime": "text/plain",
                "ttl_days": None,
            }
        ]
        terminal_failure = terminal_control.bootstrap(terminal_rows)
        invalid_vine.write_text(
            "vine 1.2.0\n---\n[root] Repaired VINE source (planning)\ntext\n",
            encoding="utf-8",
        )
        terminal_repaired = terminal_control.bootstrap(terminal_rows)
        ok &= _check(
            "post-staging index failure cleans journal and private artifact",
            [outcome.status for outcome in terminal_failure.outcomes] == ["not_committed"]
            and not terminal_control.ledger.journal_path.exists()
            and terminal_repaired.succeeded,
        )
        initial = control.bootstrap(
            [
                {
                    "op": "add",
                    "name": "first-source",
                    "origin": {"kind": "repo-file", "path": "first.md"},
                    "mime": "text/markdown",
                    "ttl_days": 1,
                },
                {
                    "op": "add",
                    "name": "plan-source",
                    "origin": {"kind": "repo-file", "path": "plan.vine"},
                    "mime": "text/plain",
                    "ttl_days": None,
                }
            ]
        )
        ok &= _check(
            "bootstrap publishes one source",
            initial.publication_id is not None and [outcome.status for outcome in initial.outcomes] == ["published", "published"],
        )
        repeated_bootstrap = control.bootstrap(
            [
                {
                    "op": "add",
                    "name": "repeated-bootstrap",
                    "origin": {"kind": "repo-file", "path": "first.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                }
            ]
        )
        ok &= _check(
            "bootstrap is disabled after activation",
            {item.code.value for item in repeated_bootstrap.diagnostics} == {"BOOTSTRAP_AFTER_ACTIVATION"}
            and "repeated-bootstrap" not in control.publications.validate_current().records,
        )
        first_record = control.publications.validate_current().records["first-source"]
        assert first_record.snapshot is not None
        materialized_at = datetime.fromisoformat(first_record.snapshot.materialized_at.replace("Z", "+00:00"))
        stale_warnings = control.attempts.warnings_for(
            {"first-source": first_record},
            materialized_at + timedelta(days=1, hours=2),
        )
        ok &= _check(
            "TTL overrun warns without another full day",
            any(
                item.code.value == "SNAPSHOT_STALE" and item.evidence["overdue_days"] == 1
                for item in stale_warnings
            ),
        )
        recovery_root = root / "bootstrap-recovery"
        recovery_repository = recovery_root / "repo"
        recovery_repository.mkdir(parents=True)
        (recovery_repository / "recovery.md").write_text("recovered initial source", encoding="utf-8")
        recovery_control = control_module.SourceControl(recovery_root, recovery_repository, fixture_config)
        recovery_rows = [
            {
                "op": "add",
                "name": "recovered-source",
                "origin": {"kind": "repo-file", "path": "recovery.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            }
        ]
        recovery_control.ledger.begin(
            {"kind": "batch", "bootstrap": True, "rows": recovery_rows}
        )
        recovered_result = recovery_control.bootstrap(recovery_rows)
        recovered = recovery_control.publications.validate_current()
        ok &= _check(
            "interrupted bootstrap recovery returns its publication result",
            recovered_result.succeeded
            and [outcome.status for outcome in recovered_result.outcomes] == ["published"]
            and set(recovered.records) == {"recovered-source"},
        )
        marker_root = root / "marker-write-recovery"
        marker_repository = marker_root / "repo"
        marker_repository.mkdir(parents=True)
        (marker_repository / "marker.md").write_text("marker recovery source", encoding="utf-8")
        marker_control = control_module.SourceControl(marker_root, marker_repository, fixture_config)
        marker_rows = [
            {
                "op": "add",
                "name": "marker-source",
                "origin": {"kind": "repo-file", "path": "marker.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            }
        ]
        original_marker_activate = marker_control.publications.activate
        marker_failed = {"value": False}

        def fail_after_marker(publication, *, expected_parent):
            if not marker_failed["value"]:
                marker_failed["value"] = True
                marker_control.publications.root.mkdir(parents=True, exist_ok=True)
                marker_control.publications._atomic_text(
                    marker_control.publications.activation_path,
                    "source-control-v1\n",
                )
                raise control_module.ScoutDiagnosticsError(
                    (
                        control_module.diagnostic(
                            control_module.DiagnosticCode.PUBLICATION_INTEGRITY_FAILED,
                            publication_id=publication.publication_id,
                            detail="fixture CURRENT replacement failure",
                        ),
                    )
                )
            return original_marker_activate(publication, expected_parent=expected_parent)

        marker_control.publications.activate = fail_after_marker
        try:
            marker_failure = marker_control.bootstrap(marker_rows)
        finally:
            marker_control.publications.activate = original_marker_activate
        marker_recovered = marker_control.bootstrap(marker_rows)
        ok &= _check(
            "marker-first activation failure retains recoverable bootstrap",
            marker_failed["value"]
            and {item.code.value for item in marker_failure.diagnostics} == {"PUBLICATION_INTEGRITY_FAILED"}
            and marker_recovered.succeeded
            and marker_control.publications.current_id() == marker_recovered.publication_id
            and not marker_control.ledger.journal_path.exists(),
        )
        invalid_candidate_root = root / "invalid-candidate-recovery"
        invalid_candidate_repository = invalid_candidate_root / "repo"
        invalid_candidate_repository.mkdir(parents=True)
        (invalid_candidate_repository / "candidate.md").write_text(
            "candidate recovery source", encoding="utf-8"
        )
        invalid_candidate_control = control_module.SourceControl(
            invalid_candidate_root,
            invalid_candidate_repository,
            fixture_config,
        )
        invalid_candidate_rows = [
            {
                "op": "add",
                "name": "candidate-source",
                "origin": {"kind": "repo-file", "path": "candidate.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            }
        ]
        parsed_candidate_rows, parse_failures = invalid_candidate_control._parse_rows(invalid_candidate_rows)
        assert not parse_failures
        staged_declaration = parsed_candidate_rows[0].row.declaration
        staged_journal = invalid_candidate_control.ledger.begin(
            {"kind": "batch", "bootstrap": True, "rows": invalid_candidate_rows}
        )
        staged_lease = invalid_candidate_control.ledger.claim_recovery()
        assert staged_lease is not None
        staged_candidate = invalid_candidate_control.materializer.materialize(staged_declaration)
        control_module.commit_candidate(staged_candidate, invalid_candidate_root)
        staged_lease.update(
            "staged",
            candidate={
                "snapshots": {
                    "candidate-source": control_module.snapshot_to_json(staged_candidate.snapshot),
                }
            },
        )
        (invalid_candidate_root / staged_candidate.snapshot.artifact_path).unlink()
        staged_lease.__exit__(None, None, None)
        invalid_candidate_control._recover_if_needed()
        recovered_candidate = invalid_candidate_control.publications.validate_current().records["candidate-source"]
        ok &= _check(
            "missing staged artifact is rematerialized on recovery",
            recovered_candidate.snapshot is not None
            and recovered_candidate.snapshot.snapshot_id != staged_candidate.snapshot.snapshot_id
            and not invalid_candidate_control.ledger.journal_path.exists(),
        )
        partial_root = root / "partial-candidate-recovery"
        partial_repository = partial_root / "repo"
        partial_repository.mkdir(parents=True)
        (partial_repository / "partial-first.md").write_text("partial first", encoding="utf-8")
        (partial_repository / "partial-second.md").write_text("partial second", encoding="utf-8")
        partial_control = control_module.SourceControl(partial_root, partial_repository, fixture_config)
        partial_rows = [
            {
                "op": "add",
                "name": "partial-first",
                "origin": {"kind": "repo-file", "path": "partial-first.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            },
            {
                "op": "add",
                "name": "partial-second",
                "origin": {"kind": "repo-file", "path": "partial-second.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            },
        ]
        original_commit_candidate = control_module.commit_candidate
        committed_partial = {"count": 0}

        def interrupt_after_first_commit(candidate, resources_root):
            committed = original_commit_candidate(candidate, resources_root)
            committed_partial["count"] += 1
            if committed_partial["count"] == 1:
                raise SystemExit("fixture interruption after first artifact commit")
            return committed

        control_module.commit_candidate = interrupt_after_first_commit
        try:
            partial_control.bootstrap(partial_rows)
        except SystemExit:
            pass
        finally:
            control_module.commit_candidate = original_commit_candidate
        partial_control._recover_if_needed()
        partial_records = partial_control.publications.validate_current().records
        partial_first_artifacts = list(
            (partial_root / "scout-source--partial-first" / "generations").glob("*/content")
        )
        ok &= _check(
            "partial candidate recovery reclaims first interrupted artifact",
            committed_partial["count"] >= 1
            and set(partial_records) == {"partial-first", "partial-second"}
            and len(partial_first_artifacts) == 1
            and not partial_control.ledger.journal_path.exists(),
        )
        prepared_root = root / "prepared-publication-recovery"
        prepared_repository = prepared_root / "repo"
        prepared_repository.mkdir(parents=True)
        (prepared_repository / "prepared.md").write_text("prepared recovery source", encoding="utf-8")
        prepared_new_path = prepared_repository / "prepared-new.md"
        prepared_new_path.write_text("prepared recovery replacement", encoding="utf-8")
        prepared_control = control_module.SourceControl(prepared_root, prepared_repository, fixture_config)
        prepared_control.bootstrap(
            [
                {
                    "op": "add",
                    "name": "prepared-base",
                    "origin": {"kind": "repo-file", "path": "prepared.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                }
            ]
        )
        prepared_rows = [
            {
                "op": "add",
                "name": "prepared-new",
                "origin": {"kind": "repo-file", "path": "prepared-new.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            }
        ]
        prepared_parsed, prepared_parse_failures = prepared_control._parse_and_preflight_inputs(prepared_rows)
        assert not prepared_parse_failures
        prepared_journal = prepared_control.ledger.begin(
            {"kind": "batch", "bootstrap": False, "rows": prepared_rows}
        )
        prepared_lease = prepared_control.ledger.claim_recovery()
        assert prepared_lease is not None
        prepared_records, prepared_parent = prepared_control._base(False, recovery_lease=prepared_lease)
        prepared_registry = prepared_control._registry_records(
            prepared_records,
            recovery_lease=prepared_lease,
        )
        prepared_outcomes = prepared_control._preflight(prepared_registry, prepared_parsed)
        prepared_snapshots = {}
        prepared_control._materialize_adds(
            prepared_lease,
            prepared_parsed,
            {},
            prepared_snapshots,
        )
        prepared_next_records = control_module.SourceControl._apply_rows(
            prepared_records,
            prepared_parsed,
            prepared_snapshots,
            prepared_journal.operation_id,
            prepared_outcomes,
        )
        prepared_generation = control_module.build_generation(prepared_root, prepared_next_records)
        prepared_publication = prepared_control.publications.create_candidate(
            prepared_next_records,
            prepared_generation,
            parent_id=prepared_parent,
        )
        prepared_result = prepared_control._publication_result(
            prepared_parsed,
            prepared_outcomes,
            prepared_journal.operation_id,
            prepared_publication.publication_id,
        )
        prepared_lease.update(
            "staged",
            candidate=prepared_control._prepared_candidate(
                prepared_snapshots,
                prepared_publication,
                prepared_result,
            ),
        )
        assert prepared_control.publications.activate(
            prepared_publication,
            expected_parent=prepared_parent,
        )
        prepared_new_path.unlink()
        prepared_lease.__exit__(None, None, None)
        prepared_recovery = prepared_control._recover_if_needed()
        prepared_current = prepared_control.publications.validate_current()
        ok &= _check(
            "prepared current publication recovers without replaying mutation",
            prepared_recovery is not None
            and [outcome.status for outcome in prepared_recovery.result.outcomes] == ["published"]
            and prepared_recovery.result.publication_id == prepared_publication.publication_id
            and prepared_current.publication_id == prepared_publication.publication_id
            and "prepared-new" in prepared_current.records
            and not prepared_control.ledger.journal_path.exists(),
        )
        invalid_prepared_root = root / "invalid-prepared-manifest"
        invalid_prepared_repository = invalid_prepared_root / "repo"
        invalid_prepared_repository.mkdir(parents=True)
        (invalid_prepared_repository / "prepared.md").write_text("prepared base", encoding="utf-8")
        (invalid_prepared_repository / "invalid-prepared.md").write_text("prepared replacement", encoding="utf-8")
        invalid_prepared_control = control_module.SourceControl(
            invalid_prepared_root,
            invalid_prepared_repository,
            fixture_config,
        )
        invalid_prepared_control.bootstrap(
            [
                {
                    "op": "add",
                    "name": "invalid-prepared-base",
                    "origin": {"kind": "repo-file", "path": "prepared.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                }
            ]
        )
        invalid_prepared_rows = [
            {
                "op": "add",
                "name": "invalid-prepared-source",
                "origin": {"kind": "repo-file", "path": "invalid-prepared.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            }
        ]
        invalid_prepared_parsed, invalid_prepared_failures = invalid_prepared_control._parse_and_preflight_inputs(
            invalid_prepared_rows
        )
        assert not invalid_prepared_failures
        invalid_prepared_journal = invalid_prepared_control.ledger.begin(
            {"kind": "batch", "bootstrap": False, "rows": invalid_prepared_rows}
        )
        invalid_prepared_lease = invalid_prepared_control.ledger.claim_recovery()
        assert invalid_prepared_lease is not None
        invalid_prepared_records, invalid_prepared_parent = invalid_prepared_control._base(
            False,
            recovery_lease=invalid_prepared_lease,
        )
        invalid_prepared_registry = invalid_prepared_control._registry_records(
            invalid_prepared_records,
            recovery_lease=invalid_prepared_lease,
        )
        invalid_prepared_outcomes = invalid_prepared_control._preflight(
            invalid_prepared_registry,
            invalid_prepared_parsed,
        )
        invalid_prepared_snapshots = {}
        invalid_prepared_control._materialize_adds(
            invalid_prepared_lease,
            invalid_prepared_parsed,
            {},
            invalid_prepared_snapshots,
        )
        invalid_prepared_next = control_module.SourceControl._apply_rows(
            invalid_prepared_records,
            invalid_prepared_parsed,
            invalid_prepared_snapshots,
            invalid_prepared_journal.operation_id,
            invalid_prepared_outcomes,
        )
        invalid_prepared_generation = control_module.build_generation(
            invalid_prepared_root,
            invalid_prepared_next,
        )
        invalid_prepared_publication = invalid_prepared_control.publications.create_candidate(
            invalid_prepared_next,
            invalid_prepared_generation,
            parent_id=invalid_prepared_parent,
        )
        invalid_prepared_result = invalid_prepared_control._publication_result(
            invalid_prepared_parsed,
            invalid_prepared_outcomes,
            invalid_prepared_journal.operation_id,
            invalid_prepared_publication.publication_id,
        )
        invalid_prepared_lease.update(
            "staged",
            candidate=invalid_prepared_control._prepared_candidate(
                invalid_prepared_snapshots,
                invalid_prepared_publication,
                invalid_prepared_result,
            ),
        )
        __import__("shutil").rmtree(
            invalid_prepared_control.publications.generations / invalid_prepared_publication.publication_id,
        )
        invalid_prepared_lease.__exit__(None, None, None)
        invalid_prepared_recovery = invalid_prepared_control._recover_if_needed()
        invalid_prepared_current = invalid_prepared_control.publications.validate_current()
        invalid_prepared_index_path = (
            invalid_prepared_root
            / ".scout-index"
            / "generations"
            / invalid_prepared_generation.generation_id
        )
        ok &= _check(
            "missing private prepared manifest recovers by rebase",
            invalid_prepared_recovery is not None
            and invalid_prepared_recovery.result.succeeded
            and "invalid-prepared-source" in invalid_prepared_current.records
            and invalid_prepared_current.publication_id != invalid_prepared_publication.publication_id
            and not invalid_prepared_index_path.exists()
            and not invalid_prepared_control.ledger.journal_path.exists(),
        )
        refresh_race_root = root / "refresh-race"
        refresh_race_repository = refresh_race_root / "repo"
        refresh_race_repository.mkdir(parents=True)
        (refresh_race_repository / "old.md").write_text("old refresh source", encoding="utf-8")
        (refresh_race_repository / "new.md").write_text("new explicit source", encoding="utf-8")
        refresh_race_control = control_module.SourceControl(
            refresh_race_root,
            refresh_race_repository,
            fixture_config,
        )
        refresh_race_control.bootstrap(
            [
                {
                    "op": "add",
                    "name": "race-source",
                    "origin": {"kind": "repo-file", "path": "old.md"},
                    "mime": "text/markdown",
                    "ttl_days": 1,
                }
            ]
        )
        original_begin_and_claim = refresh_race_control.ledger.begin_and_claim
        raced = {"value": False}

        def begin_after_upsert(mutation):
            if mutation.get("kind") == "refresh-stale" and not raced["value"]:
                raced["value"] = True
                refresh_race_control.ledger.begin_and_claim = original_begin_and_claim
                replacement = refresh_race_control.propose(
                    [
                        {
                            "op": "add",
                            "name": "race-source",
                            "origin": {"kind": "repo-file", "path": "new.md"},
                            "mime": "text/markdown",
                            "ttl_days": 1,
                        }
                    ]
                )
                assert replacement.succeeded
            return original_begin_and_claim(mutation)

        refresh_race_control.ledger.begin_and_claim = begin_after_upsert
        try:
            race_refresh = refresh_race_control.refresh_stale(
                datetime.now(timezone.utc) + timedelta(days=2)
            )
        finally:
            refresh_race_control.ledger.begin_and_claim = original_begin_and_claim
        race_record = refresh_race_control.publications.validate_current().records["race-source"]
        race_origin = getattr(race_record.declaration.origin, "path", None)
        ok &= _check(
            "refresh does not overwrite a concurrent explicit upsert",
            raced["value"]
            and [outcome.status for outcome in race_refresh.outcomes] == ["published"]
            and race_origin == "new.md",
        )
        rebase_root = root / "publication-rebase"
        rebase_repository = rebase_root / "repo"
        rebase_repository.mkdir(parents=True)
        (rebase_repository / "rebase-base.md").write_text("rebase base", encoding="utf-8")
        (rebase_repository / "rebase-a.md").write_text("rebase A", encoding="utf-8")
        (rebase_repository / "rebase-b.md").write_text("rebase B", encoding="utf-8")
        rebase_control = control_module.SourceControl(rebase_root, rebase_repository, fixture_config)
        rebase_control.bootstrap(
            [
                {
                    "op": "add",
                    "name": "rebase-base",
                    "origin": {"kind": "repo-file", "path": "rebase-base.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                }
            ]
        )
        rebase_parent = rebase_control.publications.validate_current()
        rebase_row = control_module.parse_row(
            {
                "op": "add",
                "name": "rebase-b",
                "origin": {"kind": "repo-file", "path": "rebase-b.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            },
            0,
        )
        assert isinstance(rebase_row, control_module.AddRow)
        rebase_b_candidate = rebase_control.materializer.materialize(rebase_row.declaration)
        control_module.commit_candidate(rebase_b_candidate, rebase_root)
        rebase_b_records = dict(rebase_parent.records)
        rebase_b_records["rebase-b"] = control_module.SourceRecord(
            rebase_row.declaration,
            rebase_b_candidate.snapshot,
        )
        rebase_b_generation = control_module.build_generation(rebase_root, rebase_b_records)
        competing_publication = rebase_control.publications.create_candidate(
            rebase_b_records,
            rebase_b_generation,
            parent_id=rebase_parent.publication_id,
        )
        original_activate = rebase_control.publications.activate
        competing_won = {"value": False}

        def activate_after_competitor(publication, *, expected_parent):
            if not competing_won["value"]:
                competing_won["value"] = True
                assert original_activate(competing_publication, expected_parent=rebase_parent.publication_id)
            return original_activate(publication, expected_parent=expected_parent)

        rebase_control.publications.activate = activate_after_competitor
        try:
            rebase_result = rebase_control.propose(
                [
                    {
                        "op": "add",
                        "name": "rebase-a",
                        "origin": {"kind": "repo-file", "path": "rebase-a.md"},
                        "mime": "text/markdown",
                        "ttl_days": None,
                    }
                ]
            )
        finally:
            rebase_control.publications.activate = original_activate
        rebase_records = rebase_control.publications.validate_current().records
        ok &= _check(
            "staged mutation rebases after another publication wins",
            competing_won["value"]
            and rebase_result.succeeded
            and set(rebase_records) == {"rebase-base", "rebase-a", "rebase-b"}
            and not rebase_control.ledger.journal_path.exists(),
        )
        remove_rebase_root = root / "notfound-remove-rebase"
        remove_rebase_repository = remove_rebase_root / "repo"
        remove_rebase_repository.mkdir(parents=True)
        (remove_rebase_repository / "rebase-base.md").write_text("remove rebase base", encoding="utf-8")
        (remove_rebase_repository / "rebase-b.md").write_text("late source", encoding="utf-8")
        remove_rebase_control = control_module.SourceControl(
            remove_rebase_root,
            remove_rebase_repository,
            fixture_config,
        )
        remove_rebase_control.bootstrap(
            [
                {
                    "op": "add",
                    "name": "remove-rebase-base",
                    "origin": {"kind": "repo-file", "path": "rebase-base.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                }
            ]
        )
        remove_rebase_parent = remove_rebase_control.publications.validate_current()
        late_row = control_module.parse_row(
            {
                "op": "add",
                "name": "late-source",
                "origin": {"kind": "repo-file", "path": "rebase-b.md"},
                "mime": "text/markdown",
                "ttl_days": None,
            },
            0,
        )
        assert isinstance(late_row, control_module.AddRow)
        late_candidate = remove_rebase_control.materializer.materialize(late_row.declaration)
        control_module.commit_candidate(late_candidate, remove_rebase_root)
        late_records = dict(remove_rebase_parent.records)
        late_records["late-source"] = control_module.SourceRecord(late_row.declaration, late_candidate.snapshot)
        late_generation = control_module.build_generation(remove_rebase_root, late_records)
        late_publication = remove_rebase_control.publications.create_candidate(
            late_records,
            late_generation,
            parent_id=remove_rebase_parent.publication_id,
        )
        original_remove_activate = remove_rebase_control.publications.activate
        late_won = {"value": False}

        def activate_late_source(publication, *, expected_parent):
            if not late_won["value"]:
                late_won["value"] = True
                assert original_remove_activate(
                    late_publication,
                    expected_parent=remove_rebase_parent.publication_id,
                )
            return original_remove_activate(publication, expected_parent=expected_parent)

        remove_rebase_control.publications.activate = activate_late_source
        try:
            notfound_remove = remove_rebase_control.propose([{"op": "remove", "name": "late-source"}])
        finally:
            remove_rebase_control.publications.activate = original_remove_activate
        notfound_records = remove_rebase_control.publications.validate_current().records
        ok &= _check(
            "not-found remove remains a no-op across publication rebase",
            late_won["value"]
            and [outcome.status for outcome in notfound_remove.outcomes] == ["not_found"]
            and "late-source" in notfound_records,
        )
        vine_search = control.search("source bound VINE fixture")
        vine_citation = next((hit["citation"] for hit in vine_search["hits"] if hit["citation"].endswith("#vine")), None)
        vine_read = control.read_citation(vine_citation) if isinstance(vine_citation, str) else {"diagnostics": ["missing"]}
        ok &= _check(
            "source-bound VINE citation resolves",
            vine_read.get("diagnostics") == [] and "Root source task" in vine_read.get("text", ""),
        )
        from dataclasses import replace

        plan_record = control.publications.validate_current().records["plan-source"]
        oversized_vine = root / "oversized.vine"
        oversized_vine.write_text(
            "vine 1.2.0\n---\n[root] Oversized VINE source (planning)\n"
            + "x" * (control_module.SourceControl.VINE_CITATION_PARSE_LIMIT + 1),
            encoding="utf-8",
        )
        oversized_vine_record = replace(
            plan_record,
            snapshot=replace(plan_record.snapshot, artifact_path="oversized.vine"),
        )
        oversized_vine_read = control._read_vine_citation(
            {"plan-source": oversized_vine_record},
            vine_citation,
        )
        ok &= _check(
            "oversized VINE citation is bounded before parsing",
            [item["code"] for item in oversized_vine_read["diagnostics"]] == ["SOURCE_BINDING_FAILED"],
        )
        vine_before_duplicate = control.publications.validate_current().publication_id
        duplicate_vine = control.propose(
            [
                {
                    "op": "add",
                    "name": "duplicate-vine-source",
                    "origin": {"kind": "repo-file", "path": "plan.vine"},
                    "mime": "text/plain",
                    "ttl_days": None,
                }
            ]
        )
        ok &= _check(
            "duplicate live VINE path is rejected before materialization",
            [outcome.status for outcome in duplicate_vine.outcomes] == ["rejected"]
            and control.publications.validate_current().publication_id == vine_before_duplicate,
        )
        normal = control.propose(
            [
                {
                    "op": "add",
                    "name": "second-source",
                    "origin": {"kind": "repo-file", "path": "second.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                },
                {"op": "remove", "name": "absent-source"},
            ]
        )
        ok &= _check(
            "batch publishes and reports not-found remove",
            [outcome.status for outcome in normal.outcomes] == ["published", "not_found"],
        )
        search = control.search("second publication")
        ok &= _check(
            "validated source search returns published hit",
            bool(search["hits"]) and search["diagnostics"] == [],
        )
        resolved = control.read_citation(search["hits"][0]["citation"])
        ok &= _check(
            "source citation resolves committed snapshot",
            resolved["diagnostics"] == [] and "second publication text" in resolved["text"],
        )
        removed = control.propose([{"op": "remove", "name": "second-source"}])
        ok &= _check(
            "batch reports completed remove",
            [outcome.status for outcome in removed.outcomes] == ["removed"] and removed.succeeded,
        )
        before = control.publications.validate_current().publication_id
        failure = control.propose(
            [
                {
                    "op": "add",
                    "name": "missing-source",
                    "origin": {"kind": "repo-file", "path": "missing.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                },
                {
                    "op": "add",
                    "name": "third-source",
                    "origin": {"kind": "repo-file", "path": "second.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                },
            ]
        )
        ok &= _check(
            "preflight rejection has no runtime trace",
            [outcome.status for outcome in failure.outcomes] == ["rejected", "not_committed"]
            and control.publications.validate_current().publication_id == before,
        )
        original_materializer = control.materializer

        class FailingMaterializer:
            def materialize(self, _declaration, **_kwargs):
                raise control_module.ScoutDiagnosticsError(
                    (
                        control_module.diagnostic(
                            control_module.DiagnosticCode.MATERIALIZATION_FAILED,
                            source="runtime-failure",
                            detail="fixture",
                        ),
                    )
                )

        control.materializer = FailingMaterializer()
        runtime_failure = control.propose(
            [
                {
                    "op": "add",
                    "name": "first-source",
                    "origin": {"kind": "repo-file", "path": "first.md"},
                    "mime": "text/markdown",
                    "ttl_days": 1,
                },
                {
                    "op": "add",
                    "name": "runtime-other",
                    "origin": {"kind": "repo-file", "path": "second.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                },
            ]
        )
        control.materializer = original_materializer
        ok &= _check(
            "runtime failure has no partial publication",
            [outcome.status for outcome in runtime_failure.outcomes] == ["failed", "not_committed"]
            and control.publications.validate_current().publication_id == before,
        )
        control.materializer = FailingMaterializer()
        initial_failure = control.propose(
            [
                {
                    "op": "add",
                    "name": "absent-source",
                    "origin": {"kind": "repo-file", "path": "second.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                },
                {
                    "op": "add",
                    "name": "absent-batch-other",
                    "origin": {"kind": "repo-file", "path": "first.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                }
            ]
        )
        control.materializer = original_materializer
        registered_absent = control.ledger.read().records.get("absent-source")
        unrelated_publication = control.propose(
            [
                {
                    "op": "add",
                    "name": "unrelated-source",
                    "origin": {"kind": "repo-file", "path": "second.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                }
            ]
        )
        absent_before_retry = "absent-source" not in control.publications.validate_current().records
        retry_absent = control.refresh_stale()
        retried_publication = control.publications.validate_current()
        ok &= _check(
            "failed batch first fetch registers an absent refresh target",
            [outcome.status for outcome in initial_failure.outcomes] == ["failed", "not_committed"]
            and registered_absent is not None
            and registered_absent.snapshot is None
            and unrelated_publication.succeeded
            and absent_before_retry
            and [outcome.status for outcome in retry_absent.outcomes] == ["published"]
            and retried_publication.records["absent-source"].snapshot is not None,
        )
        control.materializer = FailingMaterializer()
        refresh_failure = control.refresh_stale(datetime.now(timezone.utc) + timedelta(days=2))
        control.materializer = original_materializer
        warning_search = control.search("first publication")
        warning_codes = {item["code"] for item in warning_search["warnings"]}
        ok &= _check(
            "failed refresh warns while retaining valid hit",
            [outcome.status for outcome in refresh_failure.outcomes] == ["failed"]
            and "REFRESH_FAILED" in warning_codes
            and bool(warning_search["hits"]),
        )
        original_build_generation = control_module.build_generation

        def failing_build_generation(_resources_root, _records):
            raise control_module.ScoutDiagnosticsError(
                (
                    control_module.diagnostic(
                        control_module.DiagnosticCode.INDEX_INTEGRITY_FAILED,
                        index_id="fixture",
                        detail="fixture index failure",
                    ),
                )
            )

        control_module.build_generation = failing_build_generation
        try:
            index_refresh_failure = control.refresh_stale(
                datetime.now(timezone.utc) + timedelta(days=2)
            )
        finally:
            control_module.build_generation = original_build_generation
        index_warning_codes = {item["code"] for item in control.search("first publication")["warnings"]}
        ok &= _check(
            "index-stage refresh failure warns while retaining valid hit",
            [outcome.status for outcome in index_refresh_failure.outcomes] == ["not_committed"]
            and {item.code.value for item in index_refresh_failure.diagnostics} == {"INDEX_INTEGRITY_FAILED"}
            and "REFRESH_FAILED" in index_warning_codes
            and bool(control.search("first publication")["hits"]),
        )
        current_refresh_record = control.publications.validate_current().records["first-source"]
        current_refresh_snapshot = current_refresh_record.snapshot
        assert current_refresh_snapshot is not None
        interrupted_refresh_journal = control.ledger.begin(
            {
                "kind": "batch",
                "bootstrap": False,
                "refresh_stale": True,
                "rows": [
                    {
                        "op": "add",
                        "name": "first-source",
                        "origin": {"kind": "repo-file", "path": "first.md"},
                        "mime": "text/markdown",
                        "ttl_days": 1,
                    }
                ],
            }
        )
        interrupted_refresh_lease = control.ledger.claim_recovery()
        assert interrupted_refresh_lease is not None
        interrupted_refresh_lease.update(
            "failed",
            candidate={
                "refresh_failure": {
                    "source": "first-source",
                    "snapshot_id": current_refresh_snapshot.snapshot_id,
                    "detail": "MATERIALIZATION_FAILED",
                }
            },
        )
        interrupted_refresh_lease.__exit__(None, None, None)
        control.attempts.clear("first-source")
        recovered_refresh_control = control_module.SourceControl(root, repository, fixture_config)
        recovered_refresh_control._recover_if_needed()
        recovered_warning_codes = {
            item["code"] for item in recovered_refresh_control.search("first publication")["warnings"]
        }
        ok &= _check(
            "interrupted refresh persists warning before journal cleanup",
            interrupted_refresh_journal.operation_id
            and "REFRESH_FAILED" in recovered_warning_codes
            and not recovered_refresh_control.ledger.journal_path.exists(),
        )
        replacement = control.propose(
            [
                {
                    "op": "add",
                    "name": "first-source",
                    "origin": {"kind": "repo-file", "path": "first.md"},
                    "mime": "text/markdown",
                    "ttl_days": 1,
                }
            ]
        )
        cleared_search = control.search("first publication")
        ok &= _check(
            "successful replacement clears refresh warning",
            [outcome.status for outcome in replacement.outcomes] == ["published"]
            and "REFRESH_FAILED" not in {item["code"] for item in cleared_search["warnings"]},
        )
        rejected = control.propose(
            [
                {"op": "remove", "name": "first-source"},
                {"op": "remove", "name": "first-source"},
            ]
        )
        ok &= _check(
            "invalid proposal has every row outcome",
            [outcome.status for outcome in rejected.outcomes] == ["not_committed", "rejected"],
        )
        recovery_outcome_root = root / "recovery-outcome"
        recovery_outcome_control = control_module.SourceControl(
            recovery_outcome_root,
            repository,
            fixture_config,
        )
        recovery_outcome_control.bootstrap(
            [
                {
                    "op": "add",
                    "name": "recovery-source",
                    "origin": {"kind": "repo-file", "path": "first.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                }
            ]
        )
        recovery_outcome_control.ledger.journal_path.write_text("{", encoding="utf-8")
        recovery_outcome = recovery_outcome_control.propose(
            [
                {"op": "remove", "name": "recovery-source"},
                {},
            ]
        )
        ok &= _check(
            "recovery diagnostic preserves proposal row outcomes",
            [outcome.status for outcome in recovery_outcome.outcomes] == ["not_committed", "rejected"]
            and {item.code.value for item in recovery_outcome.diagnostics} == {"LEDGER_JOURNAL_MALFORMED"},
        )
        busy_root = root / "busy-outcome"
        busy_control = control_module.SourceControl(busy_root, repository, fixture_config)
        busy_control.bootstrap(
            [
                {
                    "op": "add",
                    "name": "busy-base",
                    "origin": {"kind": "repo-file", "path": "first.md"},
                    "mime": "text/markdown",
                    "ttl_days": None,
                }
            ]
        )
        original_begin_and_claim = busy_control.ledger.begin_and_claim

        def busy_begin_and_claim(_mutation):
            raise control_module.ScoutDiagnosticsError(
                (
                    control_module.diagnostic(
                        control_module.DiagnosticCode.LEDGER_BUSY,
                        path="fixture-ledger",
                        wait_seconds=1,
                    ),
                )
            )

        busy_control.ledger.begin_and_claim = busy_begin_and_claim
        try:
            busy_outcome = busy_control.propose(
                [
                    {
                        "op": "add",
                        "name": "busy-source",
                        "origin": {"kind": "repo-file", "path": "second.md"},
                        "mime": "text/markdown",
                        "ttl_days": None,
                    }
                ]
            )
        finally:
            busy_control.ledger.begin_and_claim = original_begin_and_claim
        ok &= _check(
            "ledger contention preserves proposal row outcome",
            [outcome.status for outcome in busy_outcome.outcomes] == ["not_committed"]
            and {item.code.value for item in busy_outcome.diagnostics} == {"LEDGER_BUSY"},
        )
        current = control.publications.validate_current()
        snapshot = current.records["first-source"].snapshot
        assert snapshot is not None
        (root / snapshot.artifact_path).write_text("tampered", encoding="utf-8")
        invalid_store = control.propose([{"op": "remove", "name": "first-source"}, {}])
        ok &= _check(
            "invalid store retains row outcomes",
            [outcome.status for outcome in invalid_store.outcomes] == ["not_committed", "rejected"]
            and {item.code.value for item in invalid_store.diagnostics} == {"SOURCE_BINDING_FAILED"},
        )

        config_root = root / "dynamic-config"
        config_repository = config_root / "repo"
        config_repository.mkdir(parents=True)
        (config_repository / "first.md").write_text("dynamic config source", encoding="utf-8")
        original_load_config = control_module.load_config
        dynamic_control = control_module.SourceControl(config_root, config_repository)
        control_module.load_config = lambda: fixture_config
        try:
            dynamic_bootstrap = dynamic_control.bootstrap(
                [
                    {
                        "op": "add",
                        "name": "dynamic-source",
                        "origin": {"kind": "repo-file", "path": "first.md"},
                        "mime": "text/markdown",
                        "ttl_days": None,
                    }
                ]
            )
            config_failure = control_module.ScoutDiagnosticsError(
                (
                    control_module.diagnostic(
                        control_module.DiagnosticCode.CONFIG_MALFORMED,
                        path="fixture-config",
                        detail="fixture",
                    ),
                )
            )

            def invalid_config():
                raise config_failure

            control_module.load_config = invalid_config
            config_search = dynamic_control.search("dynamic config")
            config_read = dynamic_control.read_citation("source:dynamic-source#missing#c0")
            config_proposal = dynamic_control.propose([{"op": "remove", "name": "dynamic-source"}, {}])
            worker_module = _resource_module("source_worker")
            worker_result = worker_module._reply(
                dynamic_control,
                {"op": "search", "query": "dynamic config", "k": 1},
            )
        finally:
            control_module.load_config = original_load_config
        ok &= _check(
            "resident control revalidates checked-in config",
            dynamic_bootstrap.succeeded
            and config_search["hits"] == []
            and {item["code"] for item in config_search["diagnostics"]} == {"CONFIG_MALFORMED"}
            and {item["code"] for item in config_read["diagnostics"]} == {"CONFIG_MALFORMED"}
            and [outcome.status for outcome in config_proposal.outcomes] == ["not_committed", "rejected"]
            and {item.code.value for item in config_proposal.diagnostics} == {"CONFIG_MALFORMED"}
            and {item["code"] for item in worker_result["diagnostics"]} == {"CONFIG_MALFORMED"},
        )
    return ok
