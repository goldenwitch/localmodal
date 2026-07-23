from __future__ import annotations

from pathlib import Path
from typing import Mapping
from urllib.parse import quote, unquote

import vine
from diagnostics import DiagnosticCode, ScoutDiagnosticsError, diagnostic
from source_model import SourceRecord


class CitationMixin:
    def read_citation(self, citation: str) -> dict[str, object]:
        """Resolve a public citation against the exact committed snapshot."""
        try:
            self._ensure_config()
            assert self.publications is not None
            publication = self.publications.validate_current()
            self._open_publication_embeddings(publication)
        except ScoutDiagnosticsError as exc:
            return {"diagnostics": [item.as_dict() for item in exc.diagnostics]}
        if citation.startswith("source:"):
            return self._read_source_citation(publication.records, citation)
        if citation.endswith("#vine"):
            return self._read_vine_citation(publication.records, citation)
        return {
            "diagnostics": [
                diagnostic(
                    DiagnosticCode.SOURCE_BINDING_FAILED,
                    source="citation",
                    detail="citation is not a source or VINE citation",
                ).as_dict()
            ]
        }

    def _read_source_citation(
        self,
        records: Mapping[str, SourceRecord],
        citation: str,
    ) -> dict[str, object]:
        parts = citation.split("#")
        if len(parts) < 3:
            return self._citation_failure("citation", "source citation must include source, snapshot, and chunk")
        name = parts[0][len("source:"):]
        snapshot_id = parts[1]
        record = records.get(name)
        if record is None or record.snapshot is None or record.snapshot.snapshot_id != snapshot_id:
            return self._citation_failure(name, "citation is not bound to the current publication")
        content = self.resources_root / record.snapshot.artifact_path
        try:
            text = self._read_citation_text(content)
        except (OSError, UnicodeError) as exc:
            return self._citation_failure(name, f"cannot read committed artifact: {type(exc).__name__}: {exc}")
        return {
            "citation": citation,
            "source": name,
            "snapshot": snapshot_id,
            "text": text,
            "diagnostics": [],
        }

    def _read_vine_citation(
        self,
        records: Mapping[str, SourceRecord],
        citation: str,
    ) -> dict[str, object]:
        parts = citation.rsplit("#", 2)
        if len(parts) != 3:
            return self._citation_failure("citation", "malformed VINE citation")
        encoded_path, target, _kind = parts
        try:
            origin_path = unquote(encoded_path)
        except Exception as exc:
            return self._citation_failure("citation", f"invalid VINE path: {exc}")
        for name, record in records.items():
            origin = record.declaration.origin
            path = getattr(origin, "path", None)
            snapshot = record.snapshot
            if not isinstance(path, str) or snapshot is None:
                continue
            if quote(path, safe="/.-_~") != encoded_path:
                continue
            artifact = self.resources_root / snapshot.artifact_path
            try:
                if artifact.stat().st_size > self.VINE_CITATION_PARSE_LIMIT:
                    return self._citation_failure(name, "VINE citation artifact exceeds the read limit")
                blocks = vine.parse_vine(artifact)
            except Exception as exc:
                return self._citation_failure(name, f"cannot parse committed VINE artifact: {type(exc).__name__}: {exc}")
            target_kind = "ref" if target.startswith("ref:") else "task"
            target_id = target[4:] if target_kind == "ref" else target
            matches = [block for block in blocks if block.kind == target_kind and block.block_id == target_id]
            if len(matches) != 1:
                return self._citation_failure(name, "VINE target is absent from committed artifact")
            return {
                "citation": citation,
                "source": name,
                "snapshot": snapshot.snapshot_id,
                "text": self._cap_citation_text(matches[0].projection),
                "diagnostics": [],
            }
        return self._citation_failure("citation", "VINE path is not bound to the current publication")

    @classmethod
    def _read_citation_text(cls, path: Path) -> str:
        with path.open("r", encoding="utf-8", errors="strict") as file:
            return cls._cap_citation_text(file.read(cls.CITATION_TEXT_LIMIT + 1))

    @classmethod
    def _cap_citation_text(cls, text: str) -> str:
        if len(text) <= cls.CITATION_TEXT_LIMIT:
            return text
        return text[:cls.CITATION_TEXT_LIMIT] + "\n[truncated]"

    @staticmethod
    def _citation_failure(source: str, detail: str) -> dict[str, object]:
        return {
            "diagnostics": [
                diagnostic(DiagnosticCode.SOURCE_BINDING_FAILED, source=source, detail=detail).as_dict()
            ]
        }