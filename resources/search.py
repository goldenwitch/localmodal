#!/usr/bin/env python3
"""Semantic search over the local corpus: papers (resources/pdf/*.pdf) and our
own writing (human-owned-spec/*.md, notes/**/*.md, proposals/**/*.md, root
*.md). Two indexes, not one: papers (the third-party PDFs) and workspace
(everything we wrote). A search of one can never bury -- or leak into -- the
other.

Off-the-shelf engine: txtai (dense sentence embeddings + a faiss index). We
build nothing search-related ourselves; we only feed our own sources in and
read results out. Every query also runs a naive keyword baseline so the recall
gap between literal matching and meaning-based matching is *shown*, not
asserted -- in keeping with "take no one's word."

Why semantic and not grep: sources name the same idea in different words
(adapter / LoRA / fine-tune; warm / provisioned / min_containers). Keyword
search has a recall hole exactly there.

Engine copied verbatim from D:/git/JEPA resources/search.py (2026-07-17);
only the corpus definitions (which files feed each index) are this repo's.
One engine delta, found by smoke test: an EMPTY corpus publishes a sentinel
version instead of crashing (txtai cannot save a zero-document index), and
searches on it answer with zero hits -- the papers corpus is legitimately
empty at repo birth. Candidate backport to the source repo.

Usage:
    python search.py "warmth thermostat reconciler"
    python search.py "prefix cache hit rate" --k 8
    python search.py "query one" "query two" "query three"   # batch: index loads once
    python search.py --rebuild                              # full re-extract + re-embed, no query
    python search.py --update                               # incremental: embed only new/changed chunks
    python search.py "..." --rebuild                        # rebuild, then query
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
from txtai import Embeddings

# Some arXiv PDFs have unparseable color spaces; PyMuPDF's C error handler can
# crash on Windows when its stdout callback fires mid-extract (OSError 22). Mute
# the display so a cosmetic warning can't kill a rebuild -- bad pages are skipped
# defensively in chunks() regardless.
fitz.TOOLS.mupdf_display_errors(False)

# The corpus is full of math glyphs. On a non-UTF-8 console (Windows cp1252)
# printing a hit would raise UnicodeEncodeError and kill the search. Prefer
# UTF-8 so symbols render; fall back to a replacement char rather than
# crashing the instrument on a glyph it can't draw.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # already-wrapped or non-reconfigurable stream; printing stays best-effort

PDF_DIR = Path(__file__).parent / "pdf"
REPO = Path(__file__).parent.parent                # repo root
SPEC_DIR = REPO / "human-owned-spec"               # the human-owned spec (design truth)
NOTES_DIR = REPO / "notes"                         # design notes (empty until they land; wired now)
PROPOSALS_DIR = REPO / "proposals"                 # forward design proposals (same)
INDEX_ROOT = Path(__file__).parent / ".index"   # one subdir per corpus: .index/papers/ + .index/workspace/,
                                                 # each a set of versioned v<ns>/ dirs + its own CURRENT pointer
MODEL = "sentence-transformers/all-MiniLM-L6-v2"
CHUNK_WORDS = 180  # MiniLM's comfortable context; pages exceed it, so we window
OVERLAP = 30


def _window(words):
    """Yield (chunk_index, text) for overlapping word windows."""
    step = CHUNK_WORDS - OVERLAP
    for ci, start in enumerate(range(0, max(len(words), 1), step)):
        window = words[start : start + CHUNK_WORDS]
        if len(window) >= 20:  # skip headers/page-number scraps
            yield ci, " ".join(window)


def paper_chunks():
    """Yield (id, text) for the third-party PDFs -> "<paper>#p<page>#c<chunk>". The
    papers corpus: text we did not write, indexed on its own so it never buries ours."""
    for pdf in sorted(PDF_DIR.glob("*.pdf")):
        key = pdf.stem
        with fitz.open(pdf) as doc:
            for pno, page in enumerate(doc, start=1):
                try:
                    words = page.get_text().split()
                except Exception as exc:  # don't kill the rebuild -- but never hide it
                    print(f"WARN: skipped {key} p{pno}: {type(exc).__name__}: {exc}",
                          file=sys.stderr)
                    continue
                for ci, text in _window(words):
                    yield f"{key}#p{pno}#c{ci}", text


def workspace_chunks():
    """Yield (id, text) for everything WE wrote, all self-citing via the id tag:
      - spec       human-owned-spec/*.md                   -> "<name>#spec#c<chunk>"
      - notes      notes/**/*.md (minus README)            -> "<name>#note#c<chunk>"
      - proposals  proposals/**/*.md (minus README)        -> "<name>#proposal#c<chunk>"
      - docs       *.md at repo root (minus README)        -> "<name>#doc#c<chunk>"
    notes/ and proposals/ are empty until they land; wired now so a landed doc
    auto-indexes with no code change. The workspace corpus: the human-owned spec,
    a design note, and a forward proposal -- ranked against each other, never
    against a thousand paper pages."""
    for spec in sorted(SPEC_DIR.glob("*.md")):
        if spec.name == "README.md":
            continue  # folder meta, not the spec
        for ci, text in _window(spec.read_text(encoding="utf-8").split()):
            yield f"{spec.stem}#spec#c{ci}", text

    for note in sorted(NOTES_DIR.glob("**/*.md")):
        if note.name == "README.md":
            continue  # folder meta, not a design note
        for ci, text in _window(note.read_text(encoding="utf-8").split()):
            yield f"{note.stem}#note#c{ci}", text

    for prop in sorted(PROPOSALS_DIR.glob("**/*.md")):
        if prop.name == "README.md":
            continue  # folder meta, not a proposal
        for ci, text in _window(prop.read_text(encoding="utf-8").split()):
            yield f"{prop.stem}#proposal#c{ci}", text

    for doc in sorted(REPO.glob("*.md")):
        if doc.name == "README.md":
            continue  # repo meta, not a design doc
        for ci, text in _window(doc.read_text(encoding="utf-8").split()):
            yield f"{doc.stem}#doc#c{ci}", text


@dataclass(frozen=True)
class Corpus:
    """One searchable index: a name, the chunk source that fills it, and its own
    versioned dir + CURRENT pointer under .index/<name>/. Two of these -- papers and
    workspace -- replace the old single mixed index. A workspace search cannot return
    a paper because no paper chunk was ever embedded into its index: separation by
    construction, not by a runtime filter we have to keep correct."""

    name: str
    chunks: Callable[[], Iterator[tuple[str, str]]]

    @property
    def dir(self) -> Path:
        return INDEX_ROOT / self.name

    @property
    def current(self) -> Path:
        return self.dir / "CURRENT"


PAPERS = Corpus("papers", paper_chunks)
WORKSPACE = Corpus("workspace", workspace_chunks)
CORPORA = {c.name: c for c in (PAPERS, WORKSPACE)}


def _read_current(corpus: Corpus) -> Path | None:
    """Return the live index version named by `corpus`'s CURRENT, or None if unset/missing."""
    try:
        name = corpus.current.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    vdir = corpus.dir / name
    return vdir if name and vdir.is_dir() else None


def _load(version_dir: Path) -> Embeddings | None:
    """Load the index at `version_dir`; None if it is the EMPTY sentinel (a published
    version with zero documents -- txtai has nothing to load there)."""
    if (version_dir / "EMPTY").exists():
        return None
    emb = Embeddings()
    emb.load(str(version_dir))
    return emb


def _cleanup(corpus: Corpus, keep: str) -> None:
    """Best-effort removal of `corpus`'s stale version dirs. One still held open by a
    live worker (a Windows file lock) is skipped silently and reclaimed on a later
    rebuild, once that worker has reloaded off CURRENT and let go -- so cleanup
    never fights a reader either."""
    for d in corpus.dir.glob("v*"):
        if d.name != keep and d.is_dir():
            with contextlib.suppress(OSError):
                shutil.rmtree(d)


def _publish(corpus: Corpus, version: str) -> None:
    """Make `version` live for `corpus` by flipping its CURRENT with a single atomic
    file rename. A reader sees either the old pointer or the new one -- never a partial
    write, never a half-built index -- and the rename touches no file the worker holds
    open. This is what makes the rebuild-vs-worker lock structurally impossible
    instead of something we avoid by hand (kill the worker, rebuild, restart)."""
    corpus.dir.mkdir(parents=True, exist_ok=True)
    tmp = corpus.dir / "CURRENT.tmp"
    tmp.write_text(version, encoding="utf-8")
    os.replace(tmp, corpus.current)  # atomic on Windows and POSIX for files
    _cleanup(corpus, keep=version)


def _sig(text: str) -> str:
    """Stable content signature for one chunk -- changes iff the chunk text changes.
    The unit of incremental work: a chunk whose sig is unchanged is never re-embedded."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _manifest(version_dir: Path) -> Path:
    return version_dir / "manifest.json"


def _read_sigs(version_dir: Path, emb: Embeddings) -> dict:
    """`id -> sig` for the chunks already in `version_dir`. Prefer the sidecar manifest
    (hashed from the exact text indexed); if it predates manifests, reconstruct it from
    the index's own stored chunk text, so even a pre-manifest index updates incrementally
    instead of forcing one last full re-embed."""
    with contextlib.suppress(OSError, json.JSONDecodeError):
        return json.loads(_manifest(version_dir).read_text(encoding="utf-8"))
    rows = emb.search("select id, text from txtai limit 1000000")
    return {r["id"]: _sig(r["text"]) for r in rows}


def _save_version(corpus: Corpus, emb: Embeddings | None, sigs: dict) -> None:
    """Persist `emb` to a fresh version dir under `corpus`, write its signature manifest
    beside it, then atomically publish (see _publish). Shared by full build and incremental
    update, so both get the same lock-free, hot-swappable publish.

    A zero-document corpus (emb None, sigs empty) publishes an EMPTY-sentinel version:
    txtai cannot save an index with no documents (its database connection is never
    created), so emptiness is made a lawful, loadable state instead of a save-time crash."""
    vdir = corpus.dir / f"v{time.time_ns()}"
    if sigs:
        emb.save(str(vdir))  # fresh dir: nothing to unlink, no open handle to fight
    else:
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "EMPTY").write_text("0 chunks", encoding="utf-8")
    _manifest(vdir).write_text(json.dumps(sigs), encoding="utf-8")
    _publish(corpus, vdir.name)


def build(corpus: Corpus) -> Embeddings | None:
    """Extract, embed, and publish a fresh index version for `corpus`.

    The index is written to a brand-new versioned dir and made live by an atomic
    pointer flip (see _publish), so a rebuild never overwrites a file a running
    worker holds open -- the lock that used to require killing the worker by hand
    is now structurally impossible, not a thing to remember."""
    docs = list(corpus.chunks())
    print(f"indexing {len(docs)} chunks for the {corpus.name} corpus ...")
    if not docs:  # a glob/path regression that empties a corpus must scream, not save a blank index
        print(f"WARN: the {corpus.name} corpus is empty -- check its source paths, then rebuild.",
              file=sys.stderr)
    if corpus is PAPERS:
        # Silent-failure guard, papers-only by nature: a PDF that yields NO chunks is almost
        # certainly a failed extraction (binary parse), where a .md that fails to read RAISES.
        seen = {uid.split("#", 1)[0] for uid, _ in docs}
        missing = [p.stem for p in sorted(PDF_DIR.glob("*.pdf")) if p.stem not in seen]
        if missing:
            print(f"WARN: 0 chunks from {len(missing)} PDF(s): {', '.join(missing)} "
                  f"(corrupt or failed extraction?)", file=sys.stderr)
    sigs = {uid: _sig(text) for uid, text in docs}
    if len(sigs) != len(docs):  # duplicate ids would silently shadow each other -- never allow it
        raise SystemExit(f"FATAL: duplicate chunk ids in the {corpus.name} corpus -- ids must be unique")
    if docs:
        emb = Embeddings(path=MODEL, content=True)
        emb.index(docs)
    else:
        emb = None  # publish the EMPTY sentinel; searches answer with zero hits
    _save_version(corpus, emb, sigs)
    return emb


def update(corpus: Corpus) -> Embeddings:
    """Incremental index update for `corpus`: embed only the chunks whose text is new or changed,
    drop the chunks that disappeared, and publish a new version -- the fast path when a few sources
    were added. A full --rebuild re-embeds everything (use it for a schema change or to compact);
    this touches only what actually moved.

    Correctness rests on the per-chunk signature: a chunk is re-embedded iff its text changed,
    so an in-place edit to a doc is caught, not merely added/removed files."""
    cur = _read_current(corpus)
    if cur is None:
        return build(corpus)  # nothing to diff against yet
    emb = _load(cur)
    if emb is None:
        return build(corpus)  # previous version was the EMPTY sentinel: everything is new
    old = _read_sigs(cur, emb)
    docs = list(corpus.chunks())
    new = {uid: _sig(text) for uid, text in docs}
    if len(new) != len(docs):
        raise SystemExit(f"FATAL: duplicate chunk ids in the {corpus.name} corpus -- ids must be unique")
    text_by_id = dict(docs)
    add = [uid for uid, s in new.items() if old.get(uid) != s]
    drop = [uid for uid in old if uid not in new]
    if not add and not drop:
        print(f"{corpus.name}: index already current -- {len(new)} chunks, nothing to embed")
        return emb
    if add:
        emb.upsert([(uid, text_by_id[uid]) for uid in add])  # embeds ONLY these
    if drop:
        emb.delete(drop)
    print(f"{corpus.name}: incremental update +{len(add)} new/changed, -{len(drop)} removed "
          f"-> {len(new)} chunks (skipped re-embedding {len(new) - len(add)})")
    _save_version(corpus, emb, new)
    return emb


def load_or_build(corpus: Corpus, rebuild: bool, update_mode: bool = False) -> Embeddings | None:
    if rebuild:
        return build(corpus)
    if update_mode:
        return update(corpus)
    cur = _read_current(corpus)
    if cur is not None:
        return _load(cur)
    return build(corpus)


def _cite(uid: str) -> str:
    paper, page, _ = uid.split("#")
    return f"{paper:<22} {page}"


def semantic(emb: Embeddings | None, query: str, k: int):
    """txtai dense search -> top-k real passages (no generation, no drift).
    None = the EMPTY-sentinel index: zero hits, honestly."""
    if emb is None:
        return []
    return emb.search(query, k)


def keyword(emb: Embeddings | None, query: str, k: int):
    """Naive baseline over the same stored chunks: AND of query terms."""
    if emb is None:
        return []
    rows = emb.search("select id, text from txtai limit 1000000")
    terms = [t for t in query.lower().split() if len(t) > 2]
    scored = []
    for r in rows:
        low = r["text"].lower()
        if all(t in low for t in terms):
            scored.append((sum(low.count(t) for t in terms), r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:k]]


def _show(label: str, hits: list) -> None:
    print(f"\n=== {label}: {len(hits)} hit(s) ===")
    if not hits:
        print("  (none)")
        return
    for h in hits:
        snippet = " ".join(h["text"].split()[:26])
        score = h.get("score")
        tag = f"{score:.3f}  " if isinstance(score, float) else ""
        print(f"  {tag}{_cite(h['id'])}  {snippet} ...")


def run_query(emb: Embeddings, query: str, k: int) -> None:
    """Run one query through both readouts and print them."""
    _show("SEMANTIC (txtai)", semantic(emb, query, k))
    _show("KEYWORD (naive baseline)", keyword(emb, query, k))


def serve(rebuild: bool) -> int:
    """Resident worker for the scout MCP server: load BOTH live indexes (papers +
    workspace), then speak line-JSON -- {"source": ..., "query": ..., "k": ...} in,
    {"hits": [...]} out, one line each way. stdout carries only protocol lines (load
    chatter goes to stderr). Exits on stdin EOF: the parent dying closes our stdin, so
    an orphaned worker cannot outlive its server.

    `source` names which index to search ("papers" | "workspace") -- an unknown or
    missing source is an error, never a silent default that searches the wrong corpus.
    Before each query we re-read that corpus's CURRENT and, if a rebuild has published a
    newer version, swap to it and close the old one -- so a rebuild is picked up live
    without a restart, and letting go of the old version lets the next rebuild reclaim it."""
    live: dict[str, list] = {}  # name -> [emb, version]
    with contextlib.redirect_stdout(sys.stderr):
        for corpus in CORPORA.values():
            if rebuild or _read_current(corpus) is None:
                build(corpus)
            cur = _read_current(corpus)
            live[corpus.name] = [_load(cur), cur.name]
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            corpus = CORPORA.get(req.get("source"))
            if corpus is None:
                raise ValueError(
                    f"unknown source {req.get('source')!r}; expected one of {sorted(CORPORA)}")
            cur = _read_current(corpus)
            if cur is not None and cur.name != live[corpus.name][1]:
                with contextlib.redirect_stdout(sys.stderr):
                    fresh = _load(cur)
                with contextlib.suppress(Exception):
                    live[corpus.name][0].close()  # release the old version dir so cleanup can reclaim it
                live[corpus.name] = [fresh, cur.name]
            emb = live[corpus.name][0]
            hits = [{"id": h["id"], "score": h.get("score"), "text": h["text"]}
                    for h in semantic(emb, req["query"], int(req.get("k", 6)))]
            out = {"hits": hits}
        except Exception as exc:  # bad line or search error: report it, keep serving
            out = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query", nargs="*",
                    help="one or more queries; the index loads once and all run")
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--rebuild", action="store_true",
                    help="full from-scratch re-embed of the whole corpus, then publish")
    ap.add_argument("--update", action="store_true",
                    help="incremental: embed only chunks new or changed since the current "
                         "index, then publish a new version -- the fast path after adding a "
                         "few papers (--rebuild re-embeds everything; use it to compact)")
    ap.add_argument("--json", action="store_true",
                    help="machine output: semantic hits only, one JSON document "
                         "on stdout (for scripts/one-shots)")
    ap.add_argument("--serve", action="store_true",
                    help="worker mode for the scout MCP server: load once, then "
                         "line-JSON on stdin/stdout until EOF")
    args = ap.parse_args(argv)

    if not args.query and not args.rebuild and not args.update and not args.serve:
        ap.error("give at least one query (or --rebuild / --update to only (re)index)")

    if args.serve:
        return serve(args.rebuild)

    if args.json:
        # stdout must stay pure JSON; chatter (index build notices) -> stderr.
        with contextlib.redirect_stdout(sys.stderr):
            embs = {c.name: load_or_build(c, args.rebuild, args.update) for c in CORPORA.values()}
            out = [{"query": q,
                    "hits": [{"id": h["id"], "score": h.get("score"),
                              "text": h["text"], "corpus": name}
                             for name, emb in embs.items()
                             for h in semantic(emb, q, args.k)]}
                   for q in args.query]
        print(json.dumps(out, ensure_ascii=False))
        return 0

    embs = {c.name: load_or_build(c, args.rebuild, args.update) for c in CORPORA.values()}

    # Batch: pay the model + index loads once, then run every query against both corpora.
    for i, query in enumerate(args.query):
        if len(args.query) > 1:
            print(f"\n##################### [{i + 1}/{len(args.query)}] {query}")
        for name, emb in embs.items():
            print(f"\n========== corpus: {name} ==========")
            run_query(emb, query, args.k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
