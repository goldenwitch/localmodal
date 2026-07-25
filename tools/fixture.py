"""A tree whose measurement is known by hand.

Both instruments check themselves against this before they report anything.
The tree is written to a temporary directory rather than checked in, so no
deliberate import cycle and no deliberate location derivation sits in the
repository for a later scan to trip over.

It exists because two measurements of the real tree were confidently wrong:

  * a bare-name import naming a *package* resolved to nothing, silently
    dropping the edge -- here, user -> pkg/__init__;
  * a relative import inside a package __init__ resolved against the parent
    directory instead of the package -- here, pkg/__init__ -> pkg/inner.

Either error, reintroduced, breaks the expected edges below.

The tree, by hand:

    res/leaf.py        imports nothing, knows no location
    res/base.py        from leaf import VALUE          (bare name, module)
    res/model.py       told its location: build(root: Path)
    res/pkg/__init__.py  from .inner import Inner      (relative, inside a package __init__)
    res/pkg/inner.py   from base import BASE           (bare name, from inside a package)
    res/user.py        from pkg import Inner           (bare name, naming a package)
    res/loop_a.py      import loop_b                   \\ the deliberate cycle
    res/loop_b.py      import loop_a                   /
    res/rebound.py     rebinds __file__, and names a .py file outside any spawn
    app/entry.py       derives its own location, spawns res/user.py
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

TREES: tuple[str, ...] = ("res", "app")
FLAT = "res"

CAPABILITIES: dict[str, tuple[str, ...]] = {
    "run": ("app/entry",),
    "cycle": ("loop_a",),
}

FILES: dict[str, str] = {
    "res/leaf.py": '''"""The floor of the graph: no imports, no location."""

VALUE = 1
''',
    "res/base.py": '''from leaf import VALUE

BASE = VALUE + 1
''',
    "res/model.py": '''from pathlib import Path


def build(root: Path) -> Path:
    """Told where it is."""
    return root / "model"
''',
    "res/pkg/__init__.py": '''from .inner import Inner

__all__ = ["Inner"]
''',
    "res/pkg/inner.py": '''from base import BASE


class Inner:
    value = BASE
''',
    "res/user.py": '''from pkg import Inner

USED = Inner.value
''',
    "res/loop_a.py": '''import loop_b

NAME = "a" + loop_b.__name__
''',
    "res/loop_b.py": '''import loop_a

NAME = "b"
''',
    "res/rebound.py": '''from pathlib import Path

# Rebound so that parents[1] arithmetic still lands where it used to. The
# literal below names a .py file and is not a spawn.
__file__ = str(Path(__file__).resolve().parents[1] / "app" / "entry.py")
ANCHOR = Path(__file__).parent
''',
    "app/entry.py": '''import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def spawn() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(ROOT / "res" / "user.py"), "--serve"],
        cwd=str(ROOT),
    )
''',
}

MODULES = frozenset(
    {
        "app/entry",
        "base",
        "leaf",
        "loop_a",
        "loop_b",
        "model",
        "pkg/__init__",
        "pkg/inner",
        "rebound",
        "user",
    }
)

# Import edges, importer -> imported.
EXPECTED_IMPORTS = frozenset(
    {
        ("base", "leaf"),
        ("loop_a", "loop_b"),
        ("loop_b", "loop_a"),
        ("pkg/__init__", "pkg/inner"),
        ("pkg/inner", "base"),
        ("user", "pkg/__init__"),
    }
)

# Process edges. rebound names "entry.py" outside a spawn, and that is not one.
EXPECTED_SPAWNS = frozenset({("app/entry", "user")})

# Longest path to a module importing nothing, over imports alone, cycles
# collapsed to one stratum. app/entry spawns but imports nothing local.
EXPECTED_STRATA = {
    "app/entry": 0,
    "base": 1,
    "leaf": 0,
    "loop_a": 0,
    "loop_b": 0,
    "model": 0,
    "pkg/__init__": 3,
    "pkg/inner": 2,
    "rebound": 0,
    "user": 4,
}

EXPECTED_FAN_IN = {
    "app/entry": 0,
    "base": 1,
    "leaf": 1,
    "loop_a": 1,
    "loop_b": 1,
    "model": 0,
    "pkg/__init__": 1,
    "pkg/inner": 1,
    "rebound": 0,
    "user": 0,
}

EXPECTED_CYCLES = frozenset({frozenset({"loop_a", "loop_b"})})

# Which capabilities can reach each module, spawn edges included. model and
# rebound serve none.
EXPECTED_SERVES = {
    "app/entry": frozenset({"run"}),
    "base": frozenset({"run"}),
    "leaf": frozenset({"run"}),
    "loop_a": frozenset({"cycle"}),
    "loop_b": frozenset({"cycle"}),
    "model": frozenset(),
    "pkg/__init__": frozenset({"run"}),
    "pkg/inner": frozenset({"run"}),
    "rebound": frozenset(),
    "user": frozenset({"run"}),
}

# Sites, not just modules: rebound reads __file__ twice and writes it once.
EXPECTED_DERIVES = {"app/entry": 1, "rebound": 2}
EXPECTED_REBINDS = {"rebound": 1}
EXPECTED_TOLD = {"model": 1}
EXPECTED_FREE = frozenset(
    {"base", "leaf", "loop_a", "loop_b", "pkg/__init__", "pkg/inner", "user"}
)


@contextmanager
def materialized() -> Iterator[Path]:
    """Write the tree to a temporary directory and yield its root."""
    with tempfile.TemporaryDirectory(prefix="scout-instrument-fixture-") as raw:
        root = Path(raw)
        for relative, source in FILES.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        yield root


def diff(label: str, expected: object, measured: object) -> list[str]:
    """Readable account of one disagreement, or nothing when they agree."""
    if expected == measured:
        return []
    problems = [f"fixture mismatch in {label}:"]
    if isinstance(expected, dict) and isinstance(measured, dict):
        for key in sorted(set(expected) | set(measured), key=str):
            if expected.get(key) != measured.get(key):
                problems.append(
                    f"  {key}: expected {expected.get(key)!r}, "
                    f"measured {measured.get(key)!r}"
                )
    elif isinstance(expected, (set, frozenset)) and isinstance(measured, (set, frozenset)):
        for item in sorted(expected - measured, key=str):
            problems.append(f"  missing: {item}")
        for item in sorted(measured - expected, key=str):
            problems.append(f"  unexpected: {item}")
    else:
        problems.append(f"  expected {expected!r}, measured {measured!r}")
    return problems
