"""What the instruments look at, and what they call what they find.

Stated once here so the two instruments measure the same population.
"""

from __future__ import annotations

from pathlib import Path

# The trees that hold this repository's own modules.
SCANNED_TREES: tuple[str, ...] = ("resources", "scout")

# Modules directly inside this tree are imported by bare name, so their names
# carry no package prefix.
FLAT_NAMESPACE = "resources"

# Public capabilities and the module each one enters through. A capability's
# entry module counts as served by it.
#
# source_search and source_mutate enter through the same two dispatch modules,
# so module-level reachability cannot separate them and reports one set for
# both. Separating them needs method-level tracing, which this instrument does
# not do; the over-approximation is declared here rather than hidden.
CAPABILITIES: dict[str, tuple[str, ...]] = {
    "web_search": ("scout/server",),
    "legacy_aliases": ("search",),
    "vendor_fetch": ("fetch_modal_docs", "fetch_papers", "fetch_vscode_docs"),
    "cli_migrate": ("source_migration",),
    "source_search": ("source_cli", "source_worker"),
    "source_mutate": ("source_cli", "source_worker"),
}


def iter_module_files(root: Path, trees: tuple[str, ...] = SCANNED_TREES) -> list[Path]:
    """Every Python file in the measured trees, in a stable order."""
    files: list[Path] = []
    for tree in trees:
        files.extend(sorted((root / tree).rglob("*.py")))
    return files


def module_name(root: Path, path: Path, flat: str = FLAT_NAMESPACE) -> str:
    """The name a module is reported under: its path without suffix, with the
    flat namespace's prefix dropped. A package's __init__ keeps its own name,
    so source_control/__init__ never reads as the package source_control."""
    parts = path.relative_to(root).with_suffix("").parts
    if parts and parts[0] == flat:
        parts = parts[1:]
    return "/".join(parts)
