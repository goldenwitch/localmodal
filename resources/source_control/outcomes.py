from __future__ import annotations

from dataclasses import dataclass

from diagnostics import Diagnostic, ScoutDiagnosticsError
from source_model import SourceRow


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