from __future__ import annotations

import sys
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from attempts import AttemptStore
from config import ScoutConfig
from diagnostics import DiagnosticCode, ScoutDiagnosticsError, diagnostic
from ledger import Journal, Ledger, LedgerState, RecoveryLease
from materializer import Materializer
from publication import PublicationStore
from source_index import keyword_search_loaded, semantic_search_loaded
from source_model import AddRow, SourceRecord, row_to_json

from .citations import CitationMixin
from .execution import ExecutionMixin
from .inputs import RowPreflightMixin
from .outcomes import BatchResult, RowOutcome, _ParsedRow, _RecoveredOperation, _RowExecutionFailure
from .recovery import RecoveryMixin
from .refresh import RefreshMixin


def _seams():
    """Resolve monkeypatchable runtime seams through the package module object."""
    return sys.modules[__package__]


class SourceControl(
    RowPreflightMixin,
    ExecutionMixin,
    RecoveryMixin,
    RefreshMixin,
    CitationMixin,
):
    """The only path from explicit source rows to a public source publication."""

    CITATION_TEXT_LIMIT = 12_000
    VINE_CITATION_PARSE_LIMIT = 1_048_576

    def __init__(
        self,
        resources_root: Path | None = None,
        repository_root: Path | None = None,
        config: ScoutConfig | None = None,
    ) -> None:
        self.resources_root = resources_root or Path(__file__).parent.parent
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

    def _ensure_config(self) -> None:
        config = self._fixed_config or _seams().load_config()
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
            _path, embeddings = _seams().open_validated_generation(self.resources_root, publication.index)
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