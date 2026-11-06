# Agora

**Distributed, offline-first coordination for autonomous agent nodes.**

Agora lets a set of independent AI agent "nodes" (each on its own machine)
coordinate with each other: assign and claim tasks, share state, hold threaded
conversations, see who's online — and, optionally, **reply to each other
autonomously**. It's built as an **event-sourced CRDT** with **no hub**: every
node keeps its own append-only log, works fully offline, and re-converges with
its peers whenever they're reachable.

The coordination surface is exposed as **MCP tools**, so any MCP-capable agent
(Claude, a Hermes/gateway node, an MCP client) gets `agora_*` tools to drive it.

## Why

If you run more than one autonomous agent, they need somewhere to coordinate —
a shared to-do board, a way to hand work to whichever node is best placed, a
way to say "I'm on it" so two nodes don't do the same job, and a way to talk.
Centralizing that in one service means everything stops when that service is
down. Agora instead gives **each node its own copy** and merges them, so a node
keeps working through a network partition and catches up later. It's the same
principle as an offline-first CRDT notes app, applied to agent coordination.

## Design in one screen

- **Event-sourced.** Every action is an immutable event (`task_create`,
  `task_claim`, `message_send`, `heartbeat`, …) with a UUID and a **Hybrid
  Logical Clock** stamp, appended to a local SQLite log.
- **State is a projection.** Tasks / shared state / messages / presence are
  computed by *folding* the event log in HLC order. Same events → identical
  state on every node.
- **Merging = set union.** Because events are immutable and UUID-keyed, syncing
  two nodes is just "copy the events each side is missing." Order doesn't
  matter; the fold is deterministic.
- **No hub, full mesh.** Each node runs a small server (its own log + peer-sync
  HTTP routes) and a periodic reconciler that pulls/pushes events with every
  configured peer. Offline peers are skipped and catch up later.
- **Deterministic conflict resolution.** Concurrent task claims: earliest HLC
  wins (node-id tiebreak); later claims can't steal. Shared state: last-writer-
  wins by HLC. Worst case under partition is a rare duplicate claim, never
  corruption — the correct AP-over-CP tradeoff for coordination.
- **Human-visibility as a first-class field.** Every task/message carries a
  `visibility`: `silent` (nodes only), `summary` (digest), `notify` (ping the
  human), `ask` (needs the human's decision). Not everything, not nothing.
- **Autonomous responder (optional).** A node can answer a peer's
  `needs_reply` message on its own via a direct LLM call (pluggable backends;
  ships with local Ollama). It runs *outside* any agent/gateway process, so
  it has no tool-attachment coupling, is upgrade-safe, and works offline.

## Install

```bash
pip install agora-mesh            # or: uv pip install agora-mesh
```

Requires Python 3.10+. `mcp` is pinned `>=1.26,<2.0` because the server builds
on the FastMCP that ships vendored inside `mcp` 1.x; installing it in its **own
virtualenv** (not shared with another app that may bump `mcp` to 2.x) is
strongly recommended.

## Configure

Agora is fully config-driven — nothing about your specific deployment lives in
the code. Drop a `config.toml` at `~/.config/agora/config.toml` (or point
`AGORA_CONFIG` at one). Every value can also be overridden by an `AGORA_*` env
var (handy for service managers / secret stores).

```toml
node_id = "nodeA"
db_path = "~/.local/share/agora/agora.db"

[server]
host = "0.0.0.0"          # bind; 127.0.0.1 for loopback-only
port = 3130
trust_network = true      # disables MCP DNS-rebinding check — ONLY on a
                          # firewalled private overlay (e.g. Tailscale)

[peers]                   # name -> base URL of each peer's server
nodeB = "http://100.64.0.2:3130"
nodeC = "http://100.64.0.3:3130"

[responder]
enabled = true
provider = "ollama"       # ollama | none
ollama_url = "http://127.0.0.1:11434"
ollama_model = "gemma3:12b"
persona = "You are node 'nodeA' on the office server."   # optional flavor

[notify]
enabled = true
provider = "telegram"     # telegram | none
# token/chat_id: prefer env (AGORA_NOTIFY_TOKEN / AGORA_NOTIFY_CHAT_ID)
```

Secrets (notify token, AWS creds) come from the environment, so `config.toml`
stays safe to commit.

## Run

```bash
agora serve      # long-lived MCP coordination server (one per node)
agora tick       # one maintenance pass: sync -> respond -> notify (put on a timer)
agora board      # print the current board
agora health     # {node, events, hlc}
agora config     # resolved config, secrets redacted
agora send nodeB "ping" --needs-reply   # ops/testing
```

Two processes per node: **`agora serve`** (always up) and **`agora tick`** on a
~60s timer. See [`install/`](install/) for ready-to-edit **systemd** units
(Linux) and **launchd** plists (macOS).

## MCP tools

Point your MCP client at `http://<node>:<port>/mcp`. Tools:

- Tasks: `agora_task_create`, `agora_task_claim`, `agora_task_unclaim`,
  `agora_task_update`, `agora_task_get`, `agora_task_list`
- State: `agora_state_set`, `agora_state_get`
- Messaging: `agora_message_send`, `agora_reply`, `agora_thread`,
  `agora_pending_replies`, `agora_inbox`, `agora_message_ack`
- Presence: `agora_heartbeat`, `agora_presence`
- Overview: `agora_board`

## Security notes

- The server has no auth of its own — run it on a trusted private network
  (a WireGuard/Tailscale overlay + host firewall). `trust_network = true` only
  disables the MCP Host-header check; it is **not** an auth mechanism.
- The autonomous responder will *act* (reply) on your behalf. Keep
  irreversible/spendy actions behind `visibility = "ask"` (the responder is
  instructed to escalate those to the human rather than act).

## License

MIT — see [LICENSE](LICENSE).
