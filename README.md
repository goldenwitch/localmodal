# localmodal

Self-hosted executor serving for a two-tier agent architecture: a hosted
frontier model plans; a self-hosted open-weight model executes, served from
Modal.

Authority lives in [human-owned-spec/](human-owned-spec/) — the human-owned
design truth.

- [human-owned-spec/initial-spec.md](human-owned-spec/initial-spec.md) — the
  executor service spec: serving stack, always-on topology.
- [localmodal.vine](localmodal.vine) — execution plan, open design gaps, and
  dated evidence; this is the plan truth.
- [scout/](scout/) — MCP search server over one source-bound publication plus
  grounded web leads, used to answer provenance questions during design. See
  its [README](scout/README.md).
- [resources/](resources/) — Scout's explicit initial-source manifest,
  source-state ledger, materializer, publication store, and search workers.
