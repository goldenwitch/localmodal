from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path
from typing import Iterable, Mapping

from diagnostics import DiagnosticCode, ScoutDiagnosticsError, diagnostic
from ledger import Journal, RecoveryLease
from publication import PublicationStore
from source_index import IndexGeneration, discard_generation, discard_generation_id
from source_model import AddRow, SourceRecord, SourceSnapshot, artifact_root, parse_snapshot, snapshot_to_json

from .outcomes import BatchResult, RowOutcome, _ParsedRow, _RowExecutionFailure


def _seams():
    """Resolve monkeypatchable runtime seams through the package module object."""
    return sys.modules[__package__]


class ExecutionMixin:
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
            with _seams().transition_lock(self.resources_root):
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
                    generation = _seams().build_generation(self.resources_root, records)
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
                _seams().commit_candidate(candidate, self.resources_root)
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