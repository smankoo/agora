"""
agora.config — single source of truth for a node's configuration.

Agora is generic and config-driven: nothing about a particular deployment
(node names, peer addresses, model choice, notifier creds) is baked into the
code. A node is configured by a TOML file, with environment-variable overrides
for anything a service manager or secret store wants to inject.

Resolution order (later wins):
  1. built-in defaults (below)
  2. the TOML config file (``$AGORA_CONFIG`` or ``~/.config/agora/config.toml``)
  3. ``AGORA_*`` environment variables

A minimal config.toml::

    node_id = "nodeA"

    [server]
    host = "0.0.0.0"
    port = 3130

    [peers]
    nodeB = "http://100.64.0.2:3130"
    nodeC = "http://100.64.0.3:3130"

    [responder]
    enabled = true
    provider = "ollama"            # ollama | none
    ollama_url = "http://127.0.0.1:11434"
    ollama_model = "gemma3:12b"

    [notify]
    enabled = true
    provider = "telegram"          # telegram | none
    # token/chat_id resolved from env by default (see below)

Secrets are never written to the TOML by convention; the notifier reads its
token/chat from env (``AGORA_NOTIFY_TOKEN`` / ``AGORA_NOTIFY_CHAT_ID``, or a
declared ``env_file``) so config.toml stays commit-safe.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # py3.11+ stdlib
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/agora/config.toml")
DEFAULT_DB = os.path.expanduser("~/.local/share/agora/agora.db")


def default_config_path() -> str:
    return os.environ.get("AGORA_CONFIG", DEFAULT_CONFIG_PATH)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 3130
    # DNS-rebinding protection off by default is UNSAFE on a public bind; we keep
    # it ON unless the operator explicitly trusts the network (tailnet + firewall).
    trust_network: bool = False


@dataclass
class ResponderConfig:
    enabled: bool = False
    provider: str = "none"  # ollama | none
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gemma3:12b"
    max_replies_per_run: int = 5
    # identity/persona the responder speaks with (generic default)
    persona: str = ""


@dataclass
class NotifyConfig:
    enabled: bool = False
    provider: str = "none"  # telegram | none
    token: str = ""
    chat_id: str = ""
    thread_id: str = ""


@dataclass
class Config:
    node_id: str
    db_path: str = DEFAULT_DB
    peers: dict[str, str] = field(default_factory=dict)
    server: ServerConfig = field(default_factory=ServerConfig)
    responder: ResponderConfig = field(default_factory=ResponderConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    sync_timeout: float = 6.0
    # the set of valid recipients for messages/tasks = peers + self + broadcast.
    # 'human' is the reserved recipient the notifier escalates 'ask' items to.
    human_recipient: str = "human"

    @property
    def members(self) -> set[str]:
        return {self.node_id, *self.peers.keys()}


def _load_toml(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("rb") as f:
        return tomllib.load(f)


def _envs(prefix: str = "AGORA_") -> dict[str, str]:
    return {k[len(prefix):].lower(): v for k, v in os.environ.items() if k.startswith(prefix)}


def load_config(path: str | None = None) -> Config:
    """Build a Config from TOML + env overrides. Never raises on a missing file
    (a node can be fully env-configured); raises only if node_id can't be found."""
    path = path or default_config_path()
    raw = _load_toml(path)
    env = _envs()

    node_id = env.get("node_id") or raw.get("node_id") or ""
    node_id = node_id.strip().lower()
    if not node_id:
        raise ValueError(
            "agora: node_id is required (set it in config.toml or AGORA_NODE_ID)"
        )

    db_path = os.path.expanduser(env.get("db") or raw.get("db_path") or DEFAULT_DB)

    peers = dict(raw.get("peers", {}) or {})
    # env peers as AGORA_PEER_<name>=<url>
    for k, v in os.environ.items():
        if k.startswith("AGORA_PEER_"):
            peers[k[len("AGORA_PEER_"):].lower()] = v

    s = raw.get("server", {}) or {}
    server = ServerConfig(
        host=env.get("host") or s.get("host", ServerConfig.host),
        port=int(env.get("port") or s.get("port", ServerConfig.port)),
        trust_network=_as_bool(env.get("trust_network"), s.get("trust_network", False)),
    )

    r = raw.get("responder", {}) or {}
    responder = ResponderConfig(
        enabled=_as_bool(env.get("responder_enabled"), r.get("enabled", False)),
        provider=env.get("responder_provider") or r.get("provider", "none"),
        ollama_url=env.get("ollama_url") or r.get("ollama_url", ResponderConfig.ollama_url),
        ollama_model=env.get("ollama_model") or r.get("ollama_model", ResponderConfig.ollama_model),
        max_replies_per_run=int(env.get("max_replies") or r.get("max_replies_per_run", 5)),
        persona=env.get("responder_persona") or r.get("persona", ""),
    )

    n = raw.get("notify", {}) or {}
    notify = NotifyConfig(
        enabled=_as_bool(env.get("notify_enabled"), n.get("enabled", False)),
        provider=env.get("notify_provider") or n.get("provider", "none"),
        token=env.get("notify_token") or n.get("token", ""),
        chat_id=env.get("notify_chat_id") or str(n.get("chat_id", "")),
        thread_id=env.get("notify_thread_id") or str(n.get("thread_id", "")),
    )

    return Config(
        node_id=node_id, db_path=db_path, peers=peers, server=server,
        responder=responder, notify=notify,
        sync_timeout=float(env.get("sync_timeout") or raw.get("sync_timeout", 6.0)),
        human_recipient=env.get("human_recipient") or raw.get("human_recipient", "human"),
    )


def _as_bool(*vals: Any) -> bool:
    for v in vals:
        if v is None:
            continue
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    return False
