#!/usr/bin/env python3
"""Structural VINE parsing, citation resolution, and token-safe segmentation.

This module intentionally implements only the subset Scout needs to index and
resolve task/ref blocks. Bacchus remains the authority for full graph semantics.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote_to_bytes

from transformers import AutoTokenizer
from transformers.utils.hub import cached_file


MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_MAGIC = re.compile(r"^vine (1\.(?:0|1|2)\.0)$")
_DEFAULT_DELIMITER = "---"
_ID = r"[A-Za-z0-9-]+(?:/[A-Za-z0-9-]+)*"
_KNOWN_FIELDS = ("@artifact ", "@guidance ", "@file ")
_STATUSES = frozenset(("complete", "started", "reviewing", "planning", "notstarted", "blocked"))
_STATUS = "(?:" + "|".join(sorted(_STATUSES)) + ")"
_ANNOTATION = r"@[A-Za-z][A-Za-z0-9]*\([^)]*\)"
_ANNOTATIONS = rf"(?:\s+{_ANNOTATION})*"
_TASK_HEADER = re.compile(rf"^\[({_ID})\]\s+(.+?)\s+\(({_STATUS})\)({_ANNOTATIONS})$")
_REF_HEADER = re.compile(rf"^ref\s+\[({_ID})\]\s+(.+?)\s+\((\S+)\)({_ANNOTATIONS})$")
_ANNOTATION_PARSE = re.compile(r"\s+@[A-Za-z][A-Za-z0-9]*\(([^)]*)\)")


class VineError(ValueError):
    """A structural VINE document error."""


class CitationResolutionError(VineError):
    """A malformed or unresolvable Scout VINE citation."""


@dataclass(frozen=True)
class VineBlock:
    """A locally parsed task or ref block suitable for indexing."""

    kind: str
    block_id: str
    name: str
    status: str | None
    projection: str


@dataclass(frozen=True)
class VineSegment:
    """A unique index segment that shares a resolver citation with its block."""

    index_id: str
    text: str
    citation: str
    ordinal: int
    token_start: int
    token_end: int
    kind: str


def _valid_id(value: str) -> bool:
    return re.fullmatch(_ID, value) is not None


def _delimiter(lines: list[str]) -> tuple[str, int]:
    if not lines or _MAGIC.fullmatch(lines[0]) is None:
        raise VineError("missing VINE magic header")

    for index, line in enumerate(lines[1:], start=1):
        if line == _DEFAULT_DELIMITER:
            delimiter = _DEFAULT_DELIMITER
            for metadata in lines[1:index]:
                key, separator, value = metadata.partition(":")
                if separator and key.strip() == "delimiter":
                    delimiter = value.strip()
            if not delimiter:
                raise VineError("empty VINE delimiter")
            return delimiter, index

    raise VineError("missing VINE preamble delimiter")


def _blocks(lines: list[str], delimiter: str, start: int) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines[start + 1:]:
        if line == delimiter:
            if any(item.strip() for item in current):
                blocks.append(current)
            current = []
        else:
            current.append(line)
    if any(item.strip() for item in current):
        blocks.append(current)
    return blocks


def _is_attachment(line: str) -> bool:
    return any(line.startswith(prefix) for prefix in _KNOWN_FIELDS)


def _valid_annotations(text: str) -> bool:
    position = 0
    while position < len(text):
        match = _ANNOTATION_PARSE.match(text, position)
        if match is None:
            return False
        values = match.group(1).split(",")
        if not values or any(not value.strip() for value in values):
            return False
        position = match.end()
    return True


def _task_projection(name: str, status: str, body: list[str]) -> str:
    lines = [f"Task: {name}", f"Status: {status}"]
    for raw in body:
        if raw.startswith("-> ") or _is_attachment(raw):
            continue
        if raw.startswith("> "):
            lines.append(f"Decision: {raw[2:]}")
        else:
            lines.append(raw)
    return "\n".join(lines).strip()


def _ref_projection(name: str, body: list[str]) -> str:
    lines = [f"Ref: {name}"]
    for raw in body:
        if _is_attachment(raw):
            raise VineError("reference blocks cannot contain attachment lines")
        if raw.startswith("-> ") or raw.startswith("> "):
            continue
        lines.append(raw)
    return "\n".join(lines).strip()


def parse_vine(path: Path) -> list[VineBlock]:
    """Parse VINE task/ref blocks needed for Scout's local index."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise VineError(f"cannot read {path}: {exc}") from exc

    delimiter, preamble_end = _delimiter(lines)
    parsed: list[VineBlock] = []
    block_ids: set[str] = set()
    for raw_block in _blocks(lines, delimiter, preamble_end):
        header_index = next((i for i, line in enumerate(raw_block) if line.strip()), None)
        if header_index is None:
            continue
        header = raw_block[header_index]
        body = raw_block[header_index + 1:]
        task = _TASK_HEADER.match(header)
        if task:
            block_id, name, status, annotations = task.groups()
            if not _valid_annotations(annotations):
                raise VineError(f"invalid VINE task annotations in {path}: {annotations!r}")
            if block_id in block_ids:
                raise VineError(f"duplicate VINE block id in {path}: {block_id!r}")
            block_ids.add(block_id)
            parsed.append(VineBlock("task", block_id, name, status, _task_projection(name, status, body)))
            continue
        ref = _REF_HEADER.match(header)
        if ref:
            block_id, name, _uri, annotations = ref.groups()
            if not _valid_annotations(annotations):
                raise VineError(f"invalid VINE ref annotations in {path}: {annotations!r}")
            if block_id in block_ids:
                raise VineError(f"duplicate VINE block id in {path}: {block_id!r}")
            block_ids.add(block_id)
            parsed.append(VineBlock("ref", block_id, name, None, _ref_projection(name, body)))
            continue
        raise VineError(f"unrecognized VINE block header in {path}: {header!r}")

    if not parsed:
        raise VineError(f"no task or ref blocks in {path}")
    return parsed


def _relative_vine_path(root: Path, path: Path) -> str:
    root = root.resolve()
    try:
        relative = path.resolve().relative_to(root)
    except ValueError as exc:
        raise CitationResolutionError(f"VINE path escapes repository root: {path}") from exc
    canonical = _canonical_path(root, PurePosixPath(relative.as_posix()), str(path), canonical=True)
    if not canonical.is_file() or canonical.suffix != ".vine":
        raise CitationResolutionError(f"invalid VINE path: {relative.as_posix()}")
    return quote(canonical.relative_to(root).as_posix(), safe="/.-_~")


def citation_for(root: Path, path: Path, block: VineBlock) -> str:
    """Return Scout's canonical resolver citation for one parsed block."""
    target = block.block_id if block.kind == "task" else f"ref:{block.block_id}"
    return f"{_relative_vine_path(root, path)}#{target}#vine"


def _citation_parts(citation: str) -> tuple[PurePosixPath, str, str]:
    parts = citation.rsplit("#", 2)
    if len(parts) != 3 or parts[2] != "vine":
        raise CitationResolutionError(f"malformed VINE citation: {citation!r}")
    path_text, target, _kind = parts
    if (not path_text or not target or "\\" in path_text or
            any(ord(character) < 0x20 or ord(character) == 0x7F for character in path_text)):
        raise CitationResolutionError(f"malformed VINE citation: {citation!r}")
    try:
        decoded_path = unquote_to_bytes(path_text).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CitationResolutionError(f"invalid VINE citation path: {path_text!r}") from exc
    if (not decoded_path or "\\" in decoded_path or decoded_path.startswith("./") or
            any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded_path) or
            quote(decoded_path, safe="/.-_~") != path_text):
        raise CitationResolutionError(f"invalid VINE citation path: {path_text!r}")
    path = PurePosixPath(decoded_path)
    if (path.is_absolute() or path.suffix != ".vine" or not path.parts or
            any(part in ("", ".", "..") or ":" in part for part in path.parts) or
            path.as_posix() != decoded_path):
        raise CitationResolutionError(f"invalid VINE citation path: {path_text!r}")
    if target.startswith("ref:"):
        target_kind, target_id = "ref", target[4:]
    else:
        target_kind, target_id = "task", target
    if not _valid_id(target_id):
        raise CitationResolutionError(f"invalid VINE citation target: {target!r}")
    return path, target_kind, target_id


def _canonical_path(root: Path, relative: PurePosixPath, context: str, *, canonical: bool) -> Path:
    """Resolve a relative path with exact on-disk spelling, even on Windows."""
    current = root
    parts = relative.parts
    for index, part in enumerate(parts):
        if not current.is_dir():
            return current.joinpath(*parts[index:])
        entries = {entry.name: entry for entry in current.iterdir()}
        exact = entries.get(part)
        if exact is not None:
            current = exact
            continue
        if canonical and any(name.casefold() == part.casefold() for name in entries):
            raise CitationResolutionError(f"noncanonical VINE citation path: {context!r}")
        return current.joinpath(*parts[index:])
    return current


def resolve_citation(root: Path, citation: str) -> VineBlock:
    """Resolve a canonical Scout citation to one local task or ref block."""
    relative, target_kind, target_id = _citation_parts(citation)
    root = root.resolve()
    path = _canonical_path(root, relative, citation, canonical=True)
    if not path.is_file():
        raise CitationResolutionError(f"VINE citation file is absent: {relative.as_posix()}")
    try:
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CitationResolutionError(
            f"VINE citation escapes repository root: {relative.as_posix()}"
        ) from exc

    matches = [block for block in parse_vine(resolved_path)
               if block.kind == target_kind and block.block_id == target_id]
    if len(matches) != 1:
        label = f"{target_kind}:{target_id}" if target_kind == "ref" else target_id
        raise CitationResolutionError(f"VINE citation target is absent or ambiguous: {label}")
    return matches[0]


@lru_cache(maxsize=1)
def _tokenizer_settings() -> tuple[object, int]:
    config_path = cached_file(MODEL, "sentence_bert_config.json")
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    sequence_limit = config.get("max_seq_length")
    if not isinstance(sequence_limit, int) or sequence_limit <= 0:
        raise VineError(f"invalid sentence-transformer max_seq_length: {sequence_limit!r}")
    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    budget = sequence_limit - tokenizer.num_special_tokens_to_add(pair=False)
    if budget <= 0:
        raise VineError("embedding sequence limit cannot fit special tokens")
    return tokenizer, sequence_limit


def effective_token_limit() -> int:
    """The configured embedding limit including special tokens."""
    _tokenizer, limit = _tokenizer_settings()
    return limit


def _encoded(tokenizer: object, text: str, specials: bool) -> list[int]:
    return tokenizer(text, add_special_tokens=specials, truncation=False, verbose=False)["input_ids"]


def _segments(block: VineBlock, citation: str) -> list[VineSegment]:
    tokenizer, sequence_limit = _tokenizer_settings()
    encoded = tokenizer(block.projection, add_special_tokens=False,
                        return_offsets_mapping=True, truncation=False, verbose=False)
    ids = encoded["input_ids"]
    offsets = encoded["offset_mapping"]
    if not ids:
        raise VineError(f"empty searchable VINE projection: {citation}")
    content_budget = sequence_limit - tokenizer.num_special_tokens_to_add(pair=False)
    segments: list[VineSegment] = []
    start = 0
    ordinal = 0
    while start < len(ids):
        end = min(start + content_budget, len(ids))
        if end < len(ids):
            minimum_forward = min(end, start + 31)
            for boundary in range(end, minimum_forward - 1, -1):
                gap = block.projection[offsets[boundary - 1][1]:offsets[boundary][0]]
                if any(character.isspace() for character in gap):
                    end = boundary
                    break

        while end > start:
            text = block.projection[offsets[start][0]:offsets[end - 1][1]]
            if len(_encoded(tokenizer, text, specials=True)) <= sequence_limit:
                break
            end -= 1
        if end <= start:
            raise VineError(f"cannot fit VINE projection token at {citation}")

        index_id = f"{citation}#s{ordinal}"
        segments.append(VineSegment(index_id, text, citation, ordinal, start, end, block.kind))
        if end == len(ids):
            break
        overlap = min(30, end - start - 1)
        start = end - overlap
        ordinal += 1
    return segments


def segments_for_vine(root: Path, path: Path) -> list[VineSegment]:
    """Return token-safe index segments for all task/ref blocks in one VINE."""
    segments: list[VineSegment] = []
    for block in parse_vine(path):
        segments.extend(_segments(block, citation_for(root, path, block)))
    return segments


def segments_for_citation_path(path: Path, citation_path: str) -> list[VineSegment]:
    """Segment an artifact while preserving its declared repository citation path."""
    encoded_path = quote(citation_path, safe="/.-_~")
    segments: list[VineSegment] = []
    for block in parse_vine(path):
        target = block.block_id if block.kind == "task" else f"ref:{block.block_id}"
        segments.extend(_segments(block, f"{encoded_path}#{target}#vine"))
    return segments