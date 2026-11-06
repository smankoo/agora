"""
agora.responder — the autonomous node-to-node reply engine.

Runs each reconcile cycle (no gateway, no MCP tools, no restarts). When a peer
has sent THIS node a message with needs_reply=True, the responder:
  1. reads the pending message(s) and their thread for context,
  2. makes a DIRECT model call (configured backend, e.g. local
     Ollama) to draft a node-to-node reply,
  3. commits the reply straight to the event log via agora.core (acking the
     incoming message),
  4. if the model decides the request genuinely needs the HUMAN operator (spend
     money, send external mail, irreversible action, or it can't answer), it
     posts a visibility='ask' message to the human-recipient instead of
     fabricating.

Doing the reply in-process sidesteps any agent/tool-attachment coupling, is
upgrade-safe, and works offline.

Strict JSON contract from the model:
  {"action": "reply"|"ask"|"skip", "body": "...", "needs_reply": bool}
"""
from __future__ import annotations

import json
import os
import sys

from . import core
from .backends import get_backends
from .config import Config, load_config

DEFAULT_PERSONA = (
    "You are node '{node}', one of several sibling AI nodes that share memory and "
    "coordinate work over a distributed system called Agora. Members: {members}."
)

SYSTEM_TMPL = """{persona}

A sibling node has sent you a message that needs a reply. You are talking
NODE-TO-NODE — the human operator is NOT in this conversation.

Reply as '{node}' would: concise, concrete, genuinely helpful to your sibling.
Engage with what they actually asked — provide info you have, coordinate on a
task, accept or decline, or ask a focused follow-up.

You MUST respond with a single JSON object and nothing else:
{{"action": "reply" | "ask" | "skip", "body": "<message text>", "needs_reply": <true|false>}}

- "reply": your node-to-node answer. needs_reply=true only if you genuinely need
  something back from the sibling to proceed.
- "ask": ONLY when the request truly requires the human operator — spending money,
  sending external email, an irreversible/destructive action, or a decision only
  they can make. The body is shown to the human; write it as a question to them.
- "skip": only if the message needs no reply at all.

Do not invent facts. If you don't know something, say so or ask the sibling."""


def _build_user_prompt(db: str, msg: dict, thread: list[dict]) -> str:
    lines = [f"Message needing your reply is from '{msg['from']}':", f'  "{msg["body"]}"', ""]
    if len(thread) > 1:
        lines.append("Full thread so far (oldest first):")
        for m in thread:
            lines.append(f"  {m['from']} -> {m['to']}: {m['body']}")
        lines.append("")
    if msg.get("task_id"):
        t = core.project(db)["tasks"].get(msg["task_id"])
        if t:
            lines.append(f"Related task: {t.get('title')} (status={t.get('status')}, "
                         f"assignee={t.get('assignee')}, claimed_by={t.get('claimed_by')})")
            lines.append("")
    lines.append("Respond now with the JSON object.")
    return "\n".join(lines)


def _draft(backends: list, system: str, user: str) -> str | None:
    for backend in backends:
        txt = backend(system, user, max_tokens=800)
        if txt:
            return txt
    return None


def _parse(raw: str | None) -> dict | None:
    if not raw:
        return None
    raw = raw.strip()
    if "```" in raw:
        seg = raw.split("```")
        raw = seg[1].lstrip("json").strip() if len(seg) > 1 else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
    except Exception:
        return None
    return obj if obj.get("action") in ("reply", "ask", "skip") else None


def run_once(cfg: Config) -> dict:
    core.init_db(cfg.db_path)
    if not cfg.responder.enabled or cfg.responder.provider == "none":
        return {"node": cfg.node_id, "answered": 0, "disabled": True}

    node = cfg.node_id
    pending = core.pending_replies(cfg.db_path, node)
    if not pending:
        return {"node": node, "answered": 0}

    backends = get_backends(cfg)
    if not backends:
        return {"node": node, "answered": 0, "error": "no backend configured"}

    persona = cfg.responder.persona or DEFAULT_PERSONA
    persona = persona.format(node=node, members=", ".join(sorted(cfg.members)))
    system = SYSTEM_TMPL.format(persona=persona, node=node,
                                members=", ".join(sorted(cfg.members)))

    st = core.project(cfg.db_path)
    answered = 0
    for msg in pending[:cfg.responder.max_replies_per_run]:
        thread = [m for m in st["messages"].values() if m.get("thread_id") == msg.get("thread_id")]
        thread.sort(key=lambda x: x.get("hlc") or "")
        obj = _parse(_draft(backends, system, _build_user_prompt(cfg.db_path, msg, thread)))
        if not obj:
            print(f"[agora-responder] unparseable model output for {msg['message_id']}; leaving pending",
                  file=sys.stderr)
            continue

        body = (obj.get("body") or "").strip()
        core.append_event(cfg.db_path, node, "message_ack", {"message_id": msg["message_id"], "by": node})

        if obj["action"] == "skip" or not body:
            core.mark_woken(cfg.db_path, [msg["message_id"]])
            answered += 1
            continue

        if obj["action"] == "ask":
            core.append_event(cfg.db_path, node, "message_send", {
                "message_id": "msg_" + os.urandom(6).hex(),
                "from": node, "to": cfg.human_recipient, "body": body,
                "task_id": msg.get("task_id"), "thread_id": msg.get("thread_id"),
                "reply_to": msg["message_id"], "needs_reply": False, "visibility": "ask",
            })
        else:  # reply
            core.append_event(cfg.db_path, node, "message_send", {
                "message_id": "msg_" + os.urandom(6).hex(),
                "from": node, "to": msg["from"], "body": body,
                "task_id": msg.get("task_id"), "thread_id": msg.get("thread_id"),
                "reply_to": msg["message_id"],
                "needs_reply": bool(obj.get("needs_reply", False)), "visibility": "silent",
            })
        core.mark_woken(cfg.db_path, [msg["message_id"]])
        answered += 1

    return {"node": node, "answered": answered, "pending_seen": len(pending)}


def main() -> None:
    print(json.dumps(run_once(load_config())))


if __name__ == "__main__":
    main()
