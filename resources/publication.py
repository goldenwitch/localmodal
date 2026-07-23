#!/usr/bin/env python3
"""Immutable source publications and the sole master reader pointer."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from activation import activation_path, is_source_control_active, transition_lock
from config import ScoutConfig
from diagnostics import Diagnostic, DiagnosticCode, ScoutDiagnosticsError, diagnostic
from durable import fsync_directory, fsync_tree, replace as durable_replace
from ledger import _FileLock
from source_index import IndexGeneration, validate_generation, validate_generation_metadata
from source_model import SourceRecord, parse_record, record_to_json


PUBLICATION_SCHEMA_VERSION = 1
PUBLICATION_ID = re.compile(r"[0-9a-f]{32}\Z")


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
        self.activation_path = activation_path(resources_root)
        self.lock_path = self.root / "LOCK"

    def is_activated(self) -> bool:
        return is_source_control_active(self.resources_root)

    def current_id(self) -> str | None:
        try:
            value = self.current_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise self._malformed(self.current_path, f"{type(exc).__name__}: {exc}") from exc
        if not value:
            return None
        return self._validated_publication_id(value, self.current_path)

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
        stage_root = self.resources_root / ".scout-publications-stage" / publication_id
        stage_publication_root = stage_root / ".scout-publications"
        stage_directory = stage_publication_root / "generations" / publication_id
        stage_directory.mkdir(parents=True, exist_ok=False)
        try:
            self._atomic_json(stage_directory / "manifest.json", self._to_json(publication))
            fsync_tree(stage_directory)
            if directory.exists():
                raise ScoutDiagnosticsError(
                    (
                        diagnostic(
                            DiagnosticCode.PUBLICATION_INTEGRITY_FAILED,
                            publication_id=publication_id,
                            detail="publication id already exists",
                        ),
                    )
                )
            if not self.root.exists():
                durable_replace(stage_publication_root, self.root)
            elif not self.generations.exists():
                durable_replace(stage_publication_root / "generations", self.generations)
            else:
                durable_replace(stage_directory, directory)
            shutil.rmtree(stage_root, ignore_errors=True)
            return publication
        except Exception:
            shutil.rmtree(stage_root, ignore_errors=True)
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def activate(self, publication: Publication, *, expected_parent: str | None) -> bool:
        """Flip master truth only if no competing publication won first."""
        with _FileLock(self.lock_path, self.config):
            with transition_lock(self.resources_root):
                if self.current_id() != expected_parent:
                    return False
                loaded = self.load(publication.publication_id)
                self.validate(loaded)
                self.root.mkdir(parents=True, exist_ok=True)
                fsync_directory(self.root.parent)
                if not self.activation_path.is_file():
                    self._atomic_text(self.activation_path, "source-control-v1\n")
                temporary = self.current_path.with_name("CURRENT.tmp")
                with temporary.open("w", encoding="utf-8", newline="\n") as file:
                    file.write(publication.publication_id + "\n")
                    file.flush()
                    os.fsync(file.fileno())
                durable_replace(temporary, self.current_path)
                return True

    def discard_candidate(self, publication: Publication) -> None:
        """Remove a private manifest that did not become the master publication."""
        if self.current_id() == publication.publication_id:
            return
        shutil.rmtree(self.generations / publication.publication_id, ignore_errors=True)

    def discard_candidate_id(self, publication_id: str) -> None:
        """Remove one validated private candidate directory when its manifest cannot load."""
        publication_id = self._validated_publication_id(publication_id, self.generations)
        if self.current_id() == publication_id:
            return
        shutil.rmtree(self.generations / publication_id, ignore_errors=True)

    def load_current(self) -> Publication:
        publication_id = self.current_id()
        if publication_id is None:
            raise ScoutDiagnosticsError(
                (diagnostic(DiagnosticCode.PUBLICATION_MISSING, path=str(self.current_path)),)
            )
        return self.load(publication_id)

    def load(self, publication_id: str) -> Publication:
        publication_id = self._validated_publication_id(publication_id, self.generations)
        manifest = self.generations / publication_id / "manifest.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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

    def _validated_publication_id(self, publication_id: object, path: Path) -> str:
        if not isinstance(publication_id, str) or PUBLICATION_ID.fullmatch(publication_id) is None:
            raise self._malformed(path, "publication id must be lowercase UUID hex")
        return publication_id

    def validate_current(self) -> Publication:
        publication = self.load_current()
        self.validate(publication)
        return publication

    def validate(self, publication: Publication) -> None:
        diagnostics: list[Diagnostic] = []
        if publication.parent_id is not None and (
            not isinstance(publication.parent_id, str)
            or PUBLICATION_ID.fullmatch(publication.parent_id) is None
        ):
            diagnostics.append(
                self._integrity_diagnostic(
                    publication.publication_id,
                    "parent_id must be lowercase UUID hex or null",
                )
            )
        diagnostics.extend(self._record_diagnostics(publication.records, publication.publication_id))
        index_path: Path | None = None
        try:
            index_path, _index_payload = validate_generation_metadata(
                self.resources_root,
                publication.index,
            )
        except ScoutDiagnosticsError as exc:
            diagnostics.extend(exc.diagnostics)
        expected_sources = {
            name: record.snapshot.snapshot_id
            for name, record in publication.records.items()
            if record.snapshot is not None
        }
        if index_path is not None:
            index_manifest = index_path / "manifest.json"
            try:
                payload = json.loads(index_manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                diagnostics.append(
                    self._integrity_diagnostic(
                        publication.publication_id,
                        f"cannot read index manifest: {type(exc).__name__}: {exc}",
                    )
                )
            else:
                if payload.get("sources") != expected_sources:
                    diagnostics.append(
                        self._integrity_diagnostic(
                            publication.publication_id,
                            "index source snapshot bindings mismatch publication",
                        )
                    )
        if diagnostics:
            raise ScoutDiagnosticsError(tuple(diagnostics))

    def _validate_records(
        self,
        records: Mapping[str, SourceRecord],
        publication_id: str = "candidate",
    ) -> None:
        diagnostics = self._record_diagnostics(records, publication_id)
        if diagnostics:
            raise ScoutDiagnosticsError(tuple(diagnostics))

    def _record_diagnostics(
        self,
        records: Mapping[str, SourceRecord],
        publication_id: str,
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        live_vine_paths: dict[str, list[str]] = {}
        for name, record in sorted(records.items()):
            if record.declaration.name != name:
                diagnostics.append(
                    self._integrity_diagnostic(
                        publication_id,
                        "source map key differs from declaration name",
                    )
                )
                continue
            origin_path = getattr(record.declaration.origin, "path", None)
            if (
                isinstance(origin_path, str)
                and origin_path not in self.config.repo_files.publishable_paths
            ):
                diagnostics.append(
                    self._binding_diagnostic(
                        name,
                        "repo-file origin is no longer on the checked-in publishable path allowlist",
                    )
                )
            snapshot = record.snapshot
            if snapshot is None:
                continue
            if isinstance(origin_path, str) and origin_path.endswith(".vine"):
                live_vine_paths.setdefault(origin_path, []).append(name)
            if snapshot.observed_mime != record.declaration.mime:
                diagnostics.append(
                    self._binding_diagnostic(name, "observed MIME differs from declaration MIME")
                )
            expected = f"scout-source--{name}/generations/{snapshot.snapshot_id}/content"
            if snapshot.artifact_path != expected:
                diagnostics.append(
                    self._binding_diagnostic(name, "artifact path does not match source/snapshot generation")
                )
                continue
            content = self.resources_root / snapshot.artifact_path
            if not content.is_file():
                diagnostics.append(diagnostic(DiagnosticCode.PUBLICATION_MISSING, path=str(content)))
                continue
            try:
                digest = _digest_file(content)
                byte_count = content.stat().st_size
            except OSError as exc:
                diagnostics.append(
                    self._binding_diagnostic(name, f"cannot read artifact: {type(exc).__name__}: {exc}")
                )
                continue
            if digest != snapshot.sha256:
                diagnostics.append(self._binding_diagnostic(name, "artifact digest mismatch"))
            if byte_count != snapshot.byte_count:
                diagnostics.append(self._binding_diagnostic(name, "artifact byte count mismatch"))
        for path, names in sorted(live_vine_paths.items()):
            if len(names) > 1:
                for name in names:
                    diagnostics.append(
                        self._binding_diagnostic(
                            name,
                            f"live VINE repository path is bound to multiple source identities: {path}",
                        )
                    )
        return diagnostics

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
        if parent_id is not None and (
            not isinstance(parent_id, str) or PUBLICATION_ID.fullmatch(parent_id) is None
        ):
            raise self._malformed(manifest, "parent_id must be lowercase UUID hex or null")
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
            generation_id = _publication_id(raw_index["generation_id"], "index generation_id")
            index = IndexGeneration(
                generation_id=generation_id,
                relative_path=_index_relative_path(raw_index["relative_path"], generation_id),
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
        raise ScoutDiagnosticsError((PublicationStore._integrity_diagnostic(publication_id, detail),))

    @staticmethod
    def _integrity_diagnostic(publication_id: str, detail: str) -> Diagnostic:
        return diagnostic(
            DiagnosticCode.PUBLICATION_INTEGRITY_FAILED,
            publication_id=publication_id,
            detail=detail,
        )

    @staticmethod
    def _binding(source: str, detail: str) -> None:
        raise ScoutDiagnosticsError((PublicationStore._binding_diagnostic(source, detail),))

    @staticmethod
    def _binding_diagnostic(source: str, detail: str) -> Diagnostic:
        return diagnostic(DiagnosticCode.SOURCE_BINDING_FAILED, source=source, detail=detail)

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
            durable_replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _atomic_text(path: Path, text: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            durable_replace(temporary, path)
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


def _publication_id(value: object, name: str) -> str:
    text = _text(value, name)
    if PUBLICATION_ID.fullmatch(text) is None:
        raise ValueError(f"{name} must be lowercase UUID hex")
    return text


def _index_relative_path(value: object, generation_id: str) -> str:
    path = _text(value, "index relative_path")
    expected = f".scout-index/generations/{generation_id}"
    if path != expected:
        raise ValueError(f"index relative_path must equal {expected}")
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
