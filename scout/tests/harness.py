from __future__ import annotations

import sys
from pathlib import Path

# Keep moved smoke helpers anchored to the original entrypoint path.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def _check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail else ""))
    return ok

def _source_control_active() -> bool:
    from pathlib import Path

    from resources.activation import is_source_control_active

    return is_source_control_active(Path(__file__).resolve().parents[1] / "resources")

def _resource_module(name: str):
    import importlib.util as iu
    from pathlib import Path

    module_name = f"scout_smoke_{name}"
    resource_dir = Path(__file__).resolve().parents[1] / "resources"
    if str(resource_dir) not in sys.path:
        sys.path.insert(0, str(resource_dir))
    module_file = resource_dir / f"{name}.py"
    if module_file.is_file():
        spec = iu.spec_from_file_location(module_name, module_file)
    else:
        # The module was decomposed into a package; load its __init__ and let
        # its relative imports resolve against the package directory.
        spec = iu.spec_from_file_location(
            module_name,
            resource_dir / name / "__init__.py",
            submodule_search_locations=[str(resource_dir / name)],
        )
    module = iu.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
