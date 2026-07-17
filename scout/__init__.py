"""scout — MCP server exposing the repo's search surfaces to agents.

Tools:
  workspace_search  txtai index over our own writing (spec, notes, proposals)
  papers_search     txtai index over the third-party papers (resources/pdf/)
  docs_search       txtai index over pinned vendor docs (resources/modal-docs/),
                    freshness-stamped (date + TTL; stale pins scream in-results)
  web_search        Gemini + Google Search grounding; sanitized, delimited,
                    citations from grounding metadata

Named `scout` and not `mcp` because the MCP SDK's pip package is literally
`mcp` — a top-level mcp/ dir would shadow it. Same trap class as `modal/`:
this repo's core dependency is the Modal SDK, so a top-level modal/ dir is
equally forbidden.
"""
