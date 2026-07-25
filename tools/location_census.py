"""Location census: which modules deduce where they are, and which are told.

Prints nothing unless its known-answer fixture passes first.

Deriving a location is reading __file__. Being told one is taking a `root`
parameter annotated Path. A rebind -- assigning to __file__ -- is counted
separately, because it makes a module assert a position that is not its own.

    python -m tools.location_census [--root PATH]
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

from . import fixture, tree


@dataclass(frozen=True)
class Census:
    modules: tuple[str, ...]
    derives: dict[str, int]
    rebinds: dict[str, int]
    told: dict[str, int]

    @property
    def free(self) -> frozenset[str]:
        return frozenset(
            module
            for module in self.modules
            if module not in self.derives and module not in self.told
        )


def build(root: Path, trees: tuple[str, ...], flat: str) -> Census:
    derives: dict[str, int] = {}
    rebinds: dict[str, int] = {}
    told: dict[str, int] = {}
    modules: list[str] = []
    for path in tree.iter_module_files(root, trees):
        name = tree.module_name(root, path, flat)
        modules.append(name)
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        counts = _counts(module)
        for target, count in zip((derives, rebinds, told), counts):
            if count:
                target[name] = count
    return Census(tuple(sorted(modules)), derives, rebinds, told)


def _counts(module: ast.Module) -> tuple[int, int, int]:
    reads = writes = injected = 0
    for node in ast.walk(module):
        if isinstance(node, ast.Name) and node.id == "__file__":
            if isinstance(node.ctx, ast.Store):
                writes += 1
            else:
                reads += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            injected += _root_parameters(node.args)
    return reads, writes, injected


def _root_parameters(args: ast.arguments) -> int:
    """Parameters naming a root and annotated with a Path."""
    every = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    for optional in (args.vararg, args.kwarg):
        if optional is not None:
            every.append(optional)
    return sum(
        1
        for arg in every
        if arg.arg.endswith("root")
        and arg.annotation is not None
        and "Path" in ast.unparse(arg.annotation)
    )


def fixture_problems() -> list[str]:
    with fixture.materialized() as root:
        census = build(root, fixture.TREES, fixture.FLAT)
        problems = fixture.diff("modules", fixture.MODULES, frozenset(census.modules))
        problems += fixture.diff("derives", fixture.EXPECTED_DERIVES, census.derives)
        problems += fixture.diff("rebinds", fixture.EXPECTED_REBINDS, census.rebinds)
        problems += fixture.diff("told", fixture.EXPECTED_TOLD, census.told)
        problems += fixture.diff("location-free", fixture.EXPECTED_FREE, census.free)
    return problems


def report(census: Census) -> None:
    both = sorted(set(census.derives) & set(census.told))
    print(f"modules {len(census.modules)}   derive {len(census.derives)}   "
          f"told {len(census.told)}   free {len(census.free)}   "
          f"rebind __file__ {len(census.rebinds)}")

    print("\nderive their location (__file__ reads)")
    for module, count in sorted(census.derives.items(), key=lambda item: (-item[1], item[0])):
        mark = "  [also told]" if module in census.told else ""
        print(f"  {count:3}  {module}{mark}")

    print("\ntold their location (root parameters annotated Path)")
    for module, count in sorted(census.told.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {count:3}  {module}")

    if census.rebinds:
        print("\nrebind __file__ (a module asserting a position that is not its own)")
        for module, count in sorted(census.rebinds.items()):
            print(f"  {count:3}  {module}")

    print("\nlocation-free")
    print("  " + ", ".join(sorted(census.free)) if census.free else "  none")

    if both:
        print("\nboth derived and told")
        print("  " + ", ".join(both))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root to measure (default: current directory)",
    )
    args = parser.parse_args(argv)

    problems = fixture_problems()
    if problems:
        for line in problems:
            print(line, file=sys.stderr)
        print("fixture failed: no measurement reported", file=sys.stderr)
        return 1

    report(build(args.root, tree.SCANNED_TREES, tree.FLAT_NAMESPACE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
