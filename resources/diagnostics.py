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
    ORIGIN_INVALID = "ORIGIN_INVALID"
    ORIGIN_NOT_FOUND = "ORIGIN_NOT_FOUND"
    SOURCE_NAME_INVALID = "SOURCE_NAME_INVALID"
    SOURCE_ROW_INVALID = "SOURCE_ROW_INVALID"
    DESTINATION_DENIED = "DESTINATION_DENIED"
    DESTINATION_RESOLUTION_FAILED = "DESTINATION_RESOLUTION_FAILED"
    FETCH_CONNECT_FAILED = "FETCH_CONNECT_FAILED"
    FETCH_REDIRECT_DENIED = "FETCH_REDIRECT_DENIED"
    FETCH_RESPONSE_LIMIT_EXCEEDED = "FETCH_RESPONSE_LIMIT_EXCEEDED"
    FETCH_MIME_MISMATCH = "FETCH_MIME_MISMATCH"
    FETCH_CHARSET_INVALID = "FETCH_CHARSET_INVALID"
    FETCH_UTF8_INVALID = "FETCH_UTF8_INVALID"
    MATERIALIZATION_FAILED = "MATERIALIZATION_FAILED"
    PUBLICATION_MISSING = "PUBLICATION_MISSING"
    PUBLICATION_MALFORMED = "PUBLICATION_MALFORMED"
    PUBLICATION_INTEGRITY_FAILED = "PUBLICATION_INTEGRITY_FAILED"
    INDEX_MISSING = "INDEX_MISSING"
    INDEX_INTEGRITY_FAILED = "INDEX_INTEGRITY_FAILED"
    SOURCE_BINDING_FAILED = "SOURCE_BINDING_FAILED"
    LEGACY_MIGRATION_REQUIRED = "LEGACY_MIGRATION_REQUIRED"
    BOOTSTRAP_AFTER_ACTIVATION = "BOOTSTRAP_AFTER_ACTIVATION"


class WarningCode(StrEnum):
    """Typed non-invalidating observations for a valid current publication."""

    SNAPSHOT_STALE = "SNAPSHOT_STALE"
    REFRESH_FAILED = "REFRESH_FAILED"


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

_SOURCE_SPECS: dict[DiagnosticCode, tuple[tuple[str, ...], str]] = {
    DiagnosticCode.ORIGIN_INVALID: (
        ("path", "detail"),
        "Repair the source origin locator at the reported path.",
    ),
    DiagnosticCode.ORIGIN_NOT_FOUND: (
        ("path",),
        "Add or restore the declared repository-local origin file.",
    ),
    DiagnosticCode.SOURCE_NAME_INVALID: (
        ("name", "rule"),
        "Use a source name that satisfies the reported rule.",
    ),
    DiagnosticCode.SOURCE_ROW_INVALID: (
        ("path", "detail"),
        "Repair the reported source proposal row.",
    ),
    DiagnosticCode.DESTINATION_DENIED: (
        ("host", "address", "reason"),
        "Use an origin that resolves only to admitted public connection targets.",
    ),
    DiagnosticCode.DESTINATION_RESOLUTION_FAILED: (
        ("host", "detail"),
        "Repair the origin hostname or its DNS resolution.",
    ),
    DiagnosticCode.FETCH_CONNECT_FAILED: (
        ("host", "address", "detail"),
        "Repair the origin availability and retry materialization.",
    ),
    DiagnosticCode.FETCH_REDIRECT_DENIED: (
        ("url", "reason"),
        "Use an origin whose redirects satisfy Scout admission policy.",
    ),
    DiagnosticCode.FETCH_RESPONSE_LIMIT_EXCEEDED: (
        ("url", "limit_bytes"),
        "Use a response within the configured byte limit.",
    ),
    DiagnosticCode.FETCH_MIME_MISMATCH: (
        ("expected", "observed"),
        "Align the declaration MIME with the admitted response MIME.",
    ),
    DiagnosticCode.FETCH_CHARSET_INVALID: (
        ("charset",),
        "Serve UTF-8 text without an incompatible charset declaration.",
    ),
    DiagnosticCode.FETCH_UTF8_INVALID: (
        ("url", "detail"),
        "Serve valid UTF-8 text at the declared origin.",
    ),
    DiagnosticCode.MATERIALIZATION_FAILED: (
        ("source", "detail"),
        "Repair the declared origin and retry materialization.",
    ),
    DiagnosticCode.PUBLICATION_MISSING: (
        ("path",),
        "Restore or create the required publication pointer or generation.",
    ),
    DiagnosticCode.PUBLICATION_MALFORMED: (
        ("path", "detail"),
        "Repair the malformed publication manifest or pointer.",
    ),
    DiagnosticCode.PUBLICATION_INTEGRITY_FAILED: (
        ("publication_id", "detail"),
        "Repair the publication component identified by the integrity check.",
    ),
    DiagnosticCode.INDEX_MISSING: (
        ("path",),
        "Restore or rebuild the indexed material referenced by the publication.",
    ),
    DiagnosticCode.INDEX_INTEGRITY_FAILED: (
        ("index_id", "detail"),
        "Rebuild the indexed material generation and republish it.",
    ),
    DiagnosticCode.SOURCE_BINDING_FAILED: (
        ("source", "detail"),
        "Repair the source snapshot binding and republish the source state.",
    ),
    DiagnosticCode.LEGACY_MIGRATION_REQUIRED: (
        ("path",),
        "Run the source migration before using the source control plane.",
    ),
    DiagnosticCode.BOOTSTRAP_AFTER_ACTIVATION: (
        ("path",),
        "Use source_cli.py propose or refresh-stale after source control has activated.",
    ),
}

_DIAGNOSTIC_SPECS = {**_FOUNDATION_SPECS, **_SOURCE_SPECS}

_WARNING_SPECS: dict[WarningCode, tuple[tuple[str, ...], str]] = {
    WarningCode.SNAPSHOT_STALE: (
        ("source", "snapshot", "ttl_days", "overdue_days"),
        "Run refresh-stale to materialize a current snapshot.",
    ),
    WarningCode.REFRESH_FAILED: (
        ("source", "snapshot", "detail"),
        "Repair the declared origin and run refresh-stale again.",
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


@dataclass(frozen=True)
class Warning:
    """One serializable non-invalidating source observation."""

    code: WarningCode
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


def diagnostic(code: DiagnosticCode, **evidence: object) -> Diagnostic:
    """Build one typed diagnostic with its fixed evidence and repair contract."""
    try:
        expected_keys, repair = _DIAGNOSTIC_SPECS[code]
    except KeyError as exc:
        raise ValueError(f"unsupported diagnostic code: {code!r}") from exc
    if tuple(sorted(evidence)) != tuple(sorted(expected_keys)):
        raise ValueError(
            f"{code.value} evidence keys must be {expected_keys!r}, got {tuple(evidence)!r}"
        )
    return Diagnostic(code=code, evidence=dict(evidence), repair=repair)


def foundation_diagnostic(code: DiagnosticCode, **evidence: object) -> Diagnostic:
    """Build one foundation diagnostic, rejecting non-foundation discriminators."""
    if code not in _FOUNDATION_SPECS:
        raise ValueError(f"unsupported foundation diagnostic code: {code!r}")
    return diagnostic(code, **evidence)


def warning(code: WarningCode, **evidence: object) -> Warning:
    """Build one typed warning with its fixed evidence and repair contract."""
    try:
        expected_keys, repair = _WARNING_SPECS[code]
    except KeyError as exc:
        raise ValueError(f"unsupported warning code: {code!r}") from exc
    if tuple(sorted(evidence)) != tuple(sorted(expected_keys)):
        raise ValueError(
            f"{code.value} evidence keys must be {expected_keys!r}, got {tuple(evidence)!r}"
        )
    return Warning(code=code, evidence=dict(evidence), repair=repair)