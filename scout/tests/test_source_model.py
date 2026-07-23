from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_source_model() -> bool:
    print("source model:")
    import subprocess
    import tempfile
    from pathlib import Path

    model = _resource_module("source_model")
    declaration = model.parse_declaration(
        {
            "name": "fixture-source",
            "origin": {"kind": "repo-file", "path": "docs/fixture.md"},
            "mime": "text/markdown",
            "ttl_days": None,
        }
    )
    ok = _check(
        "strict declaration parses",
        declaration.name == "fixture-source"
        and isinstance(declaration.origin, model.RepoFileOrigin)
        and declaration.origin.path == "docs/fixture.md"
        and declaration.ttl_days is None,
    )
    ok &= _check(
        "artifact root is deterministic",
        model.artifact_root(Path("resources"), declaration.name).as_posix()
        == "resources/scout-source--fixture-source",
    )
    add = model.parse_row(
        {
            "op": "add",
            "name": "fixture-source",
            "origin": {"kind": "https", "url": "https://example.test/a"},
            "mime": "text/plain",
            "ttl_days": 7,
        },
        0,
    )
    remove = model.parse_row({"op": "remove", "name": "fixture-source"}, 1)
    ok &= _check(
        "add and remove rows parse",
        isinstance(add, model.AddRow) and isinstance(remove, model.RemoveRow),
    )

    def code_for(call) -> str | None:
        try:
            call()
        except model.ScoutDiagnosticsError as exc:
            return exc.diagnostics[0].code.value
        return None

    ok &= _check(
        "invalid source name is typed",
        code_for(
            lambda: model.parse_declaration(
                {
                    "name": "Uppercase",
                    "origin": {"kind": "https", "url": "https://example.test"},
                    "mime": "text/plain",
                    "ttl_days": 1,
                }
            )
        ) == "SOURCE_NAME_INVALID",
    )
    ok &= _check(
        "repo traversal is typed",
        code_for(lambda: model.parse_origin({"kind": "repo-file", "path": "../outside"}))
        == "ORIGIN_INVALID",
    )
    naive_snapshot = {
        "snapshot_id": "fixture-snapshot",
        "materialized_at": "2026-07-22T12:00:00",
        "artifact_path": "scout-source--fixture-source/generations/fixture-snapshot/content",
        "sha256": "0" * 64,
        "byte_count": 0,
        "observed_mime": "text/plain",
        "origin_evidence": {},
    }
    ok &= _check(
        "naive snapshot timestamp is typed",
        code_for(lambda: model.parse_snapshot(naive_snapshot)) == "LEDGER_MALFORMED",
    )

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        root = Path(temporary) / "repo"
        root.mkdir()
        docs = root / "docs"
        docs.mkdir()
        fixture = docs / "fixture.md"
        fixture.write_text("fixture", encoding="utf-8")
        resolved = model.resolve_repo_file(
            model.RepoFileOrigin("docs/fixture.md"),
            root,
            publishable_paths=("docs/fixture.md",),
        )
        ok &= _check("repo file resolves", resolved == fixture.resolve())

        vcs = root / ".git"
        vcs.mkdir()
        (vcs / "config").write_text("private", encoding="utf-8")
        ok &= _check(
            "repo file allowlist rejects VCS config",
            code_for(
                lambda: model.resolve_repo_file(
                    model.RepoFileOrigin(".git/config"),
                    root,
                    publishable_paths=("docs/fixture.md",),
                )
            ) == "ORIGIN_INVALID",
        )

        outside = Path(temporary) / "outside"
        outside.mkdir()
        (outside / "escape.md").write_text("escape", encoding="utf-8")
        escape = root / "escape"
        if sys.platform == "win32":
            link = subprocess.run(
                f'mklink /J "{escape}" "{outside}"',
                shell=True,
                capture_output=True,
                text=True,
            )
            linked = link.returncode == 0 and escape.is_dir()
        else:
            escape.symlink_to(outside, target_is_directory=True)
            linked = escape.is_dir()
        ok &= _check("local escape fixture", linked)
        if linked:
            ok &= _check(
                "repo file escape is typed",
                code_for(
                    lambda: model.resolve_repo_file(
                        model.RepoFileOrigin("escape/escape.md"), root
                    )
                ) == "ORIGIN_NOT_FOUND",
            )
    return ok
