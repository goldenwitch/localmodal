#!/usr/bin/env python3
"""Immutable unified source-index generations referenced only by publications."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from txtai import Embeddings

from durable import fsync_tree, replace as durable_replace
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
    final_index_root = resources_root / ".scout-index"
    final_generation_dir = resources_root / relative_path
    stage_root = resources_root / ".scout-index-stage" / generation_id
    stage_index_root = stage_root / ".scout-index"
    generation_dir = stage_index_root / "generations" / generation_id
    chunks = chunks_for_records(resources_root, records)
    published = False
    try:
        generation_dir.parent.mkdir(parents=True, exist_ok=False)
        if chunks:
            embeddings = Embeddings(path=MODEL, content=True)
            embeddings.index([chunk.as_txtai() for chunk in chunks])
            embeddings.save(str(generation_dir))
            with __import__("contextlib").suppress(Exception):
                embeddings.close()
        else:
            generation_dir.mkdir(parents=True, exist_ok=False)
            with (generation_dir / "EMPTY").open("w", encoding="utf-8", newline="\n") as file:
                file.write("0 chunks\n")
                file.flush()
                os.fsync(file.fileno())
        manifest = generation_dir / "manifest.json"
        with manifest.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(
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
                file,
                sort_keys=True,
                separators=(",", ":"),
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        fsync_tree(generation_dir)
        if final_generation_dir.exists():
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.INDEX_INTEGRITY_FAILED,
                        index_id=generation_id,
                        detail="generation id already exists",
                    ),
                )
            )
        if not final_index_root.exists():
            durable_replace(stage_index_root, final_index_root)
        elif not (final_index_root / "generations").exists():
            durable_replace(stage_index_root / "generations", final_index_root / "generations")
        else:
            durable_replace(generation_dir, final_generation_dir)
        shutil.rmtree(stage_root, ignore_errors=True)
        published = True
        return IndexGeneration(
            generation_id=generation_id,
            relative_path=relative_path,
            sha256=digest_tree(final_generation_dir),
            chunk_count=len(chunks),
        )
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        if not published:
            shutil.rmtree(final_generation_dir, ignore_errors=True)
        raise


def discard_generation(resources_root: Path, generation: IndexGeneration) -> None:
    """Remove one private generation that never became publication-reachable."""
    expected = resources_root / ".scout-index" / "generations" / generation.generation_id
    path = resources_root / generation.relative_path
    if path != expected:
        raise ValueError("index generation path does not match its identity")
    shutil.rmtree(path, ignore_errors=True)


def discard_generation_id(resources_root: Path, generation_id: str) -> None:
    if len(generation_id) != 32 or any(character not in "0123456789abcdef" for character in generation_id):
        raise ValueError("index generation id must be lowercase UUID hex")
    shutil.rmtree(resources_root / ".scout-index" / "generations" / generation_id, ignore_errors=True)


def validate_generation_metadata(
    resources_root: Path,
    generation: IndexGeneration,
) -> tuple[Path, Mapping[str, object]]:
    expected_relative_path = f".scout-index/generations/{generation.generation_id}"
    if generation.relative_path != expected_relative_path:
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.INDEX_INTEGRITY_FAILED,
                    index_id=generation.generation_id,
                    detail="generation path does not match its identity",
                ),
            )
        )
    path = resources_root / generation.relative_path
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise ScoutDiagnosticsError((diagnostic(DiagnosticCode.INDEX_MISSING, path=str(path)),))
    except OSError as exc:
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.INDEX_INTEGRITY_FAILED,
                    index_id=generation.generation_id,
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
        ) from exc
    if not stat.S_ISDIR(mode):
        raise ScoutDiagnosticsError((diagnostic(DiagnosticCode.INDEX_MISSING, path=str(path)),))
    try:
        digest = digest_tree(path)
    except OSError as exc:
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.INDEX_INTEGRITY_FAILED,
                    index_id=generation.generation_id,
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
        ) from exc
    if digest != generation.sha256:
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
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
        or not isinstance(payload.get("sources"), dict)
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
    return path, payload


def validate_generation(resources_root: Path, generation: IndexGeneration) -> Path:
    path, payload = validate_generation_metadata(resources_root, generation)
    _validate_tag_bindings(path, generation, payload["sources"])
    return path


def open_validated_generation(
    resources_root: Path,
    generation: IndexGeneration,
) -> tuple[Path, Embeddings | None]:
    """Open one fully validated generation for reuse by a resident query worker."""
    path, payload = validate_generation_metadata(resources_root, generation)
    embeddings = None
    try:
        embeddings = load_generation(path)
        _validate_tag_bindings(path, generation, payload["sources"], embeddings)
    except ScoutDiagnosticsError:
        if embeddings is not None:
            with __import__("contextlib").suppress(Exception):
                embeddings.close()
        raise
    except Exception as exc:
        if embeddings is not None:
            with __import__("contextlib").suppress(Exception):
                embeddings.close()
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.INDEX_INTEGRITY_FAILED,
                    index_id=generation.generation_id,
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
        ) from exc
    return path, embeddings


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
        return semantic_search_loaded(embeddings, query, k)
    finally:
        with __import__("contextlib").suppress(Exception):
            embeddings.close()


def keyword_search(path: Path, query: str, k: int) -> list[dict[str, object]]:
    """Literal baseline over the same published source generation."""
    embeddings = load_generation(path)
    if embeddings is None:
        return []
    try:
        return keyword_search_loaded(embeddings, query, k)
    finally:
        with __import__("contextlib").suppress(Exception):
            embeddings.close()


def semantic_search_loaded(embeddings: Embeddings | None, query: str, k: int) -> list[dict[str, object]]:
    if embeddings is None:
        return []
    hits = embeddings.search(query, k)
    return [_hydrate_hit(embeddings, hit) for hit in hits]


def keyword_search_loaded(embeddings: Embeddings | None, query: str, k: int) -> list[dict[str, object]]:
    if embeddings is None:
        return []
    terms = [term for term in query.lower().split() if len(term) > 2]
    rows = embeddings.search("select id, text, tags from txtai limit 1000000")
    scored = []
    for row in rows:
        text = row["text"].lower()
        if all(term in text for term in terms):
            scored.append((sum(text.count(term) for term in terms), row))
    scored.sort(key=lambda entry: -entry[0])
    return [_hydrate_row(row) for _score, row in scored[:k]]


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


def _validate_tag_bindings(
    path: Path,
    generation: IndexGeneration,
    sources: Mapping[str, object],
    embeddings: Embeddings | None = None,
) -> None:
    if (path / "EMPTY").exists():
        if generation.chunk_count != 0:
            _index_failure(generation.generation_id, "EMPTY generation has chunks")
        return
    owned_embeddings = embeddings is None
    try:
        if embeddings is None:
            embeddings = load_generation(path)
        assert embeddings is not None
        rows = embeddings.search("select id, tags from txtai limit 1000000")
        if len(rows) != generation.chunk_count:
            _index_failure(generation.generation_id, "stored chunk count differs from manifest")
        for row in rows:
            index_id = str(row.get("id"))
            hydrated = _parse_tags(row.get("tags"), index_id)
            tags = hydrated["tags"]
            assert isinstance(tags, dict)
            source = tags.get("source")
            snapshot = tags.get("snapshot")
            if not isinstance(source, str) or not isinstance(snapshot, str):
                _index_failure(index_id, "tag lacks source/snapshot binding")
            if sources.get(source) != snapshot:
                _index_failure(index_id, "tag source/snapshot binding is absent from manifest")
    except ScoutDiagnosticsError:
        raise
    except Exception as exc:
        _index_failure(generation.generation_id, f"{type(exc).__name__}: {exc}")
    finally:
        if owned_embeddings and embeddings is not None:
            with __import__("contextlib").suppress(Exception):
                embeddings.close()


def _index_failure(index_id: str, detail: str) -> None:
    raise ScoutDiagnosticsError(
        (diagnostic(DiagnosticCode.INDEX_INTEGRITY_FAILED, index_id=index_id, detail=detail),)
    )


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
