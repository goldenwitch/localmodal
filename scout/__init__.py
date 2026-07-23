"""scout — MCP server exposing the repo's search surfaces to agents.

Tools:
  scout_search      validated search over the active source-bound publication
  source_read       resolve a citation to its committed source snapshot or VINE block
  source_propose    explicit ordered source add/remove batch
  refresh_stale     re-materialize stale or absent registered sources
  workspace_search, papers_search, docs_search
                    compatibility aliases before activation; after activation
                    they resolve scout_search rather than separate indexes
  web_search        Gemini + Google Search grounding; sanitized, delimited,
                    citations from grounding metadata

Named `scout` and not `mcp` because the MCP SDK's pip package is literally
`mcp` — a top-level mcp/ dir would shadow it. Same trap class as `modal/`:
this repo's core dependency is the Modal SDK, so a top-level modal/ dir is
equally forbidden.
"""
