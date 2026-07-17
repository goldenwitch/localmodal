"""scout — MCP server exposing the repo's search surfaces to agents.

Tools:
  workspace_search  local txtai index over OUR OWN writing (the human-owned
                    spec, notes, proposals); prefer it first, hits self-cite
  papers_search     local txtai index over third-party PDFs (resources/pdf);
                    the literature, hits self-cite as paper#page#chunk
  web_search        Gemini with Google Search grounding (live web; sanitized,
                    untrusted-delimited, citations taken from grounding metadata
                    rather than model-written text)

Named `scout` and not `mcp` because the MCP SDK's pip package is literally
`mcp` — a top-level mcp/ dir would shadow it. Same trap class as `modal/`:
this repo's core dependency is the Modal SDK, so a top-level modal/ dir is
equally forbidden.

Copied from D:/git/JEPA scout (2026-07-17); engine verbatim, corpus
definitions and steering text adapted to this repo.
"""
