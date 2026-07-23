#!/usr/bin/env python3
"""Generic source ledger, durable journals, and recovery executor leases."""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from config import ScoutConfig
from durable import fsync_directory, replace as durable_replace, unlink as durable_unlink
from diagnostics import DiagnosticCode, ScoutDiagnosticsError, diagnostic, foundation_diagnostic
from source_model import LEDGER_SCHEMA_VERSION, SourceRecord, parse_record, record_to_json


JOURNAL_SCHEMA_VERSION = 1
JOURNAL_PHASES = frozenset(("pending", "claimed", "staged", "failed", "published"))


@dataclass(frozen=True)
class LedgerState:
    records: Mapping[str, SourceRecord]


@dataclass(frozen=True)
class Journal:
    operation_id: str
    mutation: Mapping[str, object]
    phase: str
    claim_id: str | None
    candidate: Mapping[str, object] | None
    publication_id: str | None


class _FileLock(AbstractContextManager["_FileLock"]):
    """One OS-released exclusive file lock with configured bounded waiting."""

    def __init__(self, path: Path, config: ScoutConfig) -> None:
        self.path = path
        self.config = config
        self.file = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+b")
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
            os.fsync(file.fileno())
        deadline = time.monotonic() + self.config.ledger.lock_wait_seconds
        while True:
            try:
                self._lock(file)
                self.file = file
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    file.close()
                    raise ScoutDiagnosticsError(
                        (
                            foundation_diagnostic(
                                DiagnosticCode.LEDGER_BUSY,
                                path=str(self.path),
                                wait_seconds=self.config.ledger.lock_wait_seconds,
                            ),
                        )
                    )
                time.sleep(self.config.ledger.lock_poll_milliseconds / 1000)

    @staticmethod
    def _lock(file) -> None:
        file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(file) -> None:
        file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.file is not None:
            try:
                self._unlock(self.file)
            finally:
                self.file.close()
                self.file = None


class RecoveryLease(AbstractContextManager["RecoveryLease"]):
    """One executor's journal claim, held outside the short ledger lock."""

    def __init__(self, ledger: "Ledger", executor_lock: _FileLock, journal: Journal) -> None:
        self._ledger = ledger
        self._executor_lock = executor_lock
        self.journal = journal
        self.claim_id = journal.claim_id
        if self.claim_id is None:
            raise ValueError("recovery lease requires a claim id")

    def update(
        self,
        phase: str,
        *,
        candidate: Mapping[str, object] | None = None,
        publication_id: str | None = None,
    ) -> Journal:
        if phase not in JOURNAL_PHASES:
            raise ValueError(f"unknown journal phase: {phase!r}")
        with self._ledger._ledger_lock():
            current = self._ledger._read_journal_unlocked()
            self._assert_claim(current)
            updated = Journal(
                operation_id=current.operation_id,
                mutation=current.mutation,
                phase=phase,
                claim_id=self.claim_id,
                candidate=dict(candidate) if candidate is not None else current.candidate,
                publication_id=publication_id if publication_id is not None else current.publication_id,
            )
            self._ledger._write_journal_unlocked(updated)
            self.journal = updated
            return updated

    def replace_mutation(self, mutation: Mapping[str, object]) -> Journal:
        """Persist a finalized mutation plan before the lease performs external work."""
        if not isinstance(mutation, Mapping):
            raise ValueError("journal mutation must be a mapping")
        with self._ledger._ledger_lock():
            current = self._ledger._read_journal_unlocked()
            self._assert_claim(current)
            updated = Journal(
                operation_id=current.operation_id,
                mutation=dict(mutation),
                phase=current.phase,
                claim_id=self.claim_id,
                candidate=current.candidate,
                publication_id=current.publication_id,
            )
            self._ledger._write_journal_unlocked(updated)
            self.journal = updated
            return updated

    def complete(self, records: Mapping[str, SourceRecord], publication_id: str) -> None:
        """Persist ledger state only after the caller has committed publication_id."""
        with self._ledger._ledger_lock():
            current = self._ledger._read_journal_unlocked()
            self._assert_claim(current)
            if current.publication_id != publication_id or current.phase != "published":
                raise ScoutDiagnosticsError(
                    (
                        foundation_diagnostic(
                            DiagnosticCode.LEDGER_RECOVERY_FAILED,
                            operation_id=current.operation_id,
                            detail="publication must be recorded before ledger completion",
                        ),
                    )
                )
            self._ledger._write_state_unlocked(LedgerState(records=dict(records)))
            durable_unlink(self._ledger.journal_path)

    def discard(self) -> None:
        """Discard a private failed operation without mutating the live ledger."""
        with self._ledger._ledger_lock():
            current = self._ledger._read_journal_unlocked()
            self._assert_claim(current)
            durable_unlink(self._ledger.journal_path)

    def read_committed_state(self) -> LedgerState:
        """Read the last committed state while retaining this journal's recovery claim."""
        with self._ledger._ledger_lock():
            current = self._ledger._read_journal_unlocked()
            self._assert_claim(current)
            return self._ledger._read_state_unlocked()

    def register_absent_source(self, record: SourceRecord) -> None:
        """Retain a failed first-fetch declaration without making it reader-visible."""
        if record.snapshot is not None:
            raise ValueError("only no-snapshot records may be registered without publication")
        with self._ledger._ledger_lock():
            current = self._ledger._read_journal_unlocked()
            self._assert_claim(current)
            if current.phase != "claimed":
                raise ScoutDiagnosticsError(
                    (
                        foundation_diagnostic(
                            DiagnosticCode.LEDGER_RECOVERY_FAILED,
                            operation_id=current.operation_id,
                            detail="absent source registration must precede candidate staging",
                        ),
                    )
                )
            state = self._ledger._read_state_unlocked()
            name = record.declaration.name
            if name in state.records:
                raise ScoutDiagnosticsError(
                    (
                        foundation_diagnostic(
                            DiagnosticCode.LEDGER_RECOVERY_FAILED,
                            operation_id=current.operation_id,
                            detail="absent source registration already exists",
                        ),
                    )
                )
            records = dict(state.records)
            records[name] = record
            self._ledger._write_state_unlocked(LedgerState(records=records))
            durable_unlink(self._ledger.journal_path)

    def _assert_claim(self, current: Journal | None) -> None:
        if current is None or current.operation_id != self.journal.operation_id or current.claim_id != self.claim_id:
            raise ScoutDiagnosticsError(
                (
                    foundation_diagnostic(
                        DiagnosticCode.LEDGER_RECOVERY_FAILED,
                        operation_id=self.journal.operation_id,
                        detail="recovery claim was superseded",
                    ),
                )
            )

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._executor_lock.__exit__(exc_type, exc_value, traceback)


class Ledger:
    """The one physical source ledger boundary used by all later control-plane code."""

    def __init__(self, resources_root: Path, config: ScoutConfig) -> None:
        self.resources_root = resources_root
        self.config = config
        self.path = resources_root / ".scout-ledger.json"
        self.journal_path = resources_root / ".scout-ledger.json.journal"
        self.lock_path = resources_root / ".scout-ledger.json.lock"
        self.recovery_lock_path = resources_root / ".scout-ledger.json.recovery.lock"

    def read(self) -> LedgerState:
        """Return committed records, never a view that conflicts with a journal."""
        deadline = time.monotonic() + self.config.ledger.lock_wait_seconds
        while True:
            with self._ledger_lock():
                journal = self._read_journal_unlocked()
                if journal is None:
                    return self._read_state_unlocked()
            if time.monotonic() >= deadline:
                raise ScoutDiagnosticsError(
                    (
                        foundation_diagnostic(
                            DiagnosticCode.LEDGER_BUSY,
                            path=str(self.journal_path),
                            wait_seconds=self.config.ledger.lock_wait_seconds,
                        ),
                    )
                )
            time.sleep(self.config.ledger.lock_poll_milliseconds / 1000)

    def add_if_absent(self, record: SourceRecord) -> bool:
        """Atomically register a source with no existing name; never overwrites."""
        with self._ledger_lock():
            self._ensure_no_journal_unlocked()
            state = self._read_state_unlocked()
            name = record.declaration.name
            if name in state.records:
                return False
            records = dict(state.records)
            records[name] = record
            self._write_state_unlocked(LedgerState(records=records))
            return True

    def begin(self, mutation: Mapping[str, object]) -> Journal:
        """Durably record one update/remove batch before any external work."""
        if not isinstance(mutation, Mapping):
            raise ValueError("journal mutation must be a mapping")
        with self._ledger_lock():
            self._ensure_no_journal_unlocked()
            journal = Journal(
                operation_id=str(uuid.uuid4()),
                mutation=dict(mutation),
                phase="pending",
                claim_id=None,
                candidate=None,
                publication_id=None,
            )
            self._write_journal_unlocked(journal)
            return journal

    def begin_and_claim(self, mutation: Mapping[str, object]) -> RecoveryLease:
        """Create and claim one new journal without an intervening recovery race."""
        if not isinstance(mutation, Mapping):
            raise ValueError("journal mutation must be a mapping")
        executor_lock = _FileLock(self.recovery_lock_path, self.config)
        executor_lock.__enter__()
        try:
            with self._ledger_lock():
                self._ensure_no_journal_unlocked()
                journal = Journal(
                    operation_id=str(uuid.uuid4()),
                    mutation=dict(mutation),
                    phase="claimed",
                    claim_id=str(uuid.uuid4()),
                    candidate=None,
                    publication_id=None,
                )
                self._write_journal_unlocked(journal)
                return RecoveryLease(self, executor_lock, journal)
        except Exception:
            executor_lock.__exit__(None, None, None)
            raise

    def claim_recovery(self) -> RecoveryLease | None:
        """Claim the one pending operation while holding the executor lock."""
        executor_lock = _FileLock(self.recovery_lock_path, self.config)
        executor_lock.__enter__()
        try:
            with self._ledger_lock():
                journal = self._read_journal_unlocked()
                if journal is None:
                    executor_lock.__exit__(None, None, None)
                    return None
                claimed = Journal(
                    operation_id=journal.operation_id,
                    mutation=journal.mutation,
                    phase="claimed" if journal.phase == "pending" else journal.phase,
                    claim_id=str(uuid.uuid4()),
                    candidate=journal.candidate,
                    publication_id=journal.publication_id,
                )
                self._write_journal_unlocked(claimed)
                return RecoveryLease(self, executor_lock, claimed)
        except Exception:
            executor_lock.__exit__(None, None, None)
            raise

    def _ledger_lock(self) -> _FileLock:
        return _FileLock(self.lock_path, self.config)

    def _ensure_no_journal_unlocked(self) -> None:
        if self._read_journal_unlocked() is not None:
            raise ScoutDiagnosticsError(
                (
                    foundation_diagnostic(
                        DiagnosticCode.LEDGER_BUSY,
                        path=str(self.journal_path),
                        wait_seconds=self.config.ledger.lock_wait_seconds,
                    ),
                )
            )

    def _read_state_unlocked(self) -> LedgerState:
        if not self.path.exists():
            return LedgerState(records={})
        payload = self._read_json(self.path, DiagnosticCode.LEDGER_MALFORMED)
        if not isinstance(payload, dict):
            raise self._ledger_malformed("root must be an object")
        if payload.get("schema_version") != LEDGER_SCHEMA_VERSION or set(payload) != {"schema_version", "sources"}:
            if "schema_version" not in payload:
                raise ScoutDiagnosticsError(
                    (diagnostic(DiagnosticCode.LEGACY_MIGRATION_REQUIRED, path=str(self.path)),)
                )
            raise self._ledger_malformed("expected schema_version and sources")
        sources = payload.get("sources")
        if not isinstance(sources, dict):
            raise self._ledger_malformed("sources must be an object")
        records: dict[str, SourceRecord] = {}
        for name, raw_record in sources.items():
            if not isinstance(name, str):
                raise self._ledger_malformed("source map keys must be text")
            record = parse_record(raw_record, f"$.sources.{name}")
            if record.declaration.name != name:
                raise self._ledger_malformed("source map key must equal declaration name")
            records[name] = record
        return LedgerState(records=records)

    def _write_state_unlocked(self, state: LedgerState) -> None:
        payload = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "sources": {name: record_to_json(record) for name, record in sorted(state.records.items())},
        }
        self._atomic_json(self.path, payload)

    def _read_journal_unlocked(self) -> Journal | None:
        if not self.journal_path.exists():
            return None
        payload = self._read_json(self.journal_path, DiagnosticCode.LEDGER_JOURNAL_MALFORMED)
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "operation_id",
            "mutation",
            "phase",
            "claim_id",
            "candidate",
            "publication_id",
        }:
            raise self._journal_malformed("unexpected journal shape")
        if payload.get("schema_version") != JOURNAL_SCHEMA_VERSION:
            raise self._journal_malformed("unsupported journal schema")
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str):
            raise self._journal_malformed("operation_id must be text")
        try:
            uuid.UUID(operation_id)
        except (ValueError, AttributeError) as exc:
            raise self._journal_malformed("operation_id must be UUID") from exc
        mutation = payload.get("mutation")
        if not isinstance(mutation, dict):
            raise self._journal_malformed("mutation must be object")
        phase = payload.get("phase")
        if phase not in JOURNAL_PHASES:
            raise self._journal_malformed("unknown phase")
        claim_id = payload.get("claim_id")
        if claim_id is not None and not isinstance(claim_id, str):
            raise self._journal_malformed("claim_id must be text or null")
        candidate = payload.get("candidate")
        if candidate is not None and not isinstance(candidate, dict):
            raise self._journal_malformed("candidate must be object or null")
        publication_id = payload.get("publication_id")
        if publication_id is not None and not isinstance(publication_id, str):
            raise self._journal_malformed("publication_id must be text or null")
        return Journal(
            operation_id=operation_id,
            mutation=mutation,
            phase=phase,
            claim_id=claim_id,
            candidate=candidate,
            publication_id=publication_id,
        )

    def _write_journal_unlocked(self, journal: Journal) -> None:
        self._atomic_json(
            self.journal_path,
            {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "operation_id": journal.operation_id,
                "mutation": dict(journal.mutation),
                "phase": journal.phase,
                "claim_id": journal.claim_id,
                "candidate": dict(journal.candidate) if journal.candidate is not None else None,
                "publication_id": journal.publication_id,
            },
        )

    def _read_json(self, path: Path, code: DiagnosticCode) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if code is DiagnosticCode.LEDGER_MALFORMED:
                raise self._ledger_malformed(f"{type(exc).__name__}: {exc}") from exc
            raise self._journal_malformed(f"{type(exc).__name__}: {exc}") from exc

    def _ledger_malformed(self, detail: str) -> ScoutDiagnosticsError:
        return ScoutDiagnosticsError(
            (foundation_diagnostic(DiagnosticCode.LEDGER_MALFORMED, path=str(self.path), detail=detail),)
        )

    def _journal_malformed(self, detail: str) -> ScoutDiagnosticsError:
        return ScoutDiagnosticsError(
            (
                foundation_diagnostic(
                    DiagnosticCode.LEDGER_JOURNAL_MALFORMED,
                    path=str(self.journal_path),
                    detail=detail,
                ),
            )
        )

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fsync_directory(path.parent.parent)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(payload, file, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            durable_replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
