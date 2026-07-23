#!/usr/bin/env python3
"""Durable non-live refresh attempt observations for valid source publications."""
from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from diagnostics import Diagnostic, Warning, WarningCode, warning
from durable import fsync_directory, replace as durable_replace, unlink as durable_unlink
from source_model import SourceRecord


ATTEMPT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RefreshAttempt:
    source: str
    snapshot_id: str
    observed_at: str
    detail: str


class AttemptStore:
    """Stores warnings separately from snapshots and publications."""

    def __init__(self, resources_root: Path) -> None:
        self.root = resources_root / ".scout-attempts"

    def record_refresh_failure(self, record: SourceRecord, diagnostics: Iterable[Diagnostic]) -> None:
        snapshot = record.snapshot
        if snapshot is None:
            return
        detail = "; ".join(item.code.value for item in diagnostics) or "materialization failed"
        self.record_refresh_failure_detail(record.declaration.name, snapshot.snapshot_id, detail)

    def record_refresh_failure_detail(self, source: str, snapshot_id: str, detail: str) -> None:
        attempt = RefreshAttempt(
            source=source,
            snapshot_id=snapshot_id,
            observed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            detail=detail,
        )
        self._atomic_json(
            self._path(attempt.source),
            {
                "schema_version": ATTEMPT_SCHEMA_VERSION,
                "source": attempt.source,
                "snapshot_id": attempt.snapshot_id,
                "observed_at": attempt.observed_at,
                "detail": attempt.detail,
            },
        )

    def clear(self, source: str) -> None:
        durable_unlink(self._path(source))

    def warning_for(self, record: SourceRecord) -> Warning | None:
        snapshot = record.snapshot
        if snapshot is None:
            return None
        path = self._path(record.declaration.name)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "schema_version", "source", "snapshot_id", "observed_at", "detail"
            }:
                raise ValueError("unexpected attempt shape")
            if payload["schema_version"] != ATTEMPT_SCHEMA_VERSION:
                raise ValueError("unsupported attempt schema")
            if payload["source"] != record.declaration.name or payload["snapshot_id"] != snapshot.snapshot_id:
                return None
            detail = payload["detail"]
            if not isinstance(detail, str):
                raise ValueError("detail must be text")
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            return warning(
                WarningCode.REFRESH_FAILED,
                source=record.declaration.name,
                snapshot=snapshot.snapshot_id,
                detail=f"attempt record {type(exc).__name__}: {exc}",
            )
        return warning(
            WarningCode.REFRESH_FAILED,
            source=record.declaration.name,
            snapshot=snapshot.snapshot_id,
            detail=detail,
        )

    def warnings_for(self, records: Mapping[str, SourceRecord], now: datetime | None = None) -> list[Warning]:
        now = now or datetime.now(timezone.utc)
        warnings: list[Warning] = []
        for name, record in sorted(records.items()):
            snapshot = record.snapshot
            ttl = record.declaration.ttl_days
            if snapshot is not None and ttl is not None:
                materialized = datetime.fromisoformat(snapshot.materialized_at.replace("Z", "+00:00"))
                overdue_seconds = (now - materialized).total_seconds() - ttl * 86_400
                if overdue_seconds > 0:
                    warnings.append(
                        warning(
                            WarningCode.SNAPSHOT_STALE,
                            source=name,
                            snapshot=snapshot.snapshot_id,
                            ttl_days=ttl,
                            overdue_days=math.ceil(overdue_seconds / 86_400),
                        )
                    )
            refresh_failure = self.warning_for(record)
            if refresh_failure is not None:
                warnings.append(refresh_failure)
        return warnings

    def _path(self, source: str) -> Path:
        return self.root / f"{source}.json"

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fsync_directory(path.parent.parent)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(payload, file, sort_keys=True, separators=(",", ":"))
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            durable_replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
