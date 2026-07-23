#!/usr/bin/env python3
"""CLI for private bootstrap and public Scout source management commands."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from diagnostics import ScoutDiagnosticsError
from source_migration import DEFAULT_MANIFEST, bootstrap as bootstrap_migration, write_manifest
from source_control import SourceControl


def _rows(path: Path) -> list[object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read rows file {path}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit(f"rows file {path} must contain a JSON array")
    return payload


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    bootstrap = subcommands.add_parser("bootstrap", help="private initial source-bound publication")
    bootstrap.add_argument("rows", type=Path, help="explicit JSON source-row array")
    migrate = subcommands.add_parser("migrate", help="private legacy import into the initial source publication")
    migrate.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    write = subcommands.add_parser("write-migration-manifest", help="regenerate the checked-in explicit migration manifest")
    write.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    propose = subcommands.add_parser("propose", help="public explicit add/remove proposal")
    propose.add_argument("rows", type=Path, help="explicit JSON source-row array")
    subcommands.add_parser("refresh-stale", help="re-materialize stale or absent sources")
    search = subcommands.add_parser("search", help="validated unified source search")
    search.add_argument("query")
    search.add_argument("--k", type=int, default=6)
    read = subcommands.add_parser("read", help="resolve a citation against the committed source snapshot")
    read.add_argument("citation")
    args = parser.parse_args(argv)
    try:
        if args.command == "write-migration-manifest":
            write_manifest(args.manifest)
            _print({"manifest": str(args.manifest)})
            return 0
        if args.command == "migrate":
            result = bootstrap_migration(args.manifest).as_dict()
        else:
            control = SourceControl()
            if args.command == "bootstrap":
                result = control.bootstrap(_rows(args.rows)).as_dict()
            elif args.command == "propose":
                result = control.propose(_rows(args.rows)).as_dict()
            elif args.command == "refresh-stale":
                result = control.refresh_stale().as_dict()
            elif args.command == "read":
                result = control.read_citation(args.citation)
            else:
                result = control.search(args.query, max(1, min(args.k, 20)))
    except ScoutDiagnosticsError as exc:
        _print({"diagnostics": [item.as_dict() for item in exc.diagnostics]})
        return 1
    _print(result)
    if isinstance(result, dict) and "outcomes" in result:
        statuses = [outcome.get("status") for outcome in result.get("outcomes", []) if isinstance(outcome, dict)]
        return 0 if not result.get("diagnostics") and all(status in {"published", "removed", "not_found"} for status in statuses) else 1
    return 0 if not result.get("diagnostics") else 1


if __name__ == "__main__":
    raise SystemExit(main())
