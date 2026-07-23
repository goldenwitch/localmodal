from __future__ import annotations

from typing import Iterable, Mapping

from diagnostics import DiagnosticCode, ScoutDiagnosticsError, diagnostic
from source_model import AddRow, RepoFileOrigin, SourceRecord, parse_row, resolve_repo_file

from .outcomes import RowOutcome, _ParsedRow


class RowPreflightMixin:
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