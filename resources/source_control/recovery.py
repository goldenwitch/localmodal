from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from diagnostics import DiagnosticCode, ScoutDiagnosticsError, diagnostic
from ledger import Journal, RecoveryLease
from publication import PUBLICATION_ID
from source_index import discard_generation, discard_generation_id
from source_model import AddRow, RemoveRow, SourceRecord, SourceSnapshot, parse_row, snapshot_to_json

from .inputs import RowPreflightMixin
from .outcomes import BatchResult, RowOutcome, _ParsedRow, _PreparedPublication, _RecoveredOperation, _RowExecutionFailure


class RecoveryMixin:
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
            raise RecoveryMixin._recovery_error(journal, "prepared publication outcomes do not match journal rows")

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
        return RowPreflightMixin._parse_rows(raw_rows)

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