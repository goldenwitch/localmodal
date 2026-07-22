#!/usr/bin/env python3
"""Immutable unified source-index generations referenced only by publications."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from txtai import Embeddings

import vine
from diagnostics import DiagnosticCode, ScoutDiagnosticsError, diagnostic
from source_model import SourceRecord


MODEL = "sentence-transformers/all-MiniLM-L6-v2"
INDEX_SCHEMA_VERSION = 1
CHUNK_WORDS = 180
OVERLAP = 30


@dataclass(frozen=True)
class IndexChunk:
    index_id: str
    text: str
    tags: str

    def as_txtai(self) -> tuple[str, str, str]:
        return self.index_id, self.text, self.tags


@dataclass(frozen=True)
class IndexGeneration:
    generation_id: str
    relative_path: str
    sha256: str
    chunk_count: int


def _tags(citation: str, source: str, snapshot: str, **metadata: object) -> str:
    return json.dumps(
        {"citation": citation, "source": source, "snapshot": snapshot, **metadata},
        sort_keys=True,
        separators=(",", ":"),
    )


def _text_chunks(record: SourceRecord, content: Path) -> Iterable[IndexChunk]:
    snapshot = record.snapshot
    assert snapshot is not None
    try:
        words = content.read_text(encoding="utf-8", errors="strict").split()
    except (OSError, UnicodeError) as exc:
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.SOURCE_BINDING_FAILED,
                    source=record.declaration.name,
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
        ) from exc
    step = CHUNK_WORDS - OVERLAP
    for ordinal, start in enumerate(range(0, max(len(words), 1), step)):
        window = words[start:start + CHUNK_WORDS]
        if len(window) < 1:
            continue
        text = " ".join(window)
        citation = f"source:{record.declaration.name}#{snapshot.snapshot_id}#c{ordinal}"
        yield IndexChunk(
            index_id=citation,
            text=text,
            tags=_tags(citation, record.declaration.name, snapshot.snapshot_id),
        )


def _vine_chunks(record: SourceRecord, content: Path, citation_path: str) -> Iterable[IndexChunk]:
    snapshot = record.snapshot
    assert snapshot is not None
    try:
        segments = vine.segments_for_citation_path(content, citation_path)
    except Exception as exc:
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.SOURCE_BINDING_FAILED,
                    source=record.declaration.name,
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
        ) from exc
    for segment in segments:
        index_id = f"source:{record.declaration.name}#{snapshot.snapshot_id}#{segment.index_id}"
        yield IndexChunk(
            index_id=index_id,
            text=segment.text,
            tags=_tags(
                segment.citation,
                record.declaration.name,
                snapshot.snapshot_id,
                vine_kind=segment.kind,
                vine_segment=segment.ordinal,
            ),
        )


def chunks_for_records(resources_root: Path, records: Mapping[str, SourceRecord]) -> list[IndexChunk]:
    """Build all chunks from the exact source snapshots destined for one publication."""
    chunks: list[IndexChunk] = []
    for name, record in sorted(records.items()):
        snapshot = record.snapshot
        if snapshot is None:
            continue
        content = resources_root / snapshot.artifact_path
        if not content.is_file():
            raise ScoutDiagnosticsError(
                (diagnostic(DiagnosticCode.INDEX_MISSING, path=str(content)),)
            )
        origin = record.declaration.origin
        origin_path = getattr(origin, "path", None)
        if isinstance(origin_path, str) and origin_path.endswith(".vine"):
            chunks.extend(_vine_chunks(record, content, origin_path))
        else:
            chunks.extend(_text_chunks(record, content))
    ids = {chunk.index_id for chunk in chunks}
    if len(ids) != len(chunks):
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.INDEX_INTEGRITY_FAILED,
                    index_id="duplicate",
                    detail="generated index ids are not unique",
                ),
            )
        )
    return chunks


def build_generation(resources_root: Path, records: Mapping[str, SourceRecord]) -> IndexGeneration:
    """Build a private immutable index generation; this never flips a reader pointer."""
    generation_id = uuid.uuid4().hex
    relative_path = f".scout-index/generations/{generation_id}"
    generation_dir = resources_root / relative_path
    chunks = chunks_for_records(resources_root, records)
    try:
        if chunks:
            embeddings = Embeddings(path=MODEL, content=True)
            embeddings.index([chunk.as_txtai() for chunk in chunks])
            embeddings.save(str(generation_dir))
            with __import__("contextlib").suppress(Exception):
                embeddings.close()
        else:
            generation_dir.mkdir(parents=True, exist_ok=False)
            (generation_dir / "EMPTY").write_text("0 chunks\n", encoding="utf-8")
        (generation_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": INDEX_SCHEMA_VERSION,
                    "generation_id": generation_id,
                    "chunk_count": len(chunks),
                    "sources": {
                        name: record.snapshot.snapshot_id
                        for name, record in sorted(records.items())
                        if record.snapshot is not None
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        return IndexGeneration(
            generation_id=generation_id,
            relative_path=relative_path,
            sha256=digest_tree(generation_dir),
            chunk_count=len(chunks),
        )
    except Exception:
        shutil.rmtree(generation_dir, ignore_errors=True)
        raise


def validate_generation(resources_root: Path, generation: IndexGeneration) -> Path:
    path = resources_root / generation.relative_path
    if not path.is_dir():
        raise ScoutDiagnosticsError((diagnostic(DiagnosticCode.INDEX_MISSING, path=str(path)),))
    if digest_tree(path) != generation.sha256:
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.INDEX_INTEGRITY_FAILED,
                    index_id=generation.generation_id,
                    detail="generation digest mismatch",
                ),
            )
        )
    try:
        payload = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.INDEX_INTEGRITY_FAILED,
                    index_id=generation.generation_id,
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != INDEX_SCHEMA_VERSION
        or payload.get("generation_id") != generation.generation_id
        or payload.get("chunk_count") != generation.chunk_count
    ):
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.INDEX_INTEGRITY_FAILED,
                    index_id=generation.generation_id,
                    detail="manifest does not match generation reference",
                ),
            )
        )
    return path


def load_generation(path: Path) -> Embeddings | None:
    if (path / "EMPTY").exists():
        return None
    embeddings = Embeddings()
    embeddings.load(str(path))
    return embeddings


def semantic_search(path: Path, query: str, k: int) -> list[dict[str, object]]:
    """Search one validated generation and hydrate canonical tag metadata."""
    embeddings = load_generation(path)
    if embeddings is None:
        return []
    try:
        hits = embeddings.search(query, k)
        return [_hydrate_hit(embeddings, hit) for hit in hits]
    finally:
        with __import__("contextlib").suppress(Exception):
            embeddings.close()


def keyword_search(path: Path, query: str, k: int) -> list[dict[str, object]]:
    """Literal baseline over the same published source generation."""
    embeddings = load_generation(path)
    if embeddings is None:
        return []
    try:
        terms = [term for term in query.lower().split() if len(term) > 2]
        rows = embeddings.search("select id, text, tags from txtai limit 1000000")
        scored = []
        for row in rows:
            text = row["text"].lower()
            if all(term in text for term in terms):
                scored.append((sum(text.count(term) for term in terms), row))
        scored.sort(key=lambda entry: -entry[0])
        return [_hydrate_row(row) for _score, row in scored[:k]]
    finally:
        with __import__("contextlib").suppress(Exception):
            embeddings.close()


def _hydrate_hit(embeddings: Embeddings, hit: dict) -> dict[str, object]:
    row = dict(hit)
    tags_rows = embeddings.search("select tags from txtai where id = :id", parameters={"id": row["id"]})
    if len(tags_rows) != 1:
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.INDEX_INTEGRITY_FAILED,
                    index_id=str(row["id"]),
                    detail="missing tag row",
                ),
            )
        )
    row.update(_parse_tags(tags_rows[0].get("tags"), str(row["id"])))
    return row


def _hydrate_row(row: dict) -> dict[str, object]:
    hydrated = dict(row)
    hydrated.update(_parse_tags(hydrated.get("tags"), str(hydrated.get("id"))))
    return hydrated


def _parse_tags(raw: object, index_id: str) -> dict[str, object]:
    if not isinstance(raw, str):
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.INDEX_INTEGRITY_FAILED,
                    index_id=index_id,
                    detail="tags must be serialized JSON",
                ),
            )
        )
    try:
        tags = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.INDEX_INTEGRITY_FAILED,
                    index_id=index_id,
                    detail=f"JSONDecodeError: {exc}",
                ),
            )
        ) from exc
    if not isinstance(tags, dict) or not isinstance(tags.get("citation"), str):
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.INDEX_INTEGRITY_FAILED,
                    index_id=index_id,
                    detail="tags lack citation",
                ),
            )
        )
    return {"tags": tags, "citation": tags["citation"]}


def digest_tree(root: Path) -> str:
    """Digest a directory by canonical relative names and file bytes."""
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()
