#!/usr/bin/env python3
"""Immutable source publications and the sole master reader pointer."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from config import ScoutConfig
from diagnostics import DiagnosticCode, ScoutDiagnosticsError, diagnostic
from ledger import _FileLock
from source_index import IndexGeneration, validate_generation
from source_model import SourceRecord, parse_record, record_to_json


PUBLICATION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Publication:
    publication_id: str
    parent_id: str | None
    records: Mapping[str, SourceRecord]
    index: IndexGeneration


class PublicationStore:
    """Write candidate manifests privately and activate only via master CURRENT."""

    def __init__(self, resources_root: Path, config: ScoutConfig) -> None:
        self.resources_root = resources_root
        self.config = config
        self.root = resources_root / ".scout-publications"
        self.generations = self.root / "generations"
        self.current_path = self.root / "CURRENT"
        self.lock_path = self.root / "LOCK"

    def current_id(self) -> str | None:
        try:
            value = self.current_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def create_candidate(
        self,
        records: Mapping[str, SourceRecord],
        index: IndexGeneration,
        *,
        parent_id: str | None,
    ) -> Publication:
        self._validate_records(records)
        validate_generation(self.resources_root, index)
        publication_id = uuid.uuid4().hex
        publication = Publication(
            publication_id=publication_id,
            parent_id=parent_id,
            records=dict(records),
            index=index,
        )
        directory = self.generations / publication_id
        directory.mkdir(parents=True, exist_ok=False)
        try:
            self._atomic_json(directory / "manifest.json", self._to_json(publication))
            return publication
        except Exception:
            for item in directory.iterdir():
                item.unlink(missing_ok=True)
            directory.rmdir()
            raise

    def activate(self, publication: Publication, *, expected_parent: str | None) -> bool:
        """Flip master truth only if no competing publication won first."""
        with _FileLock(self.lock_path, self.config):
            if self.current_id() != expected_parent:
                return False
            loaded = self.load(publication.publication_id)
            self.validate(loaded)
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.current_path.with_name("CURRENT.tmp")
            temporary.write_text(publication.publication_id + "\n", encoding="utf-8")
            os.replace(temporary, self.current_path)
            return True

    def load_current(self) -> Publication:
        publication_id = self.current_id()
        if publication_id is None:
            raise ScoutDiagnosticsError(
                (diagnostic(DiagnosticCode.PUBLICATION_MISSING, path=str(self.current_path)),)
            )
        return self.load(publication_id)

    def load(self, publication_id: str) -> Publication:
        manifest = self.generations / publication_id / "manifest.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.PUBLICATION_MALFORMED,
                        path=str(manifest),
                        detail=f"{type(exc).__name__}: {exc}",
                    ),
                )
            ) from exc
        return self._from_json(payload, manifest, publication_id)

    def validate_current(self) -> Publication:
        publication = self.load_current()
        self.validate(publication)
        return publication

    def validate(self, publication: Publication) -> None:
        if publication.parent_id is not None and not isinstance(publication.parent_id, str):
            self._integrity(publication, "parent_id must be text or null")
        self._validate_records(publication.records, publication.publication_id)
        validate_generation(self.resources_root, publication.index)
        expected_sources = {
            name: record.snapshot.snapshot_id
            for name, record in publication.records.items()
            if record.snapshot is not None
        }
        index_manifest = self.resources_root / publication.index.relative_path / "manifest.json"
        try:
            payload = json.loads(index_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._integrity(publication, f"cannot read index manifest: {type(exc).__name__}: {exc}")
            return
        if payload.get("sources") != expected_sources:
            self._integrity(publication, "index source snapshot bindings mismatch publication")

    def _validate_records(
        self,
        records: Mapping[str, SourceRecord],
        publication_id: str = "candidate",
    ) -> None:
        for name, record in records.items():
            if record.declaration.name != name:
                self._integrity_id(publication_id, "source map key differs from declaration name")
            snapshot = record.snapshot
            if snapshot is None:
                continue
            expected = f"scout-source--{name}/generations/{snapshot.snapshot_id}/content"
            if snapshot.artifact_path != expected:
                self._binding(name, "artifact path does not match source/snapshot generation")
            content = self.resources_root / snapshot.artifact_path
            if not content.is_file():
                raise ScoutDiagnosticsError((diagnostic(DiagnosticCode.PUBLICATION_MISSING, path=str(content)),))
            digest = _digest_file(content)
            if digest != snapshot.sha256:
                self._binding(name, "artifact digest mismatch")
            if content.stat().st_size != snapshot.byte_count:
                self._binding(name, "artifact byte count mismatch")

    def _to_json(self, publication: Publication) -> dict[str, object]:
        return {
            "schema_version": PUBLICATION_SCHEMA_VERSION,
            "publication_id": publication.publication_id,
            "parent_id": publication.parent_id,
            "sources": {name: record_to_json(record) for name, record in sorted(publication.records.items())},
            "index": {
                "generation_id": publication.index.generation_id,
                "relative_path": publication.index.relative_path,
                "sha256": publication.index.sha256,
                "chunk_count": publication.index.chunk_count,
            },
        }

    def _from_json(self, payload: object, manifest: Path, expected_id: str) -> Publication:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "publication_id",
            "parent_id",
            "sources",
            "index",
        }:
            raise self._malformed(manifest, "unexpected manifest shape")
        if payload.get("schema_version") != PUBLICATION_SCHEMA_VERSION:
            raise self._malformed(manifest, "unsupported publication schema")
        publication_id = payload.get("publication_id")
        if not isinstance(publication_id, str) or publication_id != expected_id:
            raise self._malformed(manifest, "publication_id does not match generation path")
        parent_id = payload.get("parent_id")
        if parent_id is not None and not isinstance(parent_id, str):
            raise self._malformed(manifest, "parent_id must be text or null")
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, dict):
            raise self._malformed(manifest, "sources must be object")
        records: dict[str, SourceRecord] = {}
        for name, raw in raw_sources.items():
            if not isinstance(name, str):
                raise self._malformed(manifest, "source names must be text")
            record = parse_record(raw, f"$.sources.{name}")
            if record.declaration.name != name:
                raise self._malformed(manifest, "source map key differs from declaration name")
            records[name] = record
        raw_index = payload.get("index")
        if not isinstance(raw_index, dict) or set(raw_index) != {
            "generation_id",
            "relative_path",
            "sha256",
            "chunk_count",
        }:
            raise self._malformed(manifest, "invalid index reference")
        try:
            index = IndexGeneration(
                generation_id=_text(raw_index["generation_id"], "index generation_id"),
                relative_path=_relative_path(raw_index["relative_path"]),
                sha256=_sha256(raw_index["sha256"]),
                chunk_count=_nonnegative_int(raw_index["chunk_count"], "index chunk_count"),
            )
        except ValueError as exc:
            raise self._malformed(manifest, str(exc)) from exc
        return Publication(publication_id=publication_id, parent_id=parent_id, records=records, index=index)

    def _malformed(self, path: Path, detail: str) -> ScoutDiagnosticsError:
        return ScoutDiagnosticsError(
            (diagnostic(DiagnosticCode.PUBLICATION_MALFORMED, path=str(path), detail=detail),)
        )

    def _integrity(self, publication: Publication, detail: str) -> None:
        self._integrity_id(publication.publication_id, detail)

    @staticmethod
    def _integrity_id(publication_id: str, detail: str) -> None:
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.PUBLICATION_INTEGRITY_FAILED,
                    publication_id=publication_id,
                    detail=detail,
                ),
            )
        )

    @staticmethod
    def _binding(source: str, detail: str) -> None:
        raise ScoutDiagnosticsError(
            (diagnostic(DiagnosticCode.SOURCE_BINDING_FAILED, source=source, detail=detail),)
        )

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(payload, file, sort_keys=True, separators=(",", ":"))
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be nonempty text")
    return value


def _relative_path(value: object) -> str:
    path = _text(value, "index relative_path")
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("index relative_path must be relative")
    return path


def _sha256(value: object) -> str:
    text = _text(value, "index sha256")
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError("index sha256 must be lowercase hex")
    return text


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be nonnegative integer")
    return value
