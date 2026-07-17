# localmodal

Self-hosted executor serving for a two-tier agent architecture: a hosted
frontier model plans; a self-hosted open-weight model executes, served from
Modal.

Authority lives in [human-owned-spec/](human-owned-spec/) — the human-owned
design truth. The `.vine` file at the root (when it lands) is the plan truth.

- [human-owned-spec/initial-spec.md](human-owned-spec/initial-spec.md) — the executor service spec: serving stack, always-on topology.
- [scout/](scout/) — MCP search server (workspace / papers / pinned vendor docs / grounded web) used to answer provenance questions during design. See its [README](scout/README.md).
- [resources/](resources/) — pinned third-party sources (paper manifest, Modal docs mirror), the freshness ledger, and the semantic-search worker behind scout.
