#!/usr/bin/env python3
"""Batch source mutation, recovery, refresh selection, and validated source search."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from config import ScoutConfig, load_config
from diagnostics import Diagnostic, DiagnosticCode, ScoutDiagnosticsError, diagnostic
from ledger import Journal, Ledger, RecoveryLease
from materializer import Materializer, commit_candidate
from publication import PublicationStore
from source_index import build_generation, keyword_search, semantic_search, validate_generation
from source_model import (
    AddRow,
    RemoveRow,
    SourceRecord,
    SourceRow,
    SourceSnapshot,
    parse_row,
    parse_snapshot,
    row_to_json,
    snapshot_to_json,
)


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


@dataclass(frozen=True)
class _ParsedRow:
    index: int
    row: SourceRow


class _RowExecutionFailure(ScoutDiagnosticsError):
    def __init__(self, row: int, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.row = row
        super().__init__(diagnostics)


class SourceControl:
    """The only path from explicit source rows to a public source publication."""

    def __init__(
        self,
        resources_root: Path | None = None,
        repository_root: Path | None = None,
        config: ScoutConfig | None = None,
    ) -> None:
        self.resources_root = resources_root or Path(__file__).parent
        self.repository_root = repository_root or self.resources_root.parent
        self.config = config or load_config()
        self.ledger = Ledger(self.resources_root, self.config)
        self.materializer = Materializer(self.resources_root, self.repository_root, self.config)
        self.publications = PublicationStore(self.resources_root, self.config)

    def bootstrap(self, rows: list[object]) -> BatchResult:
        """Private initial migration path; no public reader exists before success."""
        self._recover_if_needed()
        return self._run(rows, bootstrap=True)

    def propose(self, rows: list[object]) -> BatchResult:
        """Apply one explicit public add/remove proposal against a valid publication."""
        self._recover_if_needed()
        try:
            self.publications.validate_current()
        except ScoutDiagnosticsError as exc:
            return BatchResult(outcomes=(), diagnostics=exc.diagnostics)
        return self._run(rows, bootstrap=False)

    def refresh_stale(self, now: datetime | None = None) -> BatchResult:
        """Re-materialize only sources with absent or TTL-expired live snapshots."""
        self._recover_if_needed()
        try:
            publication = self.publications.validate_current()
        except ScoutDiagnosticsError as exc:
            return BatchResult(outcomes=(), diagnostics=exc.diagnostics)
        now = now or datetime.now(timezone.utc)
        rows: list[object] = []
        for record in publication.records.values():
            snapshot = record.snapshot
            ttl = record.declaration.ttl_days
            stale = snapshot is None
            if snapshot is not None and ttl is not None:
                materialized = datetime.fromisoformat(snapshot.materialized_at.replace("Z", "+00:00"))
                stale = (now - materialized).total_seconds() > ttl * 86_400
            if stale:
                rows.append(row_to_json(AddRow(record.declaration)))
        if not rows:
            return BatchResult(outcomes=())
        return self._run(rows, bootstrap=False)

    def search(self, query: str, k: int = 6) -> dict[str, object]:
        """Validate the master publication before returning semantic and keyword hits."""
        try:
            publication = self.publications.validate_current()
            generation = validate_generation(self.resources_root, publication.index)
        except ScoutDiagnosticsError as exc:
            return {"hits": [], "diagnostics": [item.as_dict() for item in exc.diagnostics]}
        return {
            "hits": semantic_search(generation, query, k),
            "keyword_hits": keyword_search(generation, query, k),
            "diagnostics": [],
            "publication_id": publication.publication_id,
        }

    def _run(self, raw_rows: list[object], *, bootstrap: bool) -> BatchResult:
        parsed, parse_outcomes = self._parse_rows(raw_rows)
        if parse_outcomes:
            return BatchResult(outcomes=self._blocked_outcomes(len(raw_rows), parse_outcomes))
        try:
            base_records, parent_id = self._base(bootstrap)
        except ScoutDiagnosticsError as exc:
            return BatchResult(outcomes=(), diagnostics=exc.diagnostics)
        outcomes = self._preflight(base_records, parsed)
        mutation = {
            "kind": "batch",
            "bootstrap": bootstrap,
            "rows": [row_to_json(entry.row) for entry in parsed],
        }
        journal: Journal | None = None
        try:
            journal = self.ledger.begin(mutation)
            lease = self.ledger.claim_recovery()
            if lease is None:
                raise ScoutDiagnosticsError(
                    (
                        diagnostic(
                            DiagnosticCode.LEDGER_RECOVERY_FAILED,
                            operation_id=journal.operation_id,
                            detail="journal disappeared before claim",
                        ),
                    )
                )
            with lease:
                return self._execute(lease, parsed, outcomes, base_records, parent_id)
        except ScoutDiagnosticsError as exc:
            return BatchResult(
                outcomes=self._failure_outcomes(parsed, outcomes, exc, journal.operation_id if journal else None),
                diagnostics=() if isinstance(exc, _RowExecutionFailure) else exc.diagnostics,
            )

    def _recover_if_needed(self) -> None:
        lease = self.ledger.claim_recovery()
        if lease is None:
            return
        with lease:
            if lease.journal.phase == "published" and lease.journal.publication_id:
                publication = self.publications.load(lease.journal.publication_id)
                self.publications.validate(publication)
                lease.complete(publication.records, publication.publication_id)
                return
            mutation = lease.journal.mutation
            if mutation.get("kind") != "batch" or not isinstance(mutation.get("rows"), list):
                raise self._recovery_error(lease, "unsupported journal mutation")
            parsed, outcomes = self._parse_rows(mutation["rows"])
            if outcomes:
                raise self._recovery_error(lease, "journal rows no longer parse")
            bootstrap = mutation.get("bootstrap") is True
            base_records, parent_id = self._base(bootstrap)
            preflight = self._preflight(base_records, parsed)
            self._execute(lease, parsed, preflight, base_records, parent_id)

    def _execute(
        self,
        lease: RecoveryLease,
        parsed: list[_ParsedRow],
        outcomes: list[RowOutcome],
        base_records: Mapping[str, SourceRecord],
        parent_id: str | None,
    ) -> BatchResult:
        snapshots = self._candidate_snapshots(lease.journal)
        try:
            if not snapshots:
                snapshots = self._materialize_adds(parsed)
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
                records = self._apply_rows(base_records, parsed, snapshots, lease.journal.operation_id)
                generation = build_generation(self.resources_root, records)
                publication = self.publications.create_candidate(records, generation, parent_id=parent_id)
                if self.publications.activate(publication, expected_parent=parent_id):
                    lease.update("published", publication_id=publication.publication_id)
                    lease.complete(records, publication.publication_id)
                    return BatchResult(
                        outcomes=tuple(
                            RowOutcome(
                                row=outcome.row,
                                status=outcome.status if outcome.status == "not_found" else "published",
                                diagnostics=outcome.diagnostics,
                                batch_operation_id=lease.journal.operation_id,
                            )
                            for outcome in outcomes
                        ),
                        publication_id=publication.publication_id,
                    )
                current = self.publications.validate_current()
                base_records = current.records
                parent_id = current.publication_id
        except ScoutDiagnosticsError:
            if lease.journal.phase == "claimed":
                lease.discard()
            raise

    def _base(self, bootstrap: bool) -> tuple[Mapping[str, SourceRecord], str | None]:
        current = self.publications.current_id()
        if current is not None:
            publication = self.publications.validate_current()
            return publication.records, publication.publication_id
        if not bootstrap:
            raise ScoutDiagnosticsError(
                (diagnostic(DiagnosticCode.PUBLICATION_MISSING, path=str(self.publications.current_path)),)
            )
        try:
            return self.ledger.read().records, None
        except ScoutDiagnosticsError as exc:
            codes = {item.code for item in exc.diagnostics}
            if codes == {DiagnosticCode.LEGACY_MIGRATION_REQUIRED}:
                return {}, None
            raise

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

    @staticmethod
    def _blocked_outcomes(length: int, rejected: list[RowOutcome]) -> tuple[RowOutcome, ...]:
        by_row = {outcome.row: outcome for outcome in rejected}
        return tuple(
            by_row.get(index, RowOutcome(row=index, status="not_committed"))
            for index in range(length)
        )

    @staticmethod
    def _preflight(base_records: Mapping[str, SourceRecord], parsed: Iterable[_ParsedRow]) -> list[RowOutcome]:
        records = dict(base_records)
        outcomes: list[RowOutcome] = []
        for entry in parsed:
            row = entry.row
            if isinstance(row, AddRow):
                records[row.declaration.name] = SourceRecord(declaration=row.declaration, snapshot=None)
                outcomes.append(RowOutcome(row=entry.index, status="accepted"))
            elif row.name in records:
                records.pop(row.name)
                outcomes.append(RowOutcome(row=entry.index, status="accepted"))
            else:
                outcomes.append(RowOutcome(row=entry.index, status="not_found"))
        return outcomes

    def _materialize_adds(self, parsed: Iterable[_ParsedRow]) -> Mapping[str, SourceSnapshot]:
        snapshots: dict[str, SourceSnapshot] = {}
        for entry in parsed:
            if not isinstance(entry.row, AddRow):
                continue
            try:
                candidate = self.materializer.materialize(entry.row.declaration)
                commit_candidate(candidate, self.resources_root)
                snapshots[entry.row.declaration.name] = candidate.snapshot
            except ScoutDiagnosticsError as exc:
                raise _RowExecutionFailure(entry.index, exc.diagnostics) from exc
        return snapshots

    def _candidate_snapshots(self, journal: Journal) -> Mapping[str, SourceSnapshot]:
        candidate = journal.candidate
        if candidate is None:
            return {}
        raw_snapshots = candidate.get("snapshots")
        if not isinstance(raw_snapshots, dict):
            raise self._recovery_error(journal, "staged journal candidate is malformed")
        return {
            name: parse_snapshot(raw, f"$.candidate.snapshots.{name}")
            for name, raw in raw_snapshots.items()
        }

    @staticmethod
    def _apply_rows(
        base_records: Mapping[str, SourceRecord],
        parsed: Iterable[_ParsedRow],
        snapshots: Mapping[str, SourceSnapshot],
        operation_id: str,
    ) -> Mapping[str, SourceRecord]:
        records = dict(base_records)
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
            else:
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
            outcome = by_row[entry.index]
            if outcome.status == "not_found":
                result.append(RowOutcome(entry.index, "not_found", batch_operation_id=operation_id))
            elif entry.index == failed_row:
                result.append(RowOutcome(entry.index, "failed", error.diagnostics, operation_id))
            else:
                result.append(RowOutcome(entry.index, "not_committed", batch_operation_id=operation_id))
        return tuple(result)

    @staticmethod
    def _recovery_error(journal: Journal, detail: str) -> ScoutDiagnosticsError:
        return ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.LEDGER_RECOVERY_FAILED,
                    operation_id=journal.operation_id,
                    detail=detail,
                ),
            )
        )
