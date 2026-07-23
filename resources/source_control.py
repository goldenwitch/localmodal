#!/usr/bin/env python3
"""Batch source mutation, recovery, refresh selection, and validated source search."""
from __future__ import annotations

import hashlib
import shutil
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping
from urllib.parse import quote, unquote

from activation import transition_lock
from attempts import AttemptStore
from config import ScoutConfig, load_config
from diagnostics import Diagnostic, DiagnosticCode, ScoutDiagnosticsError, diagnostic
from ledger import Journal, Ledger, LedgerState, RecoveryLease
from materializer import Materializer, commit_candidate
from publication import PUBLICATION_ID, PublicationStore
from source_index import (
    IndexGeneration,
    build_generation,
    discard_generation,
    discard_generation_id,
    keyword_search_loaded,
    open_validated_generation,
    semantic_search_loaded,
)
from source_model import (
    AddRow,
    RepoFileOrigin,
    RemoveRow,
    SourceRecord,
    SourceRow,
    SourceSnapshot,
    artifact_root,
    parse_row,
    parse_snapshot,
    resolve_repo_file,
    row_to_json,
    snapshot_to_json,
)
import vine


@dataclass(frozen=True)
class RowOutcome:
    row: int
    status: str
    diagnostics: tuple[Diagnostic, ...] = ()
    batch_operation_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "row": self.row,
            "status": self.status,
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "batch_operation_id": self.batch_operation_id,
        }


@dataclass(frozen=True)
class BatchResult:
    outcomes: tuple[RowOutcome, ...]
    diagnostics: tuple[Diagnostic, ...] = ()
    publication_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "publication_id": self.publication_id,
        }

    @property
    def succeeded(self) -> bool:
        return not self.diagnostics and all(
            outcome.status in {"published", "removed", "not_found"} for outcome in self.outcomes
        )


@dataclass(frozen=True)
class _ParsedRow:
    index: int
    row: SourceRow


@dataclass(frozen=True)
class _RecoveredOperation:
    bootstrap: bool
    result: BatchResult


@dataclass(frozen=True)
class _PreparedPublication:
    publication_id: str
    parent_id: str | None
    index_generation_id: str
    outcomes: tuple[RowOutcome, ...]


class _RowExecutionFailure(ScoutDiagnosticsError):
    def __init__(self, row: int, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.row = row
        super().__init__(diagnostics)


class SourceControl:
    """The only path from explicit source rows to a public source publication."""

    CITATION_TEXT_LIMIT = 12_000
    VINE_CITATION_PARSE_LIMIT = 1_048_576

    def __init__(
        self,
        resources_root: Path | None = None,
        repository_root: Path | None = None,
        config: ScoutConfig | None = None,
    ) -> None:
        self.resources_root = resources_root or Path(__file__).parent
        self.repository_root = repository_root or self.resources_root.parent
        self._fixed_config = config
        self.config: ScoutConfig | None = None
        self.ledger: Ledger | None = None
        self.materializer: Materializer | None = None
        self.publications: PublicationStore | None = None
        self.attempts = AttemptStore(self.resources_root)
        self._loaded_publication_id: str | None = None
        self._loaded_embeddings = None
        if config is not None:
            self._configure(config)

    def bootstrap(self, rows: list[object]) -> BatchResult:
        """Private initial migration path; no public reader exists before success."""
        try:
            self._ensure_config()
        except ScoutDiagnosticsError as exc:
            return BatchResult(outcomes=(), diagnostics=exc.diagnostics)
        recovered = self._recover_if_needed()
        if recovered is not None and recovered.bootstrap:
            return recovered.result
        assert self.publications is not None
        if self.publications.is_activated():
            return self._bootstrap_after_activation()
        return self._run(rows, bootstrap=True)

    def bootstrap_import(self, rows: list[object], import_paths: Mapping[int, Path]) -> BatchResult:
        """Private one-time migration import of exact legacy files into declarations."""
        try:
            self._ensure_config()
        except ScoutDiagnosticsError as exc:
            return BatchResult(outcomes=(), diagnostics=exc.diagnostics)
        recovered = self._recover_if_needed()
        if recovered is not None and recovered.bootstrap:
            return recovered.result
        assert self.publications is not None
        if self.publications.is_activated():
            return self._bootstrap_after_activation()
        return self._run(rows, bootstrap=True, import_paths=import_paths)

    def propose(self, rows: list[object]) -> BatchResult:
        """Apply one explicit public add/remove proposal against a valid publication."""
        try:
            self._ensure_config()
        except ScoutDiagnosticsError as exc:
            _parsed, rejected = self._parse_rows(rows)
            return BatchResult(
                outcomes=self._blocked_outcomes(len(rows), rejected),
                diagnostics=exc.diagnostics,
            )
        try:
            self._recover_if_needed()
        except ScoutDiagnosticsError as exc:
            _parsed, rejected = self._parse_and_preflight_inputs(rows)
            return BatchResult(
                outcomes=self._blocked_outcomes(len(rows), rejected),
                diagnostics=exc.diagnostics,
            )
        try:
            assert self.publications is not None
            self.publications.validate_current()
        except ScoutDiagnosticsError as exc:
            _parsed, rejected = self._parse_and_preflight_inputs(rows)
            return BatchResult(
                outcomes=self._blocked_outcomes(len(rows), rejected),
                diagnostics=exc.diagnostics,
            )
        return self._run(rows, bootstrap=False)

    def refresh_stale(self, now: datetime | None = None) -> BatchResult:
        """Re-materialize only sources with absent or TTL-expired live snapshots."""
        try:
            self._ensure_config()
        except ScoutDiagnosticsError as exc:
            return BatchResult(outcomes=(), diagnostics=exc.diagnostics)
        self._recover_if_needed()
        try:
            lease = self.ledger.begin_and_claim({"kind": "refresh-stale"})
            with lease:
                return self._refresh_stale_with_lease(lease, now or datetime.now(timezone.utc))
        except ScoutDiagnosticsError as exc:
            return BatchResult(outcomes=(), diagnostics=exc.diagnostics)

    def search(self, query: str, k: int = 6) -> dict[str, object]:
        """Validate the master publication before returning semantic and keyword hits."""
        try:
            self._ensure_config()
            assert self.publications is not None
            publication = self.publications.validate_current()
            embeddings = self._open_publication_embeddings(publication)
        except ScoutDiagnosticsError as exc:
            return {"hits": [], "diagnostics": [item.as_dict() for item in exc.diagnostics]}
        return {
            "hits": semantic_search_loaded(embeddings, query, k),
            "keyword_hits": keyword_search_loaded(embeddings, query, k),
            "diagnostics": [],
            "warnings": [item.as_dict() for item in self.attempts.warnings_for(publication.records)],
            "publication_id": publication.publication_id,
        }

    def read_citation(self, citation: str) -> dict[str, object]:
        """Resolve a public citation against the exact committed snapshot."""
        try:
            self._ensure_config()
            assert self.publications is not None
            publication = self.publications.validate_current()
            self._open_publication_embeddings(publication)
        except ScoutDiagnosticsError as exc:
            return {"diagnostics": [item.as_dict() for item in exc.diagnostics]}
        if citation.startswith("source:"):
            return self._read_source_citation(publication.records, citation)
        if citation.endswith("#vine"):
            return self._read_vine_citation(publication.records, citation)
        return {
            "diagnostics": [
                diagnostic(
                    DiagnosticCode.SOURCE_BINDING_FAILED,
                    source="citation",
                    detail="citation is not a source or VINE citation",
                ).as_dict()
            ]
        }

    def _ensure_config(self) -> None:
        config = self._fixed_config or load_config()
        if self.config != config or self.ledger is None:
            self._configure(config)

    def _configure(self, config: ScoutConfig) -> None:
        self.config = config
        self.ledger = Ledger(self.resources_root, config)
        self.materializer = Materializer(self.resources_root, self.repository_root, config)
        self.publications = PublicationStore(self.resources_root, config)

    def close(self) -> None:
        """Release the resident embeddings object when this control plane stops."""
        if self._loaded_embeddings is not None:
            with suppress(Exception):
                self._loaded_embeddings.close()
        self._loaded_publication_id = None
        self._loaded_embeddings = None

    def _open_publication_embeddings(self, publication) -> object | None:
        if self._loaded_publication_id != publication.publication_id:
            self.close()
            _path, embeddings = open_validated_generation(self.resources_root, publication.index)
            self._loaded_publication_id = publication.publication_id
            self._loaded_embeddings = embeddings
        return self._loaded_embeddings

    def _run(
        self,
        raw_rows: list[object],
        *,
        bootstrap: bool,
        import_paths: Mapping[int, Path] | None = None,
    ) -> BatchResult:
        parsed, rejected = self._parse_and_preflight_inputs(raw_rows)
        if rejected:
            return BatchResult(outcomes=self._blocked_outcomes(len(raw_rows), rejected))
        mutation = {
            "kind": "batch",
            "bootstrap": bootstrap,
            "rows": [row_to_json(entry.row) for entry in parsed],
        }
        if import_paths:
            mutation["imports"] = self._serialize_import_paths(import_paths)
        operation_id: str | None = None
        outcomes: list[RowOutcome] = []
        try:
            lease = self.ledger.begin_and_claim(mutation)
            operation_id = lease.journal.operation_id
            with lease:
                try:
                    publication_records, parent_id = self._base(bootstrap, recovery_lease=lease)
                    registry_records = self._registry_records(
                        publication_records,
                        recovery_lease=lease,
                        bootstrap=bootstrap,
                    )
                    outcomes = self._preflight(registry_records, parsed)
                    rejected = [outcome for outcome in outcomes if outcome.status == "rejected"]
                    if rejected:
                        lease.discard()
                        return BatchResult(outcomes=self._blocked_outcomes(len(raw_rows), rejected))
                    return self._execute(
                        lease,
                        parsed,
                        outcomes,
                        publication_records,
                        registry_records,
                        parent_id,
                        import_paths or {},
                        bootstrap=bootstrap,
                    )
                except ScoutDiagnosticsError:
                    if (
                        self.ledger.journal_path.exists()
                        and lease.journal.phase != "published"
                        and not self._has_prepared_publication(lease.journal)
                    ):
                        lease.discard()
                    raise
        except ScoutDiagnosticsError as exc:
            return BatchResult(
                outcomes=self._failure_outcomes(parsed, outcomes, exc, operation_id),
                diagnostics=() if isinstance(exc, _RowExecutionFailure) else exc.diagnostics,
            )

    def _recover_if_needed(self) -> _RecoveredOperation | None:
        lease = self.ledger.claim_recovery()
        if lease is None:
            return None
        with lease:
            bootstrap = lease.journal.mutation.get("bootstrap") is True
            if lease.journal.phase == "published" and lease.journal.publication_id:
                publication = self.publications.load(lease.journal.publication_id)
                self.publications.validate(publication)
                parsed, rejected = self._parse_rows_for_completed_journal(lease.journal)
                if rejected:
                    raise self._recovery_error(lease, "published journal rows no longer parse")
                prepared = self._prepared_publication(lease.journal)
                if prepared is not None:
                    if prepared.publication_id != publication.publication_id:
                        raise self._recovery_error(lease, "prepared publication differs from published journal")
                    self._validate_prepared_outcomes(prepared, parsed, lease.journal)
                registry_records = self._registry_records(
                    publication.records,
                    recovery_lease=lease,
                    bootstrap=bootstrap,
                )
                lease.complete(
                    self._ledger_records_for_publication(publication.records, registry_records, parsed),
                    publication.publication_id,
                )
                return _RecoveredOperation(
                    bootstrap=bootstrap,
                    result=(
                        BatchResult(outcomes=prepared.outcomes, publication_id=publication.publication_id)
                        if prepared is not None
                        else self._published_recovery_result(
                            lease.journal,
                            publication.records,
                            publication.publication_id,
                            parsed,
                        )
                    ),
                )
            mutation = lease.journal.mutation
            if lease.journal.phase == "failed":
                if mutation.get("refresh_stale") is not True:
                    raise self._recovery_error(lease, "failed journal is not a refresh attempt")
                self._finalize_refresh_failure(lease)
                return None
            if mutation.get("kind") == "refresh-stale":
                self._refresh_stale_with_lease(lease, datetime.now(timezone.utc))
                return None
            if mutation.get("kind") != "batch" or not isinstance(mutation.get("rows"), list):
                raise self._recovery_error(lease, "unsupported journal mutation")
            parsed, rejected = self._parse_rows(mutation["rows"])
            if rejected:
                raise self._recovery_error(lease, "journal rows no longer parse")
            self._discard_operation_staging(lease.journal.operation_id, parsed)
            prepared = self._prepared_publication(lease.journal)
            if prepared is not None:
                if self.publications.current_id() == prepared.publication_id:
                    publication = self.publications.load(prepared.publication_id)
                    self.publications.validate(publication)
                    self._validate_prepared_outcomes(prepared, parsed, lease.journal)
                    registry_records = self._registry_records(
                        publication.records,
                        recovery_lease=lease,
                        bootstrap=bootstrap,
                    )
                    lease.update("published", publication_id=prepared.publication_id)
                    lease.complete(
                        self._ledger_records_for_publication(publication.records, registry_records, parsed),
                        prepared.publication_id,
                    )
                    return _RecoveredOperation(
                        bootstrap=bootstrap,
                        result=BatchResult(outcomes=prepared.outcomes, publication_id=prepared.publication_id),
                    )
                if self.publications.current_id() == prepared.parent_id:
                    try:
                        publication = self.publications.load(prepared.publication_id)
                        self.publications.validate(publication)
                        activated = self.publications.activate(
                            publication,
                            expected_parent=prepared.parent_id,
                        )
                    except OSError as exc:
                        raise ScoutDiagnosticsError(
                            (
                                diagnostic(
                                    DiagnosticCode.PUBLICATION_INTEGRITY_FAILED,
                                    publication_id=prepared.publication_id,
                                    detail=f"{type(exc).__name__}: {exc}",
                                ),
                            )
                        ) from exc
                    except ScoutDiagnosticsError:
                        activated = False
                    if activated:
                        publication = self.publications.load(prepared.publication_id)
                        registry_records = self._registry_records(
                            publication.records,
                            recovery_lease=lease,
                            bootstrap=bootstrap,
                        )
                        lease.update("published", publication_id=prepared.publication_id)
                        lease.complete(
                            self._ledger_records_for_publication(publication.records, registry_records, parsed),
                            prepared.publication_id,
                        )
                        return _RecoveredOperation(
                            bootstrap=bootstrap,
                            result=BatchResult(outcomes=prepared.outcomes, publication_id=prepared.publication_id),
                        )
                self._discard_prepared_publication(prepared)
                self._clear_prepared_candidate(lease, parsed)
            snapshots = self._candidate_snapshots(lease.journal, parsed)
            if snapshots:
                import_paths = {}
            else:
                parsed, rejected = self._parse_and_preflight_inputs(mutation["rows"])
                if rejected:
                    raise self._recovery_error(lease, "journal rows no longer admit")
                import_paths = self._parse_import_paths(mutation.get("imports"))
            publication_records, parent_id = self._base(bootstrap, recovery_lease=lease)
            registry_records = self._registry_records(
                publication_records,
                recovery_lease=lease,
                bootstrap=bootstrap,
            )
            preflight = self._preflight(registry_records, parsed)
            if any(outcome.status == "rejected" for outcome in preflight):
                raise self._recovery_error(lease, "journal batch no longer preflights")
            return _RecoveredOperation(
                bootstrap=bootstrap,
                result=self._execute(
                    lease,
                    parsed,
                    preflight,
                    publication_records,
                    registry_records,
                    parent_id,
                    import_paths,
                    bootstrap=bootstrap,
                ),
            )

    def _refresh_stale_with_lease(self, lease: RecoveryLease, now: datetime) -> BatchResult:
        """Select refresh targets only after the journal excludes competing source mutations."""
        parsed: list[_ParsedRow] = []
        selected: dict[int, SourceRecord] = {}
        outcomes: list[RowOutcome] = []
        try:
            publication_records, parent_id = self._base(False, recovery_lease=lease)
            registry_records = self._registry_records(publication_records, recovery_lease=lease)
            execution_records = dict(publication_records)
            for record in registry_records.values():
                snapshot = record.snapshot
                ttl = record.declaration.ttl_days
                stale = snapshot is None
                if snapshot is not None and ttl is not None:
                    materialized = datetime.fromisoformat(snapshot.materialized_at.replace("Z", "+00:00"))
                    stale = (now - materialized).total_seconds() > ttl * 86_400
                if stale:
                    row = _ParsedRow(index=len(parsed), row=AddRow(record.declaration))
                    parsed.append(row)
                    selected[row.index] = record
                    if record.snapshot is None:
                        execution_records[record.declaration.name] = record
            if not parsed:
                lease.discard()
                return BatchResult(outcomes=())
            mutation = {
                "kind": "batch",
                "bootstrap": False,
                "refresh_stale": True,
                "rows": [row_to_json(entry.row) for entry in parsed],
            }
            lease.replace_mutation(mutation)
            outcomes = self._preflight(registry_records, parsed)
            rejected = [outcome for outcome in outcomes if outcome.status == "rejected"]
            if rejected:
                lease.discard()
                return BatchResult(outcomes=self._blocked_outcomes(len(parsed), rejected))
            result = self._execute(
                lease,
                parsed,
                outcomes,
                execution_records,
                registry_records,
                parent_id,
                {},
                bootstrap=False,
            )
        except ScoutDiagnosticsError as exc:
            if lease.journal.phase == "failed":
                self._finalize_refresh_failure(lease)
            elif not self._prepared_publication_is_current(lease.journal):
                if self._stage_refresh_failures(lease, selected, exc):
                    self._finalize_refresh_failure(lease)
                elif self.ledger.journal_path.exists():
                    lease.discard()
            result = BatchResult(
                outcomes=self._failure_outcomes(parsed, outcomes, exc, lease.journal.operation_id),
                diagnostics=() if isinstance(exc, _RowExecutionFailure) else exc.diagnostics,
            )
        return result

    def _execute(
        self,
        lease: RecoveryLease,
        parsed: list[_ParsedRow],
        outcomes: list[RowOutcome],
        publication_records: Mapping[str, SourceRecord],
        registry_records: Mapping[str, SourceRecord],
        parent_id: str | None,
        import_paths: Mapping[int, Path],
        *,
        bootstrap: bool,
    ) -> BatchResult:
        if bootstrap and import_paths:
            with transition_lock(self.resources_root):
                return self._execute_inner(
                    lease,
                    parsed,
                    outcomes,
                    publication_records,
                    registry_records,
                    parent_id,
                    import_paths,
                    bootstrap=bootstrap,
                )
        return self._execute_inner(
            lease,
            parsed,
            outcomes,
            publication_records,
            registry_records,
            parent_id,
            import_paths,
            bootstrap=bootstrap,
        )

    def _execute_inner(
        self,
        lease: RecoveryLease,
        parsed: list[_ParsedRow],
        outcomes: list[RowOutcome],
        publication_records: Mapping[str, SourceRecord],
        registry_records: Mapping[str, SourceRecord],
        parent_id: str | None,
        import_paths: Mapping[int, Path],
        *,
        bootstrap: bool,
    ) -> BatchResult:
        snapshots = dict(self._candidate_snapshots(lease.journal, parsed))
        generation: IndexGeneration | None = None
        publication = None
        try:
            if not snapshots:
                if import_paths:
                    self._materialize_adds(
                        lease,
                        parsed,
                        import_paths,
                        snapshots,
                        import_only=True,
                    )
                self._materialize_adds(
                    lease,
                    parsed,
                    import_paths,
                    snapshots,
                    import_only=False if import_paths else None,
                )
                lease.update(
                    "staged",
                    candidate={
                        "snapshots": {
                            name: snapshot_to_json(snapshot)
                            for name, snapshot in snapshots.items()
                        }
                    },
                )
            while True:
                records = self._apply_rows(
                    publication_records,
                    parsed,
                    snapshots,
                    lease.journal.operation_id,
                    outcomes,
                )
                try:
                    generation = build_generation(self.resources_root, records)
                except OSError as exc:
                    raise ScoutDiagnosticsError(
                        (
                            diagnostic(
                                DiagnosticCode.INDEX_INTEGRITY_FAILED,
                                index_id="candidate",
                                detail=f"{type(exc).__name__}: {exc}",
                            ),
                        )
                    ) from exc
                try:
                    publication = self.publications.create_candidate(records, generation, parent_id=parent_id)
                    prepared_result = self._publication_result(
                        parsed,
                        outcomes,
                        lease.journal.operation_id,
                        publication.publication_id,
                    )
                    lease.update(
                        "staged",
                        candidate=self._prepared_candidate(snapshots, publication, prepared_result),
                    )
                    activated = self.publications.activate(publication, expected_parent=parent_id)
                except OSError as exc:
                    raise ScoutDiagnosticsError(
                        (
                            diagnostic(
                                DiagnosticCode.PUBLICATION_INTEGRITY_FAILED,
                                publication_id=(publication.publication_id if publication is not None else "candidate"),
                                detail=f"{type(exc).__name__}: {exc}",
                            ),
                        )
                    ) from exc
                if activated:
                    lease.update("published", publication_id=publication.publication_id)
                    ledger_records = self._ledger_records_for_publication(
                        records,
                        registry_records,
                        parsed,
                    )
                    lease.complete(ledger_records, publication.publication_id)
                    for entry in parsed:
                        if isinstance(entry.row, AddRow):
                            self.attempts.clear(entry.row.declaration.name)
                    return prepared_result
                self._clear_prepared_candidate(lease, parsed)
                self.publications.discard_candidate(publication)
                discard_generation(self.resources_root, generation)
                publication = None
                generation = None
                current = self.publications.validate_current()
                publication_records = current.records
                registry_records = self._registry_records(
                    publication_records,
                    recovery_lease=lease,
                    bootstrap=bootstrap,
                )
                parent_id = current.publication_id
        except _RowExecutionFailure as exc:
            self._discard_operation_candidates(
                parsed,
                snapshots,
                generation,
                publication,
                lease.journal.operation_id,
            )
            if self._stage_refresh_failure(lease, parsed, publication_records, exc):
                raise
            registered_absent = self._register_initial_fetch_failure(lease, parsed, registry_records, exc)
            if not registered_absent and lease.journal.phase == "claimed":
                lease.discard()
            raise
        except ScoutDiagnosticsError:
            prepared_is_current = self._prepared_publication_is_current(lease.journal)
            is_refresh = lease.journal.mutation.get("refresh_stale") is True
            if not prepared_is_current and not self._has_prepared_publication(lease.journal):
                self._discard_operation_candidates(
                    parsed,
                    snapshots,
                    generation,
                    publication,
                    lease.journal.operation_id,
                )
            if (
                self.ledger.journal_path.exists()
                and not prepared_is_current
                and not self._has_prepared_publication(lease.journal)
                and not is_refresh
            ):
                lease.discard()
            raise

    def _stage_refresh_failure(
        self,
        lease: RecoveryLease,
        parsed: Iterable[_ParsedRow],
        base_records: Mapping[str, SourceRecord],
        failure: _RowExecutionFailure,
    ) -> bool:
        if lease.journal.mutation.get("refresh_stale") is not True:
            return False
        failed_entry = next((entry for entry in parsed if entry.index == failure.row), None)
        if not isinstance(failed_entry.row if failed_entry is not None else None, AddRow):
            return False
        record = base_records.get(failed_entry.row.declaration.name)
        snapshot = record.snapshot if record is not None else None
        if snapshot is None:
            return False
        detail = "; ".join(item.code.value for item in failure.diagnostics) or "materialization failed"
        lease.update(
            "failed",
            candidate={
                "refresh_failure": {
                    "source": record.declaration.name,
                    "snapshot_id": snapshot.snapshot_id,
                    "detail": detail,
                }
            },
        )
        return True

    def _finalize_refresh_failure(self, lease: RecoveryLease) -> None:
        candidate = lease.journal.candidate
        failure = candidate.get("refresh_failure") if isinstance(candidate, Mapping) else None
        failures = candidate.get("refresh_failures") if isinstance(candidate, Mapping) else None
        if isinstance(failure, Mapping):
            failures = [failure]
        if not isinstance(failures, list) or not failures:
            raise self._recovery_error(lease, "failed refresh journal has no failure detail")
        for failure in failures:
            if not isinstance(failure, Mapping):
                raise self._recovery_error(lease, "failed refresh journal has malformed failure detail")
            source = failure.get("source")
            snapshot_id = failure.get("snapshot_id")
            detail = failure.get("detail")
            if not all(isinstance(value, str) and value for value in (source, snapshot_id, detail)):
                raise self._recovery_error(lease, "failed refresh journal has malformed failure detail")
            self.attempts.record_refresh_failure_detail(source, snapshot_id, detail)
        lease.discard()

    def _stage_refresh_failures(
        self,
        lease: RecoveryLease,
        selected: Mapping[int, SourceRecord],
        error: ScoutDiagnosticsError,
    ) -> bool:
        failures = [
            {
                "source": record.declaration.name,
                "snapshot_id": record.snapshot.snapshot_id,
                "detail": "; ".join(item.code.value for item in error.diagnostics)
                or "refresh failed",
            }
            for record in selected.values()
            if record.snapshot is not None
        ]
        if not failures:
            return False
        lease.update("failed", candidate={"refresh_failures": failures})
        return True

    def _prepared_candidate(
        self,
        snapshots: Mapping[str, SourceSnapshot],
        publication,
        result: BatchResult,
    ) -> dict[str, object]:
        return {
            "snapshots": {
                name: snapshot_to_json(snapshot)
                for name, snapshot in snapshots.items()
            },
            "prepared_publication": {
                "publication_id": publication.publication_id,
                "parent_id": publication.parent_id,
                "index_generation_id": publication.index.generation_id,
                "outcomes": [
                    {"row": outcome.row, "status": outcome.status}
                    for outcome in result.outcomes
                ],
            },
        }

    def _prepared_publication(self, journal: Journal) -> _PreparedPublication | None:
        candidate = journal.candidate
        if candidate is None or "prepared_publication" not in candidate:
            return None
        raw = candidate.get("prepared_publication")
        if not isinstance(raw, dict) or set(raw) != {
            "publication_id", "parent_id", "index_generation_id", "outcomes"
        }:
            raise self._recovery_error(journal, "prepared publication is malformed")
        publication_id = raw.get("publication_id")
        parent_id = raw.get("parent_id")
        index_generation_id = raw.get("index_generation_id")
        raw_outcomes = raw.get("outcomes")
        if (
            not isinstance(publication_id, str)
            or PUBLICATION_ID.fullmatch(publication_id) is None
            or parent_id is not None and (
                not isinstance(parent_id, str) or PUBLICATION_ID.fullmatch(parent_id) is None
            )
            or not isinstance(index_generation_id, str)
            or PUBLICATION_ID.fullmatch(index_generation_id) is None
            or not isinstance(raw_outcomes, list)
        ):
            raise self._recovery_error(journal, "prepared publication has invalid fields")
        outcomes: list[RowOutcome] = []
        rows: set[int] = set()
        for raw_outcome in raw_outcomes:
            if (
                not isinstance(raw_outcome, dict)
                or set(raw_outcome) != {"row", "status"}
                or isinstance(raw_outcome.get("row"), bool)
                or not isinstance(raw_outcome.get("row"), int)
                or raw_outcome["row"] < 0
                or raw_outcome["row"] in rows
                or raw_outcome.get("status") not in {"published", "removed", "not_found"}
            ):
                raise self._recovery_error(journal, "prepared publication outcomes are malformed")
            rows.add(raw_outcome["row"])
            outcomes.append(
                RowOutcome(
                    row=raw_outcome["row"],
                    status=raw_outcome["status"],
                    batch_operation_id=journal.operation_id,
                )
            )
        return _PreparedPublication(publication_id, parent_id, index_generation_id, tuple(outcomes))

    def _clear_prepared_candidate(self, lease: RecoveryLease, parsed: Iterable[_ParsedRow]) -> None:
        snapshots = self._candidate_snapshots(lease.journal, parsed)
        lease.update(
            "staged",
            candidate={
                "snapshots": {
                    name: snapshot_to_json(snapshot)
                    for name, snapshot in snapshots.items()
                }
            },
        )

    def _discard_prepared_publication(self, prepared: _PreparedPublication) -> None:
        try:
            publication = self.publications.load(prepared.publication_id)
        except ScoutDiagnosticsError:
            self.publications.discard_candidate_id(prepared.publication_id)
            discard_generation_id(self.resources_root, prepared.index_generation_id)
            return
        self.publications.discard_candidate(publication)
        discard_generation(self.resources_root, publication.index)

    def _prepared_publication_is_current(self, journal: Journal) -> bool:
        prepared = self._prepared_publication(journal)
        return prepared is not None and self.publications.current_id() == prepared.publication_id

    @staticmethod
    def _has_prepared_publication(journal: Journal) -> bool:
        return isinstance(journal.candidate, Mapping) and "prepared_publication" in journal.candidate

    @staticmethod
    def _validate_prepared_outcomes(
        prepared: _PreparedPublication,
        parsed: Iterable[_ParsedRow],
        journal: Journal,
    ) -> None:
        expected_rows = {entry.index for entry in parsed}
        if {outcome.row for outcome in prepared.outcomes} != expected_rows:
            raise SourceControl._recovery_error(journal, "prepared publication outcomes do not match journal rows")

    @staticmethod
    def _publication_result(
        parsed: Iterable[_ParsedRow],
        outcomes: Iterable[RowOutcome],
        operation_id: str,
        publication_id: str,
    ) -> BatchResult:
        rows = {entry.index: entry.row for entry in parsed}
        return BatchResult(
            outcomes=tuple(
                RowOutcome(
                    row=outcome.row,
                    status=(
                        outcome.status
                        if outcome.status == "not_found"
                        else "removed"
                        if isinstance(rows[outcome.row], RemoveRow)
                        else "published"
                    ),
                    batch_operation_id=operation_id,
                )
                for outcome in outcomes
            ),
            publication_id=publication_id,
        )

    def _base(
        self,
        bootstrap: bool,
        recovery_lease: RecoveryLease | None = None,
    ) -> tuple[Mapping[str, SourceRecord], str | None]:
        current = self.publications.current_id()
        if current is not None:
            publication = self.publications.validate_current()
            return publication.records, publication.publication_id
        if not bootstrap:
            raise ScoutDiagnosticsError(
                (diagnostic(DiagnosticCode.PUBLICATION_MISSING, path=str(self.publications.current_path)),)
            )
        state = self._ledger_state(bootstrap=True, recovery_lease=recovery_lease)
        return {
            name: record
            for name, record in state.records.items()
            if record.snapshot is not None
        }, None

    def _registry_records(
        self,
        publication_records: Mapping[str, SourceRecord],
        *,
        recovery_lease: RecoveryLease | None = None,
        bootstrap: bool = False,
    ) -> Mapping[str, SourceRecord]:
        state = self._ledger_state(bootstrap=bootstrap, recovery_lease=recovery_lease)
        records = dict(publication_records)
        for name, record in state.records.items():
            if record.snapshot is None:
                records[name] = record
        return records

    def _ledger_state(
        self,
        *,
        bootstrap: bool,
        recovery_lease: RecoveryLease | None,
    ) -> LedgerState:
        try:
            return (
                recovery_lease.read_committed_state()
                if recovery_lease is not None
                else self.ledger.read()
            )
        except ScoutDiagnosticsError as exc:
            codes = {item.code for item in exc.diagnostics}
            if bootstrap and codes == {DiagnosticCode.LEGACY_MIGRATION_REQUIRED}:
                return LedgerState(records={})
            raise

    @staticmethod
    def _published_recovery_result(
        journal: Journal,
        records: Mapping[str, SourceRecord],
        publication_id: str,
        parsed: Iterable[_ParsedRow],
    ) -> BatchResult:
        outcomes = []
        for entry in parsed:
            if isinstance(entry.row, AddRow):
                status = "published"
            else:
                status = "not_found" if entry.row.name in records else "removed"
            outcomes.append(
                RowOutcome(
                    row=entry.index,
                    status=status,
                    batch_operation_id=journal.operation_id,
                )
            )
        return BatchResult(outcomes=tuple(outcomes), publication_id=publication_id)

    @staticmethod
    def _ledger_records_for_publication(
        publication_records: Mapping[str, SourceRecord],
        registry_records: Mapping[str, SourceRecord],
        parsed: Iterable[_ParsedRow],
    ) -> Mapping[str, SourceRecord]:
        records = dict(publication_records)
        touched = {
            entry.row.declaration.name if isinstance(entry.row, AddRow) else entry.row.name
            for entry in parsed
        }
        for name, record in registry_records.items():
            if record.snapshot is None and name not in touched:
                records[name] = record
        return records

    @staticmethod
    def _parse_rows_for_completed_journal(journal: Journal) -> tuple[list[_ParsedRow], list[RowOutcome]]:
        raw_rows = journal.mutation.get("rows")
        if not isinstance(raw_rows, list):
            return [], [RowOutcome(row=0, status="rejected")]
        return SourceControl._parse_rows(raw_rows)

    def _bootstrap_after_activation(self) -> BatchResult:
        return BatchResult(
            outcomes=(),
            diagnostics=(
                diagnostic(
                    DiagnosticCode.BOOTSTRAP_AFTER_ACTIVATION,
                    path=str(self.publications.activation_path),
                ),
            ),
        )

    @staticmethod
    def _register_initial_fetch_failure(
        lease: RecoveryLease,
        parsed: Iterable[_ParsedRow],
        base_records: Mapping[str, SourceRecord],
        failure: _RowExecutionFailure,
    ) -> bool:
        entries = tuple(parsed)
        failed_entry = next((entry for entry in entries if entry.index == failure.row), None)
        if not isinstance(failed_entry.row if failed_entry is not None else None, AddRow):
            return False
        declaration = failed_entry.row.declaration
        if declaration.name in base_records:
            return False
        lease.register_absent_source(SourceRecord(declaration=declaration, snapshot=None))
        return True

    @staticmethod
    def _parse_rows(raw_rows: list[object]) -> tuple[list[_ParsedRow], list[RowOutcome]]:
        parsed: list[_ParsedRow] = []
        rejected: list[RowOutcome] = []
        names: set[str] = set()
        for index, raw in enumerate(raw_rows):
            try:
                row = parse_row(raw, index)
                name = row.declaration.name if isinstance(row, AddRow) else row.name
                if name in names:
                    raise ScoutDiagnosticsError(
                        (
                            diagnostic(
                                DiagnosticCode.SOURCE_ROW_INVALID,
                                path=f"$[{index}]",
                                detail="batch may name each source at most once",
                            ),
                        )
                    )
                names.add(name)
                parsed.append(_ParsedRow(index=index, row=row))
            except ScoutDiagnosticsError as exc:
                rejected.append(RowOutcome(row=index, status="rejected", diagnostics=exc.diagnostics))
        return parsed, rejected

    def _parse_and_preflight_inputs(
        self,
        raw_rows: list[object],
    ) -> tuple[list[_ParsedRow], list[RowOutcome]]:
        parsed, rejected = self._parse_rows(raw_rows)
        for entry in parsed:
            if not isinstance(entry.row, AddRow):
                continue
            origin_path = getattr(entry.row.declaration.origin, "path", None)
            if not isinstance(origin_path, str):
                continue
            try:
                resolve_repo_file(
                    RepoFileOrigin(origin_path),
                    self.repository_root,
                    publishable_paths=self.config.repo_files.publishable_paths,
                )
            except ScoutDiagnosticsError as exc:
                rejected.append(RowOutcome(entry.index, "rejected", exc.diagnostics))
        return parsed, rejected

    @staticmethod
    def _blocked_outcomes(length: int, rejected: list[RowOutcome]) -> tuple[RowOutcome, ...]:
        by_row = {outcome.row: outcome for outcome in rejected}
        return tuple(
            by_row.get(index, RowOutcome(row=index, status="not_committed"))
            for index in range(length)
        )

    def _preflight(self, base_records: Mapping[str, SourceRecord], parsed: Iterable[_ParsedRow]) -> list[RowOutcome]:
        records = dict(base_records)
        outcomes: list[RowOutcome] = []
        live_vine_paths = {
            name: path
            for name, record in records.items()
            if record.snapshot is not None
            if (path := self._vine_origin_path(record.declaration)) is not None
        }
        for entry in parsed:
            row = entry.row
            if isinstance(row, AddRow):
                records[row.declaration.name] = SourceRecord(declaration=row.declaration, snapshot=None)
                path = self._vine_origin_path(row.declaration)
                if path is None:
                    live_vine_paths.pop(row.declaration.name, None)
                else:
                    live_vine_paths[row.declaration.name] = path
                outcomes.append(RowOutcome(row=entry.index, status="accepted"))
            elif row.name in records:
                records.pop(row.name)
                live_vine_paths.pop(row.name, None)
                outcomes.append(RowOutcome(row=entry.index, status="accepted"))
            else:
                outcomes.append(RowOutcome(row=entry.index, status="not_found"))
        duplicate_paths: dict[str, set[str]] = {}
        for name, path in live_vine_paths.items():
            duplicate_paths.setdefault(path, set()).add(name)
        conflicting_names = {
            name: path
            for path, names in duplicate_paths.items()
            if len(names) > 1
            for name in names
        }
        if conflicting_names:
            by_row = {entry.index: entry for entry in parsed}
            outcomes = [
                RowOutcome(
                    row=outcome.row,
                    status="rejected",
                    diagnostics=(
                        diagnostic(
                            DiagnosticCode.SOURCE_ROW_INVALID,
                            path=f"$[{outcome.row}]",
                            detail=(
                                "live VINE repository path is already bound to another source: "
                                f"{conflicting_names[by_row[outcome.row].row.declaration.name]!r}"
                            ),
                        ),
                    ),
                )
                if (
                    outcome.status == "accepted"
                    and isinstance(by_row[outcome.row].row, AddRow)
                    and by_row[outcome.row].row.declaration.name in conflicting_names
                )
                else outcome
                for outcome in outcomes
            ]
        return outcomes

    @staticmethod
    def _vine_origin_path(declaration) -> str | None:
        path = getattr(declaration.origin, "path", None)
        return path if isinstance(path, str) and path.endswith(".vine") else None

    def _materialize_adds(
        self,
        lease: RecoveryLease,
        parsed: Iterable[_ParsedRow],
        import_paths: Mapping[int, Path],
        snapshots: dict[str, SourceSnapshot],
        *,
        import_only: bool | None = None,
    ) -> None:
        for entry in parsed:
            if not isinstance(entry.row, AddRow):
                continue
            try:
                import_path = import_paths.get(entry.index)
                if import_only is not None and (import_path is not None) != import_only:
                    continue
                if import_path is None:
                    candidate = self.materializer.materialize(
                        entry.row.declaration,
                        operation_id=lease.journal.operation_id,
                    )
                else:
                    candidate = self.materializer.import_file(
                        entry.row.declaration,
                        import_path,
                        import_path.relative_to(self.repository_root).as_posix(),
                        operation_id=lease.journal.operation_id,
                    )
                snapshots[entry.row.declaration.name] = candidate.snapshot
                lease.update(
                    "claimed",
                    candidate={
                        "snapshots": {
                            name: snapshot_to_json(snapshot)
                            for name, snapshot in snapshots.items()
                        }
                    },
                )
                commit_candidate(candidate, self.resources_root)
            except ScoutDiagnosticsError as exc:
                raise _RowExecutionFailure(entry.index, exc.diagnostics) from exc
            except OSError as exc:
                raise _RowExecutionFailure(
                    entry.index,
                    (
                        diagnostic(
                            DiagnosticCode.MATERIALIZATION_FAILED,
                            source=entry.row.declaration.name,
                            detail=f"{type(exc).__name__}: {exc}",
                        ),
                    ),
                ) from exc

    def _discard_operation_candidates(
        self,
        parsed: Iterable[_ParsedRow],
        snapshots: Mapping[str, SourceSnapshot],
        generation: IndexGeneration | None,
        publication,
        operation_id: str,
    ) -> None:
        if publication is not None:
            self.publications.discard_candidate(publication)
        if generation is not None:
            discard_generation(self.resources_root, generation)
        self._discard_snapshot_artifacts(snapshots)
        self._discard_operation_staging(operation_id, parsed)

    def _discard_operation_staging(
        self,
        operation_id: str,
        parsed: Iterable[_ParsedRow],
    ) -> None:
        for entry in parsed:
            if not isinstance(entry.row, AddRow):
                continue
            root = artifact_root(self.resources_root, entry.row.declaration.name)
            shutil.rmtree(root / "staging" / operation_id, ignore_errors=True)
            shutil.rmtree(
                self.resources_root / ".scout-staging" / operation_id / root.name,
                ignore_errors=True,
            )
        operation_root = self.resources_root / ".scout-staging" / operation_id
        try:
            operation_root.rmdir()
        except OSError:
            pass

    def _discard_snapshot_artifacts(self, snapshots: Mapping[str, SourceSnapshot]) -> None:
        for name, snapshot in snapshots.items():
            expected = (
                self.resources_root
                / f"scout-source--{name}"
                / "generations"
                / snapshot.snapshot_id
                / "content"
            )
            content = self.resources_root / snapshot.artifact_path
            if content != expected:
                continue
            shutil.rmtree(content.parent, ignore_errors=True)

    def _serialize_import_paths(self, import_paths: Mapping[int, Path]) -> dict[str, str]:
        serialized: dict[str, str] = {}
        root = self.repository_root.resolve()
        for index, path in import_paths.items():
            try:
                serialized[str(index)] = path.resolve(strict=True).relative_to(root).as_posix()
            except (OSError, RuntimeError, ValueError) as exc:
                raise ScoutDiagnosticsError(
                    (
                        diagnostic(
                            DiagnosticCode.ORIGIN_NOT_FOUND,
                            path=str(path),
                        ),
                    )
                ) from exc
        return serialized

    def _parse_import_paths(self, raw: object) -> Mapping[int, Path]:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise self._recovery_error_from_detail("journal imports must be object")
        root = self.repository_root.resolve()
        imports: dict[int, Path] = {}
        for raw_index, raw_path in raw.items():
            try:
                index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise self._recovery_error_from_detail("journal import index must be integer") from exc
            if index < 0 or not isinstance(raw_path, str):
                raise self._recovery_error_from_detail("journal import path is invalid")
            if "\\" in raw_path:
                raise self._recovery_error_from_detail("journal import path must use POSIX separators")
            relative = PurePosixPath(raw_path)
            if (
                relative.is_absolute()
                or relative.as_posix() != raw_path
                or any(part in ("", ".", "..") or ":" in part for part in relative.parts)
            ):
                raise self._recovery_error_from_detail("journal import path is invalid")
            candidate = root.joinpath(*relative.parts)
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise self._recovery_error_from_detail("journal import path escapes repository") from exc
            if not resolved.is_file():
                raise self._recovery_error_from_detail("journal import path is not a file")
            imports[index] = resolved
        return imports

    def _candidate_snapshots(
        self,
        journal: Journal,
        parsed: Iterable[_ParsedRow],
    ) -> Mapping[str, SourceSnapshot]:
        candidate = journal.candidate
        if candidate is None:
            return {}
        raw_snapshots = candidate.get("snapshots")
        if not isinstance(raw_snapshots, dict):
            raise self._recovery_error(journal, "staged journal candidate is malformed")
        snapshots = {
            name: parse_snapshot(raw, f"$.candidate.snapshots.{name}")
            for name, raw in raw_snapshots.items()
        }
        expected_names = {
            entry.row.declaration.name
            for entry in parsed
            if isinstance(entry.row, AddRow)
        }
        if set(snapshots) != expected_names or any(
            not self._snapshot_artifact_matches(snapshot) for snapshot in snapshots.values()
        ):
            self._discard_snapshot_artifacts(snapshots)
            return {}
        return snapshots

    def _snapshot_artifact_matches(self, snapshot: SourceSnapshot) -> bool:
        content = self.resources_root / snapshot.artifact_path
        try:
            if not content.is_file() or content.stat().st_size != snapshot.byte_count:
                return False
            digest = hashlib.sha256()
            with content.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    digest.update(chunk)
            return digest.hexdigest() == snapshot.sha256
        except OSError:
            return False

    @staticmethod
    def _apply_rows(
        base_records: Mapping[str, SourceRecord],
        parsed: Iterable[_ParsedRow],
        snapshots: Mapping[str, SourceSnapshot],
        operation_id: str,
        outcomes: Iterable[RowOutcome],
    ) -> Mapping[str, SourceRecord]:
        records = dict(base_records)
        outcomes_by_row = {outcome.row: outcome for outcome in outcomes}
        for entry in parsed:
            row = entry.row
            if isinstance(row, AddRow):
                snapshot = snapshots.get(row.declaration.name)
                if snapshot is None:
                    raise ScoutDiagnosticsError(
                        (
                            diagnostic(
                                DiagnosticCode.LEDGER_RECOVERY_FAILED,
                                operation_id=operation_id,
                                detail=f"missing candidate snapshot for {row.declaration.name}",
                            ),
                        )
                    )
                records[row.declaration.name] = SourceRecord(row.declaration, snapshot)
            elif outcomes_by_row[entry.index].status != "not_found":
                records.pop(row.name, None)
        return records

    @staticmethod
    def _failure_outcomes(
        parsed: Iterable[_ParsedRow],
        outcomes: Iterable[RowOutcome],
        error: ScoutDiagnosticsError,
        operation_id: str | None,
    ) -> tuple[RowOutcome, ...]:
        by_row = {outcome.row: outcome for outcome in outcomes}
        failed_row = error.row if isinstance(error, _RowExecutionFailure) else None
        result = []
        for entry in parsed:
            outcome = by_row.get(entry.index, RowOutcome(entry.index, "not_committed"))
            if outcome.status == "not_found":
                result.append(RowOutcome(entry.index, "not_found", batch_operation_id=operation_id))
            elif entry.index == failed_row:
                result.append(RowOutcome(entry.index, "failed", error.diagnostics, operation_id))
            else:
                result.append(RowOutcome(entry.index, "not_committed", batch_operation_id=operation_id))
        return tuple(result)

    @staticmethod
    def _recovery_error(journal: Journal | RecoveryLease, detail: str) -> ScoutDiagnosticsError:
        if isinstance(journal, RecoveryLease):
            journal = journal.journal
        return ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.LEDGER_RECOVERY_FAILED,
                    operation_id=journal.operation_id,
                    detail=detail,
                ),
            )
        )

    @staticmethod
    def _recovery_error_from_detail(detail: str) -> ScoutDiagnosticsError:
        return ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.LEDGER_RECOVERY_FAILED,
                    operation_id="unknown",
                    detail=detail,
                ),
            )
        )

    def _read_source_citation(
        self,
        records: Mapping[str, SourceRecord],
        citation: str,
    ) -> dict[str, object]:
        parts = citation.split("#")
        if len(parts) < 3:
            return self._citation_failure("citation", "source citation must include source, snapshot, and chunk")
        name = parts[0][len("source:"):]
        snapshot_id = parts[1]
        record = records.get(name)
        if record is None or record.snapshot is None or record.snapshot.snapshot_id != snapshot_id:
            return self._citation_failure(name, "citation is not bound to the current publication")
        content = self.resources_root / record.snapshot.artifact_path
        try:
            text = self._read_citation_text(content)
        except (OSError, UnicodeError) as exc:
            return self._citation_failure(name, f"cannot read committed artifact: {type(exc).__name__}: {exc}")
        return {
            "citation": citation,
            "source": name,
            "snapshot": snapshot_id,
            "text": text,
            "diagnostics": [],
        }

    def _read_vine_citation(
        self,
        records: Mapping[str, SourceRecord],
        citation: str,
    ) -> dict[str, object]:
        parts = citation.rsplit("#", 2)
        if len(parts) != 3:
            return self._citation_failure("citation", "malformed VINE citation")
        encoded_path, target, _kind = parts
        try:
            origin_path = unquote(encoded_path)
        except Exception as exc:
            return self._citation_failure("citation", f"invalid VINE path: {exc}")
        for name, record in records.items():
            origin = record.declaration.origin
            path = getattr(origin, "path", None)
            snapshot = record.snapshot
            if not isinstance(path, str) or snapshot is None:
                continue
            if quote(path, safe="/.-_~") != encoded_path:
                continue
            artifact = self.resources_root / snapshot.artifact_path
            try:
                if artifact.stat().st_size > self.VINE_CITATION_PARSE_LIMIT:
                    return self._citation_failure(name, "VINE citation artifact exceeds the read limit")
                blocks = vine.parse_vine(artifact)
            except Exception as exc:
                return self._citation_failure(name, f"cannot parse committed VINE artifact: {type(exc).__name__}: {exc}")
            target_kind = "ref" if target.startswith("ref:") else "task"
            target_id = target[4:] if target_kind == "ref" else target
            matches = [block for block in blocks if block.kind == target_kind and block.block_id == target_id]
            if len(matches) != 1:
                return self._citation_failure(name, "VINE target is absent from committed artifact")
            return {
                "citation": citation,
                "source": name,
                "snapshot": snapshot.snapshot_id,
                "text": self._cap_citation_text(matches[0].projection),
                "diagnostics": [],
            }
        return self._citation_failure("citation", "VINE path is not bound to the current publication")

    @classmethod
    def _read_citation_text(cls, path: Path) -> str:
        with path.open("r", encoding="utf-8", errors="strict") as file:
            return cls._cap_citation_text(file.read(cls.CITATION_TEXT_LIMIT + 1))

    @classmethod
    def _cap_citation_text(cls, text: str) -> str:
        if len(text) <= cls.CITATION_TEXT_LIMIT:
            return text
        return text[:cls.CITATION_TEXT_LIMIT] + "\n[truncated]"

    @staticmethod
    def _citation_failure(source: str, detail: str) -> dict[str, object]:
        return {
            "diagnostics": [
                diagnostic(DiagnosticCode.SOURCE_BINDING_FAILED, source=source, detail=detail).as_dict()
            ]
        }
