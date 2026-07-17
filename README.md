# localmodal

Self-hosted executor serving for a two-tier agent architecture: a hosted
frontier model plans; a self-hosted open-weight model executes, served from
Modal.

Authority lives in [human-owned-spec/](human-owned-spec/) — the human-owned
design truth. The `.vine` file at the root (when it lands) is the plan truth.

- [human-owned-spec/initial-spec.md](human-owned-spec/initial-spec.md) — the executor service spec: serving stack, warmth thermostat, 30-day calibration latch, telemetry, day-30 decision rule.
- [scout/](scout/) — MCP search server (workspace / papers / grounded web) used to answer provenance questions during design. See its [README](scout/README.md).
- [resources/](resources/) — the third-party paper manifest and the semantic-search worker behind scout.
