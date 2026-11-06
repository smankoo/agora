# Changelog

All notable changes to Agora are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning is
[SemVer](https://semver.org/).

## [0.1.1] — 2026-11-02

### Fixed
- Heartbeats no longer accumulate. Presence only reads the newest heartbeat per
  node, but every reconcile tick appended a new one and kept it forever, so after
  two months a three-node log was 99.96% heartbeats (~184k events, ~11 MB) and
  every 60 s sync shipped all of it. Nodes now keep only the latest heartbeat
  per node, refuse to re-ingest superseded ones, and compact any backlog on the
  next tick.
- `GET /agora/events` is no longer capped at 50,000 events. The reconciler
  diffs the peer's full id set against this feed, so a truncated feed made every
  event past the cap look missing and get re-POSTed on every cycle.

## [0.1.0] — 2026-09-02

Initial release. Extracted and generalized from a working three-node
deployment into a config-driven, installable package.

### Added
- Event-sourced CRDT core (`agora.core`): append-only SQLite event log, Hybrid
  Logical Clocks, deterministic fold/projection, set-union merge.
- MCP coordination server (`agora.server`): 17 `agora_*` tools (tasks, shared
  state, threaded messaging, presence, board) over streamable-http, plus
  `/agora/health` and `/agora/events` peer-sync routes.
- Full-mesh reconciler (`agora.reconcile`): pull/push event union with every
  configured peer; offline peers skipped and caught up later.
- Autonomous responder (`agora.responder`): answers a peer's `needs_reply`
  message via a direct LLM call, or escalates to the human with
  `visibility="ask"`. Pluggable backends (`agora.backends`):
  local Ollama (automatic offline fallback).
- Human-visibility notifier (`agora.notify`): Telegram provider; `notify`/`ask`
  events pinged, deduped mesh-wide.
- Config layer (`agora.config`): TOML + `AGORA_*` env overrides; no deployment
  specifics in code.
- `agora` CLI: `serve`, `sync`, `respond`, `notify`, `tick`, `board`, `health`,
  `config`, `send`, `version`.
- Install templates: systemd units (Linux) and launchd plists (macOS).
