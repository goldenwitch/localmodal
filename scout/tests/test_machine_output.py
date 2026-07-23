from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module, _source_control_active

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_machine_output() -> bool:
    print("machine output schemas:")
    import contextlib
    import json
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    query = "source publication boundary automatic rebase"
    active = _source_control_active()
    command = (
        [sys.executable, "resources/source_cli.py", "search", query, "--k", "3"]
        if active
        else [sys.executable, "resources/search.py", "--json", query]
    )
    cli = subprocess.run(command, cwd=root, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    ok = _check("json command succeeds", cli.returncode == 0, cli.stderr[:120].replace("\n", " "))
    try:
        payload = json.loads(cli.stdout)
        cli_hits = payload["hits"] if active else payload[0]["hits"]
        vine_hit = next(hit for hit in cli_hits if hit["citation"].endswith("#vine"))
        ok &= _check("json retains id and citation",
                     isinstance(vine_hit["id"], str) and vine_hit["id"] != vine_hit["citation"])
    except (KeyError, StopIteration, json.JSONDecodeError, IndexError, TypeError) as exc:
        ok &= _check("json retains id and citation", False, type(exc).__name__)

    worker_command = [sys.executable, "resources/source_worker.py"] if active else [sys.executable, "resources/search.py", "--serve"]
    worker = subprocess.Popen(
        worker_command, cwd=root,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    try:
        ready = json.loads(worker.stdout.readline())
        request = {"op": "search", "query": query, "k": 3} if active else {"source": "workspace", "query": query, "k": 3}
        worker.stdin.write(json.dumps(request) + "\n")
        worker.stdin.flush()
        reply = json.loads(worker.stdout.readline())
        worker_hits = reply["result"]["hits"] if active else reply["hits"]
        worker_vine = next(hit for hit in worker_hits if hit["citation"].endswith("#vine"))
        ok &= _check("worker retains id and citation",
                     ready.get("ready") is True and isinstance(worker_vine["id"], str) and
                     worker_vine["id"] != worker_vine["citation"])
    except (KeyError, StopIteration, json.JSONDecodeError, OSError) as exc:
        ok &= _check("worker retains id and citation", False, type(exc).__name__)
    finally:
        if worker.stdin:
            worker.stdin.close()
        with contextlib.suppress(Exception):
            worker.wait(timeout=30)
        if worker.poll() is None:
            worker.kill()
    if active:
        legacy = subprocess.run(
            [sys.executable, "resources/search.py", query], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        ok &= _check(
            "legacy routed index is blocked after activation",
            legacy.returncode != 0 and "source control is active" in legacy.stderr,
            legacy.stderr[:120].replace("\n", " "),
        )
    return ok
