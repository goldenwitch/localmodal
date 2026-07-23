from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_vine() -> bool:
    print("vine parser + citations:")
    import subprocess
    import tempfile
    from pathlib import Path

    vine = _resource_module("vine")
    long_description = " ".join(f"token{index}" for index in range(900))
    long_ref_description = " ".join(f"ref{index}" for index in range(900))
    dense_description = "." * 10_000
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        path = root / "plan.vine"
        path.write_text(
            "vine 1.2.0\n"
            "delimiter: ===\n"
            "---\n"
            "[root] Root task (planning) @priority(high)\n"
            "> durable decision\n"
            "  -> prose that remains description\n"
            ">not a decision\n"
            "@guidanceful prose\n"
            " ===\n"
            f"{long_description}\n"
            "===\n"
            "[dense] Dense task (planning)\n"
            f"{dense_description}\n"
            "===\n"
            "[short] Short task (planning)\n"
            "Brief task description\n"
            "===\n"
            "ref [child] Child graph (https://example.test/a_(b)) @sprite(./sprites/child.svg)\n"
            "Local proxy description\n"
            "===\n"
            "ref [long-ref] Long child graph (child.vine)\n"
            f"{long_ref_description}\n",
            encoding="utf-8",
        )
        blocks = vine.parse_vine(path)
        task = next(block for block in blocks if block.kind == "task")
        ref = next(block for block in blocks if block.kind == "ref")
        dense = next(block for block in blocks if block.block_id == "dense")
        short_task = next(block for block in blocks if block.block_id == "short")
        long_ref = next(block for block in blocks if block.block_id == "long-ref")
        task_citation = vine.citation_for(root, path, task)
        ref_citation = vine.citation_for(root, path, ref)
        dense_citation = vine.citation_for(root, path, dense)
        short_task_citation = vine.citation_for(root, path, short_task)
        long_ref_citation = vine.citation_for(root, path, long_ref)
        ok = True
        ok &= _check("task citation resolves",
                     vine.resolve_citation(root, task_citation).block_id == "root")
        ok &= _check("ref citation resolves",
                     vine.resolve_citation(root, ref_citation).block_id == "child")
        ok &= _check("exact field prefixes preserve prose",
                     "  -> prose that remains description" in task.projection and
                     ">not a decision" in task.projection and
                     "@guidanceful prose" in task.projection and
                     " ===" in task.projection and
                     "Decision: durable decision" in task.projection)
        ok &= _check("ref projection stays local",
                     "Local proxy description" in ref.projection and "example.test" not in ref.projection)

        compatibility_paths = []
        for version in ("1.0.0", "1.1.0"):
            compatibility = root / f"compat-{version}.vine"
            compatibility.write_text(
                f"vine {version}\n---\n[compat] Compatibility ({'planning'})\ntext\n",
                encoding="utf-8",
            )
            compatibility_paths.append(compatibility)
        ok &= _check("prior VINE versions parse",
                     all(vine.parse_vine(compatibility)[0].block_id == "compat"
                         for compatibility in compatibility_paths))

        spaced_delimiter = root / "spaced.vine"
        spaced_delimiter.write_text(
            "vine 1.2.0\n delimiter : === \n---\n[root] Root (planning)\n===\n[child] Child (planning)\n",
            encoding="utf-8",
        )
        ok &= _check("delimiter metadata whitespace", len(vine.parse_vine(spaced_delimiter)) == 2)

        unknown_at_ref = root / "unknown-at.vine"
        unknown_at_ref.write_text(
            "vine 1.2.0\n---\nref [child] Child (child.vine)\n@local-note remains description\n",
            encoding="utf-8",
        )
        ok &= _check("unknown ref @ stays description",
                     "@local-note remains description" in vine.parse_vine(unknown_at_ref)[0].projection)

        reserved_dir = root / "dir#part"
        reserved_dir.mkdir()
        reserved_path = reserved_dir / "plan.vine"
        reserved_path.write_text("vine 1.2.0\n---\n[root] Root (planning)\ntext\n", encoding="utf-8")
        reserved_block = vine.parse_vine(reserved_path)[0]
        reserved_citation = vine.citation_for(root, reserved_path, reserved_block)
        ok &= _check("reserved path characters encode canonically",
                     "%23" in reserved_citation and
                     vine.resolve_citation(root, reserved_citation).block_id == "root")

        citation_root = root / "repository"
        (citation_root / "sub").mkdir(parents=True)
        outside_dir = root / "outside"
        outside_dir.mkdir()
        outside = outside_dir / "plan.vine"
        outside.write_text("vine 1.2.0\n---\n[root] Outside (planning)\ntext\n", encoding="utf-8")
        try:
            vine.resolve_citation(citation_root, "sub%5C..%5C..%5Coutside.vine#root#vine")
        except vine.CitationResolutionError as exc:
            encoded_separator_rejected = "invalid VINE citation path" in str(exc)
        else:
            encoded_separator_rejected = False
        ok &= _check("encoded Windows separator rejected before resolution",
                     encoded_separator_rejected)

        escape = citation_root / "escape"
        if sys.platform == "win32":
            result = subprocess.run(
                f'mklink /J "{escape}" "{outside_dir}"',
                shell=True,
                capture_output=True,
                text=True,
            )
            link_created = result.returncode == 0 and escape.is_dir()
        else:
            escape.symlink_to(outside_dir, target_is_directory=True)
            link_created = escape.is_dir()
        ok &= _check("outside directory link fixture", link_created)
        try:
            vine.resolve_citation(citation_root, "escape/plan.vine#root#vine")
        except vine.CitationResolutionError as exc:
            link_escape_rejected = "escapes repository root" in str(exc)
        else:
            link_escape_rejected = False
        ok &= _check("outside directory link rejected", link_escape_rejected)

        bad_citations = (
            "plan.vine#root",
            "./plan.vine#root#vine",
            "PLAN.vine#root#vine",
            "../plan.vine#root#vine",
            "sub%5C..%5C..%5Coutside.vine#root#vine",
            "plan//nested.vine#root#vine",
            "/plan.vine#root#vine",
            "plan#bad.vine#root#vine",
            "dir#part/plan.vine#root#vine",
            "missing.vine#root#vine",
            "plan.vine#missing#vine",
            "plan.vine#ref:root#vine",
            "plan.vine#child#vine",
        )
        failures = []
        for citation in bad_citations:
            try:
                vine.resolve_citation(root, citation)
            except vine.CitationResolutionError:
                failures.append(citation)
        ok &= _check("bad citations fail explicitly", len(failures) == len(bad_citations))

        invalid_headers = (
            "vine 1.2.0\n---\n[bad] Bad ()\n",
            "vine 1.2.0\n---\n[bad] Bad (unknown-status)\n",
            "vine 1.2.0\n---\n[bad] Bad (planning) @broken\n",
            "vine 1.2.0\n---\nref [bad] Bad ()\n",
            "vine 1.2.0\n---\nref [bad] Bad (https://example.test/a)\n@artifact forbidden\n",
            "vine 1.2.0\ndelimiter: ===\n---\n[a] First (planning)\n===\n[a] Duplicate (planning)\n",
            " vine 1.2.0\n---\n[bad] Bad (planning)\n",
            "vine nonsense\n---\n[bad] Bad (planning)\n",
        )
        invalid_failures = 0
        for index, fixture in enumerate(invalid_headers):
            invalid_path = root / f"invalid{index}.vine"
            invalid_path.write_text(fixture, encoding="utf-8")
            try:
                vine.parse_vine(invalid_path)
            except vine.VineError:
                invalid_failures += 1
        ok &= _check("invalid headers fail explicitly", invalid_failures == len(invalid_headers))

        segments = vine.segments_for_vine(root, path)
        root_segments = [segment for segment in segments if segment.citation == task_citation]
        dense_segments = [segment for segment in segments if segment.citation == dense_citation]
        short_task_segments = [segment for segment in segments if segment.citation == short_task_citation]
        long_ref_segments = [segment for segment in segments if segment.citation == long_ref_citation]
        tokenizer, limit = vine._tokenizer_settings()
        token_safe = all(
            len(tokenizer(segment.text, add_special_tokens=True,
                          truncation=False, verbose=False)["input_ids"]) <= limit
            for segment in segments
        )

        def ranges_cover(block, block_segments):
            token_count = len(tokenizer(block.projection, add_special_tokens=False,
                                        truncation=False, verbose=False)["input_ids"])
            ordered = sorted(block_segments, key=lambda segment: segment.ordinal)
            if not ordered or ordered[0].token_start != 0 or ordered[-1].token_end != token_count:
                return False
            return all(
                current.token_end > previous.token_end and
                0 <= previous.token_end - current.token_start <= 30
                for previous, current in zip(ordered, ordered[1:])
            )

        ok &= _check("long task segments", len(root_segments) > 1)
        ok &= _check("long ref segments", len(long_ref_segments) > 1)
        ok &= _check("dense fallback segments", len(dense_segments) > 1)
        ok &= _check("segment ids unique", len({segment.index_id for segment in segments}) == len(segments))
        ok &= _check("segments token-safe", token_safe, f"limit={limit}")
        ok &= _check("shared task citation", all(segment.citation == task_citation for segment in root_segments))
        ok &= _check("short task emits", len(short_task_segments) == 1)
        ok &= _check("short ref emits", any(segment.citation == ref_citation for segment in segments))
        ok &= _check("task token ranges cover projection", ranges_cover(task, root_segments))
        ok &= _check("ref token ranges cover projection", ranges_cover(long_ref, long_ref_segments))
        ok &= _check("dense token ranges cover projection", ranges_cover(dense, dense_segments))
        joined = "\n".join(segment.text for segment in root_segments)
        ok &= _check("selected task text retained", "durable decision" in joined and "token899" in joined)
        return ok
