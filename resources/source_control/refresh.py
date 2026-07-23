from __future__ import annotations

from datetime import datetime
from typing import Mapping

from diagnostics import ScoutDiagnosticsError
from ledger import RecoveryLease
from source_model import AddRow, SourceRecord, row_to_json

from .outcomes import BatchResult, RowOutcome, _ParsedRow, _RowExecutionFailure


class RefreshMixin:
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