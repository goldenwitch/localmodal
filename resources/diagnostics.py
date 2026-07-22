#!/usr/bin/env python3
"""Typed Scout diagnostics shared by source-management foundations."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class DiagnosticCode(StrEnum):
    """Closed foundation diagnostic discriminators."""

    CONFIG_MISSING = "CONFIG_MISSING"
    CONFIG_MALFORMED = "CONFIG_MALFORMED"
    CONFIG_UNKNOWN_KEY = "CONFIG_UNKNOWN_KEY"
    CONFIG_WRONG_TYPE = "CONFIG_WRONG_TYPE"
    CONFIG_INVALID_VALUE = "CONFIG_INVALID_VALUE"
    LEDGER_BUSY = "LEDGER_BUSY"
    LEDGER_MALFORMED = "LEDGER_MALFORMED"
    LEDGER_JOURNAL_MALFORMED = "LEDGER_JOURNAL_MALFORMED"
    LEDGER_RECOVERY_FAILED = "LEDGER_RECOVERY_FAILED"


_FOUNDATION_SPECS: dict[DiagnosticCode, tuple[tuple[str, ...], str]] = {
    DiagnosticCode.CONFIG_MISSING: (
        ("path",),
        "Create the checked-in Scout configuration at the reported path.",
    ),
    DiagnosticCode.CONFIG_MALFORMED: (
        ("path", "detail"),
        "Repair the JSON syntax at the reported configuration path.",
    ),
    DiagnosticCode.CONFIG_UNKNOWN_KEY: (
        ("path", "key"),
        "Remove the unknown configuration key.",
    ),
    DiagnosticCode.CONFIG_WRONG_TYPE: (
        ("path", "expected", "actual"),
        "Replace the configuration value with the reported expected type.",
    ),
    DiagnosticCode.CONFIG_INVALID_VALUE: (
        ("path", "rule", "value"),
        "Replace the configuration value with one that satisfies the reported rule.",
    ),
    DiagnosticCode.LEDGER_BUSY: (
        ("path", "wait_seconds"),
        "Retry after the current ledger recovery operation finishes.",
    ),
    DiagnosticCode.LEDGER_MALFORMED: (
        ("path", "detail"),
        "Restore the ledger to valid JSON matching the Scout ledger schema.",
    ),
    DiagnosticCode.LEDGER_JOURNAL_MALFORMED: (
        ("path", "detail"),
        "Repair or remove the malformed journal only after confirming the intended mutation.",
    ),
    DiagnosticCode.LEDGER_RECOVERY_FAILED: (
        ("operation_id", "detail"),
        "Inspect the reported recovery operation and retry after repairing its dependency.",
    ),
}


@dataclass(frozen=True)
class Diagnostic:
    """One serializable canonical diagnostic value."""

    code: DiagnosticCode
    evidence: Mapping[str, object]
    repair: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "evidence": dict(self.evidence),
            "repair": self.repair,
        }


class ScoutDiagnosticsError(Exception):
    """Internal transport for one or more typed diagnostics."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("ScoutDiagnosticsError requires at least one diagnostic")
        self.diagnostics = diagnostics
        super().__init__(",".join(diagnostic.code.value for diagnostic in diagnostics))


def foundation_diagnostic(code: DiagnosticCode, **evidence: object) -> Diagnostic:
    """Build one foundation diagnostic with its fixed evidence and repair contract."""
    try:
        expected_keys, repair = _FOUNDATION_SPECS[code]
    except KeyError as exc:
        raise ValueError(f"unsupported foundation diagnostic code: {code!r}") from exc
    if tuple(sorted(evidence)) != tuple(sorted(expected_keys)):
        raise ValueError(
            f"{code.value} evidence keys must be {expected_keys!r}, got {tuple(evidence)!r}"
        )
    return Diagnostic(code=code, evidence=dict(evidence), repair=repair)