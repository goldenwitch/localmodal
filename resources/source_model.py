#!/usr/bin/env python3
"""Strict source declarations and durable source-state serialization."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from diagnostics import DiagnosticCode, ScoutDiagnosticsError, diagnostic


LEDGER_SCHEMA_VERSION = 1
SOURCE_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
ALLOWED_MIME_TYPES = frozenset(("text/plain", "text/markdown"))
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@dataclass(frozen=True)
class HttpsOrigin:
    url: str


@dataclass(frozen=True)
class RepoFileOrigin:
    path: str


Origin = HttpsOrigin | RepoFileOrigin


@dataclass(frozen=True)
class SourceDeclaration:
    name: str
    origin: Origin
    mime: str
    ttl_days: int | None


@dataclass(frozen=True)
class SourceSnapshot:
    snapshot_id: str
    materialized_at: str
    artifact_path: str
    sha256: str
    byte_count: int
    observed_mime: str
    origin_evidence: Mapping[str, object]


@dataclass(frozen=True)
class SourceRecord:
    declaration: SourceDeclaration
    snapshot: SourceSnapshot | None


@dataclass(frozen=True)
class AddRow:
    declaration: SourceDeclaration


@dataclass(frozen=True)
class RemoveRow:
    name: str


SourceRow = AddRow | RemoveRow


def _diagnostic_error(code: DiagnosticCode, **evidence: object) -> ScoutDiagnosticsError:
    return ScoutDiagnosticsError((diagnostic(code, **evidence),))


def _object(value: object, path: str, keys: tuple[str, ...], code: DiagnosticCode) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _diagnostic_error(code, path=path, detail="must be an object")
    actual = set(value)
    expected = set(keys)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = f"keys must be {sorted(expected)!r}"
        if missing:
            detail += f"; missing {missing!r}"
        if extra:
            detail += f"; unknown {extra!r}"
        raise _diagnostic_error(code, path=path, detail=detail)
    return value


def _valid_name(name: object) -> str:
    if not isinstance(name, str) or SOURCE_NAME.fullmatch(name) is None:
        raise _diagnostic_error(
            DiagnosticCode.SOURCE_NAME_INVALID,
            name=name if isinstance(name, str) else repr(name),
            rule="must match [a-z][a-z0-9-]{0,63}",
        )
    return name


def _valid_mime(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in ALLOWED_MIME_TYPES:
        observed = value if isinstance(value, str) else type(value).__name__
        raise _diagnostic_error(
            DiagnosticCode.FETCH_MIME_MISMATCH,
            expected="text/plain or text/markdown",
            observed=observed,
        )
    return value


def _valid_ttl(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _diagnostic_error(
            DiagnosticCode.SOURCE_ROW_INVALID,
            row="ttl_days",
            detail="must be null or a positive integer",
        )
    return value


def parse_origin(value: object, path: str = "$.origin") -> Origin:
    payload = _object(value, path, ("kind", "url") if isinstance(value, dict) and value.get("kind") == "https" else ("kind", "path"), DiagnosticCode.ORIGIN_INVALID)
    kind = payload.get("kind")
    if kind == "https":
        url = payload.get("url")
        if not isinstance(url, str) or not url or url != url.strip():
            raise _diagnostic_error(DiagnosticCode.ORIGIN_INVALID, path=path, detail="https url must be nonempty text")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise _diagnostic_error(DiagnosticCode.ORIGIN_INVALID, path=path, detail=str(exc)) from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port is not None and not (0 < port <= 65535)
        ):
            raise _diagnostic_error(
                DiagnosticCode.ORIGIN_INVALID,
                path=path,
                detail="must be credential-free absolute https URL without fragment",
            )
        normalized = urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))
        return HttpsOrigin(url=normalized)
    if kind == "repo-file":
        raw_path = payload.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise _diagnostic_error(DiagnosticCode.ORIGIN_INVALID, path=path, detail="repo-file path must be nonempty text")
        if "\\" in raw_path:
            raise _diagnostic_error(DiagnosticCode.ORIGIN_INVALID, path=path, detail="repo-file path must use POSIX separators")
        candidate = PurePosixPath(raw_path)
        if (
            candidate.is_absolute()
            or candidate.as_posix() != raw_path
            or any(part in ("", ".", "..") or ":" in part for part in candidate.parts)
        ):
            raise _diagnostic_error(DiagnosticCode.ORIGIN_INVALID, path=path, detail="repo-file path must be normalized relative POSIX path")
        return RepoFileOrigin(path=raw_path)
    raise _diagnostic_error(DiagnosticCode.ORIGIN_INVALID, path=path, detail="kind must be https or repo-file")


def parse_declaration(value: object, path: str = "$") -> SourceDeclaration:
    payload = _object(value, path, ("name", "origin", "mime", "ttl_days"), DiagnosticCode.SOURCE_ROW_INVALID)
    return SourceDeclaration(
        name=_valid_name(payload["name"]),
        origin=parse_origin(payload["origin"], f"{path}.origin"),
        mime=_valid_mime(payload["mime"], f"{path}.mime"),
        ttl_days=_valid_ttl(payload["ttl_days"]),
    )


def resolve_repo_file(origin: RepoFileOrigin, repository_root: Path) -> Path:
    root = repository_root.resolve()
    candidate = root.joinpath(*PurePosixPath(origin.path).parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _diagnostic_error(DiagnosticCode.ORIGIN_NOT_FOUND, path=origin.path) from exc
    if not resolved.is_file():
        raise _diagnostic_error(DiagnosticCode.ORIGIN_NOT_FOUND, path=origin.path)
    return resolved


def artifact_root(resources_root: Path, name: str) -> Path:
    """Return the unique stable artifact root for a valid source name."""
    return resources_root / f"scout-source--{_valid_name(name)}"


def origin_to_json(origin: Origin) -> dict[str, str]:
    if isinstance(origin, HttpsOrigin):
        return {"kind": "https", "url": origin.url}
    return {"kind": "repo-file", "path": origin.path}


def declaration_to_json(declaration: SourceDeclaration) -> dict[str, object]:
    return {
        "name": declaration.name,
        "origin": origin_to_json(declaration.origin),
        "mime": declaration.mime,
        "ttl_days": declaration.ttl_days,
    }


def snapshot_to_json(snapshot: SourceSnapshot) -> dict[str, object]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "materialized_at": snapshot.materialized_at,
        "artifact_path": snapshot.artifact_path,
        "sha256": snapshot.sha256,
        "byte_count": snapshot.byte_count,
        "observed_mime": snapshot.observed_mime,
        "origin_evidence": dict(snapshot.origin_evidence),
    }


def parse_snapshot(value: object, path: str = "$.snapshot") -> SourceSnapshot:
    payload = _object(
        value,
        path,
        (
            "snapshot_id",
            "materialized_at",
            "artifact_path",
            "sha256",
            "byte_count",
            "observed_mime",
            "origin_evidence",
        ),
        DiagnosticCode.LEDGER_MALFORMED,
    )
    snapshot_id = payload["snapshot_id"]
    if not isinstance(snapshot_id, str) or SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise _diagnostic_error(DiagnosticCode.LEDGER_MALFORMED, path=path, detail="invalid snapshot_id")
    materialized_at = payload["materialized_at"]
    if not isinstance(materialized_at, str):
        raise _diagnostic_error(DiagnosticCode.LEDGER_MALFORMED, path=path, detail="materialized_at must be text")
    try:
        datetime.fromisoformat(materialized_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _diagnostic_error(DiagnosticCode.LEDGER_MALFORMED, path=path, detail="invalid materialized_at") from exc
    artifact_path = payload["artifact_path"]
    if not isinstance(artifact_path, str) or not artifact_path:
        raise _diagnostic_error(DiagnosticCode.LEDGER_MALFORMED, path=path, detail="artifact_path must be text")
    artifact = PurePosixPath(artifact_path)
    if artifact.is_absolute() or any(part in ("", ".", "..") or ":" in part for part in artifact.parts):
        raise _diagnostic_error(DiagnosticCode.LEDGER_MALFORMED, path=path, detail="artifact_path must be relative")
    sha256 = payload["sha256"]
    if not isinstance(sha256, str) or SHA256.fullmatch(sha256) is None:
        raise _diagnostic_error(DiagnosticCode.LEDGER_MALFORMED, path=path, detail="invalid sha256")
    byte_count = payload["byte_count"]
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise _diagnostic_error(DiagnosticCode.LEDGER_MALFORMED, path=path, detail="invalid byte_count")
    observed_mime = _valid_mime(payload["observed_mime"], f"{path}.observed_mime")
    evidence = payload["origin_evidence"]
    if not isinstance(evidence, dict):
        raise _diagnostic_error(DiagnosticCode.LEDGER_MALFORMED, path=path, detail="origin_evidence must be object")
    return SourceSnapshot(
        snapshot_id=snapshot_id,
        materialized_at=materialized_at,
        artifact_path=artifact_path,
        sha256=sha256,
        byte_count=byte_count,
        observed_mime=observed_mime,
        origin_evidence=dict(evidence),
    )


def record_to_json(record: SourceRecord) -> dict[str, object]:
    return {
        "declaration": declaration_to_json(record.declaration),
        "snapshot": snapshot_to_json(record.snapshot) if record.snapshot else None,
    }


def parse_record(value: object, path: str = "$") -> SourceRecord:
    payload = _object(value, path, ("declaration", "snapshot"), DiagnosticCode.LEDGER_MALFORMED)
    declaration = parse_declaration(payload["declaration"], f"{path}.declaration")
    snapshot_raw = payload["snapshot"]
    snapshot = None if snapshot_raw is None else parse_snapshot(snapshot_raw, f"{path}.snapshot")
    return SourceRecord(declaration=declaration, snapshot=snapshot)


def parse_row(value: object, row_index: int) -> SourceRow:
    path = f"$[{row_index}]"
    if not isinstance(value, dict):
        raise _diagnostic_error(DiagnosticCode.SOURCE_ROW_INVALID, path=path, detail="must be an object")
    operation = value.get("op")
    if operation == "add":
        declaration = parse_declaration({key: value.get(key) for key in ("name", "origin", "mime", "ttl_days")}, path)
        if set(value) != {"op", "name", "origin", "mime", "ttl_days"}:
            raise _diagnostic_error(DiagnosticCode.SOURCE_ROW_INVALID, path=path, detail="add must carry exactly op, name, origin, mime, ttl_days")
        return AddRow(declaration=declaration)
    if operation == "remove":
        if set(value) != {"op", "name"}:
            raise _diagnostic_error(DiagnosticCode.SOURCE_ROW_INVALID, path=path, detail="remove must carry exactly op and name")
        return RemoveRow(name=_valid_name(value.get("name")))
    raise _diagnostic_error(DiagnosticCode.SOURCE_ROW_INVALID, path=path, detail="op must be add or remove")