"""Import graph over the measured trees: strata, cycles, capability reach.

Prints nothing unless its known-answer fixture passes first.

What it sees: import statements, plus the modules named as subprocess.Popen
targets. What it cannot see: coupling that is not an import -- notably a
package resolved through sys.modules at call time, which is a runtime cycle no
static reading of imports reports.

    python -m tools.import_graph [--root PATH]
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from graphlib import TopologicalSorter
from pathlib import Path

from . import fixture, tree


@dataclass(frozen=True)
class Graph:
    modules: tuple[str, ...]
    imports: frozenset[tuple[str, str]]
    spawns: frozenset[tuple[str, str]]
    lines: dict[str, int]
    unresolved: frozenset[tuple[str, str]]

    @property
    def edges(self) -> frozenset[tuple[str, str]]:
        """Every way one module reaches another, process boundary included."""
        return self.imports | self.spawns


def build(root: Path, trees: tuple[str, ...], flat: str) -> Graph:
    files = tree.iter_module_files(root, trees)
    sources = {path: path.read_text(encoding="utf-8") for path in files}
    names = {path: tree.module_name(root, path, flat) for path in files}
    by_key = _resolution_index(root, names, flat)
    by_filename: dict[str, set[str]] = defaultdict(set)
    for path, name in names.items():
        by_filename[path.name].add(name)

    imports: set[tuple[str, str]] = set()
    spawns: set[tuple[str, str]] = set()
    unresolved: set[tuple[str, str]] = set()
    for path, name in names.items():
        module = ast.parse(sources[path], filename=str(path))
        for key in _import_keys(module, root, path):
            target = by_key.get(key)
            if target is None:
                unresolved.add((name, key))
            elif target != name:
                imports.add((name, target))
        for filename in _spawn_targets(module):
            for target in by_filename.get(filename, ()):
                if target != name:
                    spawns.add((name, target))

    lines = {names[path]: len(sources[path].splitlines()) for path in files}
    return Graph(
        modules=tuple(sorted(names.values())),
        imports=frozenset(imports),
        spawns=frozenset(spawns),
        lines=lines,
        unresolved=frozenset(unresolved),
    )


def _resolution_index(root: Path, names: dict[Path, str], flat: str) -> dict[str, str]:
    """Every name an import statement may spell a module by."""
    index: dict[str, str] = {}
    for path, name in names.items():
        parts = path.relative_to(root).with_suffix("").parts
        if parts and parts[-1] == "__init__":
            # A package answers to its own name. Without this, an import
            # naming a package resolves to nothing and the edge disappears.
            parts = parts[:-1]
        for candidate in (parts, parts[1:] if parts[:1] == (flat,) else ()):
            if candidate:
                index[".".join(candidate)] = name
    return index


def _package_parts(root: Path, path: Path) -> tuple[str, ...]:
    """The package a relative import counts from: the directory holding the
    file. Taken from the path rather than from the module name, because the
    name of a package's __init__ is the package itself, and trimming a
    component off that name lands in the parent."""
    return path.relative_to(root).parts[:-1]


def _import_keys(module: ast.Module, root: Path, path: Path) -> Iterator[str]:
    package = _package_parts(root, path)
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = tuple((node.module or "").split("."))
            else:
                trimmed = package[: len(package) - (node.level - 1)]
                base = trimmed
                if node.module:
                    base = trimmed + tuple(node.module.split("."))
            if not base:
                continue
            yield ".".join(base)
            for alias in node.names:
                # from X import y is an edge to X.y when that is a module.
                yield ".".join(base + (alias.name,))


def _spawn_targets(module: ast.Module) -> Iterator[str]:
    """Filenames handed to subprocess.Popen: a process boundary is still an
    edge."""
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called != "Popen":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                if inner.value.endswith(".py"):
                    yield inner.value


def _components(
    modules: tuple[str, ...], edges: frozenset[tuple[str, str]]
) -> tuple[list[frozenset[str]], dict[str, int]]:
    """Strongly connected components, so a cycle is one node of a DAG."""
    out: dict[str, set[str]] = defaultdict(set)
    into: dict[str, set[str]] = defaultdict(set)
    for importer, imported in edges:
        out[importer].add(imported)
        into[imported].add(importer)

    order: list[str] = []
    seen: set[str] = set()
    for start in modules:
        if start in seen:
            continue
        seen.add(start)
        stack = [(start, iter(sorted(out[start])))]
        while stack:
            node, children = stack[-1]
            for child in children:
                if child not in seen:
                    seen.add(child)
                    stack.append((child, iter(sorted(out[child]))))
                    break
            else:
                order.append(node)
                stack.pop()

    assigned: dict[str, int] = {}
    components: list[frozenset[str]] = []
    for node in reversed(order):
        if node in assigned:
            continue
        index = len(components)
        assigned[node] = index
        member: set[str] = set()
        stack_back = [node]
        while stack_back:
            current = stack_back.pop()
            member.add(current)
            for previous in sorted(into[current]):
                if previous not in assigned:
                    assigned[previous] = index
                    stack_back.append(previous)
        components.append(frozenset(member))
    return components, assigned


def strata(graph: Graph) -> dict[str, int]:
    """Longest path to a module that imports nothing. Over imports alone: a
    spawned process is reached, not imported."""
    components, assigned = _components(graph.modules, graph.imports)
    edges: dict[int, set[int]] = {index: set() for index in range(len(components))}
    for importer, imported in graph.imports:
        if assigned[importer] != assigned[imported]:
            edges[assigned[importer]].add(assigned[imported])
    level: dict[int, int] = {}
    for index in TopologicalSorter(edges).static_order():
        level[index] = 1 + max((level[dep] for dep in edges[index]), default=-1)
    return {
        module: level[assigned[module]]
        for module in graph.modules
    }


def cycles(graph: Graph) -> frozenset[frozenset[str]]:
    components, _ = _components(graph.modules, graph.imports)
    return frozenset(component for component in components if len(component) > 1)


def fan_in(graph: Graph) -> dict[str, int]:
    counts = dict.fromkeys(graph.modules, 0)
    for _, imported in graph.imports:
        counts[imported] += 1
    return counts


def serves(graph: Graph, capabilities: dict[str, tuple[str, ...]]) -> dict[str, frozenset[str]]:
    """Which capabilities can reach each module, entries included."""
    out: dict[str, set[str]] = defaultdict(set)
    for importer, imported in graph.edges:
        out[importer].add(imported)
    reached: dict[str, set[str]] = {module: set() for module in graph.modules}
    for capability, entries in capabilities.items():
        stack = list(entries)
        seen: set[str] = set()
        while stack:
            module = stack.pop()
            if module in seen:
                continue
            seen.add(module)
            stack.extend(out[module])
        for module in seen:
            reached[module].add(capability)
    return {module: frozenset(names) for module, names in reached.items()}


def unknown_entries(graph: Graph, capabilities: dict[str, tuple[str, ...]]) -> list[str]:
    known = set(graph.modules)
    return sorted(
        f"{capability}: {entry}"
        for capability, entries in capabilities.items()
        for entry in entries
        if entry not in known
    )


def fixture_problems() -> list[str]:
    with fixture.materialized() as root:
        graph = build(root, fixture.TREES, fixture.FLAT)
        problems = fixture.diff("modules", fixture.MODULES, frozenset(graph.modules))
        problems += fixture.diff("imports", fixture.EXPECTED_IMPORTS, graph.imports)
        problems += fixture.diff("spawns", fixture.EXPECTED_SPAWNS, graph.spawns)
        problems += fixture.diff("strata", fixture.EXPECTED_STRATA, strata(graph))
        problems += fixture.diff("fan-in", fixture.EXPECTED_FAN_IN, fan_in(graph))
        problems += fixture.diff("cycles", fixture.EXPECTED_CYCLES, cycles(graph))
        problems += fixture.diff(
            "capability reach",
            fixture.EXPECTED_SERVES,
            serves(graph, fixture.CAPABILITIES),
        )
    return problems


def report(graph: Graph, capabilities: dict[str, tuple[str, ...]]) -> None:
    levels = strata(graph)
    fan = fan_in(graph)
    found = cycles(graph)
    print(f"modules {len(graph.modules)}   imports {len(graph.imports)}   "
          f"spawns {len(graph.spawns)}   strata {max(levels.values()) + 1}   "
          f"import cycles {len(found)}")

    print("\nstrata (fan-in in parentheses)")
    by_level: dict[int, list[str]] = defaultdict(list)
    for module, level in levels.items():
        by_level[level].append(module)
    for level in sorted(by_level):
        members = ", ".join(
            f"{module} ({fan[module]})"
            for module in sorted(by_level[level], key=lambda m: (-fan[m], m))
        )
        print(f"  {level}  {members}")

    print("\ncycles")
    if not found:
        print("  none in the import edges")
    for component in sorted(found, key=lambda c: sorted(c)):
        print("  " + " <-> ".join(sorted(component)))

    if graph.spawns:
        print("\nspawned, not imported")
        for importer, imported in sorted(graph.spawns):
            print(f"  {importer} -> {imported}")

    print("\ncapability reach (imports and spawns)")
    reach = serves(graph, capabilities)
    groups: dict[frozenset[str], list[str]] = defaultdict(list)
    for module, names in reach.items():
        groups[names].append(module)
    for names in sorted(groups, key=lambda n: (len(n), sorted(n))):
        served = ", ".join(sorted(names)) if names else "nothing"
        members = sorted(groups[names])
        total = sum(graph.lines[module] for module in members)
        print(f"  serves {served}  --  {len(members)} modules, {total} lines")
        print(f"    {', '.join(members)}")

    if graph.unresolved:
        print("\nimports resolving outside the measured trees")
        external = sorted({key.split(".")[0] for _, key in graph.unresolved})
        print("  " + ", ".join(external))


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

    graph = build(args.root, tree.SCANNED_TREES, tree.FLAT_NAMESPACE)
    missing = unknown_entries(graph, tree.CAPABILITIES)
    if missing:
        for entry in missing:
            print(f"declared capability entry is not a module: {entry}", file=sys.stderr)
        print("stale capability declaration: no measurement reported", file=sys.stderr)
        return 1

    report(graph, tree.CAPABILITIES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
