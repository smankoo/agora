"""agora.cli — the ``agora`` command-line entrypoint.

Subcommands:
  agora serve            run the MCP coordination server (long-lived)
  agora sync             run one reconcile cycle (pull/push with peers)
  agora respond          run one responder pass (autonomous replies)
  agora notify           run one notifier pass (human-visibility)
  agora tick             sync -> respond -> notify, one pass (the timer target)
  agora board            print the current board as JSON
  agora health           print local {node, events, hlc}
  agora config           print the resolved config (secrets redacted)
  agora send <to> <msg>  send a message to a peer (ops/testing)
  agora version
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import load_config


def _cmd_serve(args) -> int:
    import sys
    from .server import build_server
    cfg = load_config(args.config)
    print(f"[agora] node={cfg.node_id} db={cfg.db_path} "
          f"bind={cfg.server.host}:{cfg.server.port} peers={list(cfg.peers)}", file=sys.stderr)
    build_server(cfg).run(transport="streamable-http")
    return 0


def _cmd_sync(args) -> int:
    from .reconcile import reconcile_once
    print(json.dumps(reconcile_once(load_config(args.config), verbose=False)))
    return 0


def _cmd_respond(args) -> int:
    from .responder import run_once
    print(json.dumps(run_once(load_config(args.config))))
    return 0


def _cmd_notify(args) -> int:
    from .notify import run_once
    print(json.dumps(run_once(load_config(args.config))))
    return 0


def _cmd_tick(args) -> int:
    """One full maintenance pass: reconcile, then respond, then notify.
    This is what a systemd timer / launchd StartInterval job runs."""
    from .reconcile import reconcile_once
    from .responder import run_once as respond_once
    from .notify import run_once as notify_once
    cfg = load_config(args.config)
    out = {
        "sync": reconcile_once(cfg, verbose=False),
        "respond": respond_once(cfg),
        "notify": notify_once(cfg),
    }
    print(json.dumps(out))
    return 0


def _cmd_board(args) -> int:
    from . import core
    cfg = load_config(args.config)
    core.init_db(cfg.db_path)
    st = core.project(cfg.db_path)
    active = [t for t in st["tasks"].values() if t.get("status") not in ("done", "cancelled")]
    print(json.dumps({
        "node": cfg.node_id, "members": sorted(cfg.members),
        "active_tasks": len(active),
        "tasks": active,
        "presence": st["presence"],
        "shared_state": {k: v.get("value") for k, v in st["state"].items()},
    }, indent=2))
    return 0


def _cmd_health(args) -> int:
    from . import core
    cfg = load_config(args.config)
    core.init_db(cfg.db_path)
    evs = core.get_events(cfg.db_path, limit=1000000)
    print(json.dumps({"ok": True, "node": cfg.node_id, "events": len(evs),
                      "hlc": evs[-1]["hlc"] if evs else None}))
    return 0


def _cmd_config(args) -> int:
    cfg = load_config(args.config)
    redacted_token = "***set***" if cfg.notify.token else ""
    print(json.dumps({
        "node_id": cfg.node_id, "db_path": cfg.db_path, "peers": cfg.peers,
        "server": {"host": cfg.server.host, "port": cfg.server.port,
                   "trust_network": cfg.server.trust_network},
        "responder": {"enabled": cfg.responder.enabled, "provider": cfg.responder.provider,
                      "ollama_model": cfg.responder.ollama_model},
        "notify": {"enabled": cfg.notify.enabled, "provider": cfg.notify.provider,
                   "token": redacted_token, "chat_id": cfg.notify.chat_id},
        "human_recipient": cfg.human_recipient,
    }, indent=2))
    return 0


def _cmd_send(args) -> int:
    from . import core
    cfg = load_config(args.config)
    core.init_db(cfg.db_path)
    import uuid
    mid = "msg_" + uuid.uuid4().hex[:12]
    core.append_event(cfg.db_path, cfg.node_id, "message_send", {
        "message_id": mid, "from": cfg.node_id, "to": args.to.lower(), "body": args.body,
        "needs_reply": args.needs_reply, "visibility": args.visibility, "thread_id": args.thread or None,
    })
    print(json.dumps({"ok": True, "message_id": mid, "to": args.to.lower()}))
    return 0


def _cmd_version(args) -> int:
    print(f"agora {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agora", description=__doc__.splitlines()[0])
    p.add_argument("-c", "--config", default=None, help="path to config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the MCP coordination server").set_defaults(fn=_cmd_serve)
    sub.add_parser("sync", help="one reconcile cycle").set_defaults(fn=_cmd_sync)
    sub.add_parser("respond", help="one responder pass").set_defaults(fn=_cmd_respond)
    sub.add_parser("notify", help="one notifier pass").set_defaults(fn=_cmd_notify)
    sub.add_parser("tick", help="sync -> respond -> notify, one pass").set_defaults(fn=_cmd_tick)
    sub.add_parser("board", help="print current board").set_defaults(fn=_cmd_board)
    sub.add_parser("health", help="print local node health").set_defaults(fn=_cmd_health)
    sub.add_parser("config", help="print resolved config (redacted)").set_defaults(fn=_cmd_config)
    sub.add_parser("version", help="print version").set_defaults(fn=_cmd_version)

    s = sub.add_parser("send", help="send a message to a peer (ops/testing)")
    s.add_argument("to")
    s.add_argument("body")
    s.add_argument("--needs-reply", action="store_true", dest="needs_reply")
    s.add_argument("--visibility", default="summary")
    s.add_argument("--thread", default="")
    s.set_defaults(fn=_cmd_send)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
