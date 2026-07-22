#!/usr/bin/env python3
"""Strict checked-in operational configuration for Scout."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from diagnostics import DiagnosticCode, ScoutDiagnosticsError, foundation_diagnostic


CONFIG_PATH = Path(__file__).with_name("scout.json")


@dataclass(frozen=True)
class LedgerConfig:
    lock_wait_seconds: int
    lock_poll_milliseconds: int


@dataclass(frozen=True)
class FetchConfig:
    request_timeout_seconds: int
    max_redirects: int
    max_response_bytes: int


@dataclass(frozen=True)
class ScoutConfig:
    schema_version: int
    ledger: LedgerConfig
    fetch: FetchConfig


class _Validator:
    def __init__(self) -> None:
        self.diagnostics = []

    def missing(self, path: str) -> None:
        self.diagnostics.append(
            foundation_diagnostic(DiagnosticCode.CONFIG_MISSING, path=path)
        )

    def unknown_key(self, path: str, key: str) -> None:
        self.diagnostics.append(
            foundation_diagnostic(DiagnosticCode.CONFIG_UNKNOWN_KEY, path=path, key=key)
        )

    def wrong_type(self, path: str, expected: str, value: object) -> None:
        self.diagnostics.append(
            foundation_diagnostic(
                DiagnosticCode.CONFIG_WRONG_TYPE,
                path=path,
                expected=expected,
                actual=type(value).__name__,
            )
        )

    def invalid_value(self, path: str, rule: str, value: object) -> None:
        self.diagnostics.append(
            foundation_diagnostic(
                DiagnosticCode.CONFIG_INVALID_VALUE,
                path=path,
                rule=rule,
                value=value,
            )
        )

    def object(self, value: object, path: str, keys: tuple[str, ...]) -> dict[str, object] | None:
        if not isinstance(value, dict):
            self.wrong_type(path, "object", value)
            return None
        for key in value:
            if key not in keys:
                self.unknown_key(path, key)
        for key in keys:
            if key not in value:
                self.missing(f"{path}.{key}")
        return value

    def integer(self, value: object, path: str) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            self.wrong_type(path, "integer", value)
            return None
        return value


def _load_json(path: Path) -> object:
    if not path.exists():
        raise ScoutDiagnosticsError(
            (foundation_diagnostic(DiagnosticCode.CONFIG_MISSING, path=str(path)),)
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoutDiagnosticsError(
            (
                foundation_diagnostic(
                    DiagnosticCode.CONFIG_MALFORMED,
                    path=str(path),
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
        ) from exc


def load_config(path: Path = CONFIG_PATH) -> ScoutConfig:
    """Load the exact v1 Scout config or raise typed aggregate diagnostics."""
    payload = _load_json(path)
    validator = _Validator()
    root = validator.object(payload, "$", ("schema_version", "ledger", "fetch"))
    if root is None:
        raise ScoutDiagnosticsError(tuple(validator.diagnostics))

    schema_version = validator.integer(root.get("schema_version"), "$.schema_version")
    if schema_version is not None and schema_version != 1:
        validator.invalid_value("$.schema_version", "must equal 1", schema_version)

    ledger_raw = validator.object(
        root.get("ledger"), "$.ledger", ("lock_wait_seconds", "lock_poll_milliseconds")
    )
    fetch_raw = validator.object(
        root.get("fetch"),
        "$.fetch",
        ("request_timeout_seconds", "max_redirects", "max_response_bytes"),
    )

    wait = poll = timeout = redirects = response_bytes = None
    if ledger_raw is not None:
        wait = validator.integer(ledger_raw.get("lock_wait_seconds"), "$.ledger.lock_wait_seconds")
        poll = validator.integer(
            ledger_raw.get("lock_poll_milliseconds"), "$.ledger.lock_poll_milliseconds"
        )
        if wait is not None and wait <= 0:
            validator.invalid_value("$.ledger.lock_wait_seconds", "must be positive", wait)
        if poll is not None and poll <= 0:
            validator.invalid_value("$.ledger.lock_poll_milliseconds", "must be positive", poll)
        if wait is not None and poll is not None and wait > 0 and poll > wait * 1000:
            validator.invalid_value(
                "$.ledger.lock_poll_milliseconds",
                "must not exceed lock_wait_seconds * 1000",
                poll,
            )

    if fetch_raw is not None:
        timeout = validator.integer(
            fetch_raw.get("request_timeout_seconds"), "$.fetch.request_timeout_seconds"
        )
        redirects = validator.integer(fetch_raw.get("max_redirects"), "$.fetch.max_redirects")
        response_bytes = validator.integer(
            fetch_raw.get("max_response_bytes"), "$.fetch.max_response_bytes")
        if timeout is not None and timeout <= 0:
            validator.invalid_value("$.fetch.request_timeout_seconds", "must be positive", timeout)
        if redirects is not None and redirects < 0:
            validator.invalid_value("$.fetch.max_redirects", "must be nonnegative", redirects)
        if response_bytes is not None and response_bytes <= 0:
            validator.invalid_value("$.fetch.max_response_bytes", "must be positive", response_bytes)

    if validator.diagnostics:
        raise ScoutDiagnosticsError(tuple(validator.diagnostics))

    assert schema_version == 1
    assert wait is not None and poll is not None
    assert timeout is not None and redirects is not None and response_bytes is not None
    return ScoutConfig(
        schema_version=schema_version,
        ledger=LedgerConfig(lock_wait_seconds=wait, lock_poll_milliseconds=poll),
        fetch=FetchConfig(
            request_timeout_seconds=timeout,
            max_redirects=redirects,
            max_response_bytes=response_bytes,
        ),
    )