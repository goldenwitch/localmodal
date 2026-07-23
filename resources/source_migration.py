#!/usr/bin/env python3
"""Generate and apply the one-time explicit initial Scout source manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from diagnostics import DiagnosticCode, ScoutDiagnosticsError, diagnostic
from source_control import BatchResult, SourceControl


MANIFEST_SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[1]
RESOURCES = Path(__file__).resolve().parent
DEFAULT_MANIFEST = RESOURCES / "initial-sources.json"
REPOSITORY_FILES = (
    "README.md",
    "localmodal.vine",
    "human-owned-spec/initial-spec.md",
    "proposals/scout-source-management.vine",
    "proposals/scout-vocabulary.md",
    "resources/papers.md",
    "scout/README.md",
)
VSCODE_URLS = {
    "language-models.md": "https://raw.githubusercontent.com/microsoft/vscode-docs/main/docs/agent-customization/language-models.md",
    "mcp-servers.md": "https://raw.githubusercontent.com/microsoft/vscode-docs/main/docs/agent-customization/mcp-servers.md",
}


@dataclass(frozen=True)
class InitialManifest:
    rows: list[object]
    imports: dict[int, Path]


def _name(prefix: str, identity: str) -> str:
    # The explicit manifest verifies uniqueness after deriving this stable readable hash name.
    return f"{prefix}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:56]}"


def generate_manifest(repository_root: Path = ROOT, resources_root: Path = RESOURCES) -> dict[str, object]:
    """Build the exact retained-source inventory from fixed legacy roots."""
    rows: list[dict[str, object]] = []
    imports: dict[str, str] = {}
    names: set[str] = set()

    def add(row: dict[str, object], import_path: Path | None = None) -> None:
        name = row["name"]
        assert isinstance(name, str)
        if name in names:
            raise RuntimeError(f"initial source name collision: {name}")
        names.add(name)
        index = len(rows)
        rows.append(row)
        if import_path is not None:
            imports[str(index)] = import_path.relative_to(repository_root).as_posix()

    for relative in REPOSITORY_FILES:
        path = repository_root / relative
        if not path.is_file():
            raise RuntimeError(f"retained repository source absent: {relative}")
        suffix = path.suffix.casefold()
        mime = "text/markdown" if suffix == ".md" else "text/plain"
        add(
            {
                "op": "add",
                "name": _name("repo", relative),
                "origin": {"kind": "repo-file", "path": relative},
                "mime": mime,
                "ttl_days": 1,
            }
        )

    modal_root = resources_root / "modal-docs"
    for path in sorted(modal_root.glob("**/*.md")):
        relative = path.relative_to(modal_root).as_posix()
        url = f"https://modal.com/docs/{relative}"
        add(
            {
                "op": "add",
                "name": _name("modal", url),
                "origin": {"kind": "https", "url": url},
                "mime": "text/markdown",
                "ttl_days": 30,
            },
            path,
        )

    vscode_root = resources_root / "vscode-docs"
    for relative, url in sorted(VSCODE_URLS.items()):
        path = vscode_root / relative
        if not path.is_file():
            raise RuntimeError(f"retained VS Code source absent: {relative}")
        add(
            {
                "op": "add",
                "name": _name("vscode", url),
                "origin": {"kind": "https", "url": url},
                "mime": "text/plain",
                "ttl_days": 30,
            },
            path,
        )

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "rows": rows,
        "imports": imports,
    }


def write_manifest(path: Path = DEFAULT_MANIFEST) -> None:
    payload = generate_manifest()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_manifest(path: Path, repository_root: Path = ROOT) -> InitialManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.LEGACY_MIGRATION_REQUIRED,
                    path=str(path),
                ),
            )
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "rows", "imports"}:
        raise ScoutDiagnosticsError(
            (diagnostic(DiagnosticCode.LEGACY_MIGRATION_REQUIRED, path=str(path)),)
        )
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION or not isinstance(payload.get("rows"), list):
        raise ScoutDiagnosticsError(
            (diagnostic(DiagnosticCode.LEGACY_MIGRATION_REQUIRED, path=str(path)),)
        )
    raw_imports = payload.get("imports")
    if not isinstance(raw_imports, dict):
        raise ScoutDiagnosticsError(
            (diagnostic(DiagnosticCode.LEGACY_MIGRATION_REQUIRED, path=str(path)),)
        )
    imports: dict[int, Path] = {}
    root = repository_root.resolve()
    for raw_index, raw_path in raw_imports.items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ScoutDiagnosticsError(
                (diagnostic(DiagnosticCode.LEGACY_MIGRATION_REQUIRED, path=str(path)),)
            ) from exc
        if not isinstance(raw_path, str) or index < 0 or index >= len(payload["rows"]):
            raise ScoutDiagnosticsError(
                (diagnostic(DiagnosticCode.LEGACY_MIGRATION_REQUIRED, path=str(path)),)
            )
        if "\\" in raw_path:
            raise ScoutDiagnosticsError(
                (diagnostic(DiagnosticCode.LEGACY_MIGRATION_REQUIRED, path=str(path)),)
            )
        relative = PurePosixPath(raw_path)
        if (
            relative.is_absolute()
            or relative.as_posix() != raw_path
            or any(part in ("", ".", "..") or ":" in part for part in relative.parts)
        ):
            raise ScoutDiagnosticsError(
                (diagnostic(DiagnosticCode.LEGACY_MIGRATION_REQUIRED, path=str(path)),)
            )
        candidate = root.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError, ValueError) as exc:
            raise ScoutDiagnosticsError(
                (diagnostic(DiagnosticCode.LEGACY_MIGRATION_REQUIRED, path=str(path)),)
            ) from exc
        if not resolved.is_file():
            continue
        imports[index] = resolved
    return InitialManifest(rows=payload["rows"], imports=imports)


def bootstrap(path: Path = DEFAULT_MANIFEST) -> BatchResult:
    manifest = load_manifest(path)
    return SourceControl().bootstrap_import(manifest.rows, manifest.imports)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("write-manifest", "bootstrap"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    if args.command == "write-manifest":
        write_manifest(args.manifest)
        print(args.manifest)
        return 0
    result = bootstrap(args.manifest)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
