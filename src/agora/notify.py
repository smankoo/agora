"""
agora.notify — the human-visibility layer.

For events THIS node created whose visibility is 'notify' or 'ask' and which
haven't yet been marked delivered (no notify_delivered event anywhere in the
folded log), send one message via the configured notify provider, then emit a
notify_delivered marker. Keying on created_by guarantees exactly ONE node sends,
mesh-wide, even though every node holds the same event after sync.

Visibility levels:
  silent   -> nobody is notified
  summary  -> not pinged here (belongs in a periodic digest)
  notify   -> immediate FYI to the human
  ask      -> immediate ping framed as needing the human's input

Providers are pluggable; 'telegram' ships built in. Creds come from config/env
so config.toml stays commit-safe.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from . import core
from .config import Config, load_config

ICON = {"ask": "\u2753", "notify": "\U0001f514"}  # ❓ / 🔔


def _send_telegram(token: str, chat_id: str, text: str, thread_id: str = "") -> bool:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
               "disable_web_page_preview": True}
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            pass
    data = json.dumps(payload).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        print(f"[agora-notify] telegram send failed: {e}", file=sys.stderr)
        return False


def _format(ev: dict, task: dict | None) -> str:
    p = ev["payload"]
    vis = p.get("visibility") or (task or {}).get("visibility") or "notify"
    node = ev["node_id"].capitalize()
    icon = ICON.get(vis, "\U0001f514")
    kind = ev["kind"]
    if kind == "task_create":
        body = f"New task *{p.get('title','(untitled)')}* assigned to *{p.get('assignee','any')}*."
        if vis == "ask":
            body += "\n_Needs your input before it proceeds._"
        ctx = p.get("context") or p.get("description")
        if ctx:
            body += f"\n\n{ctx[:500]}"
        return f"{icon} *{node} \u2192 Agora*\n{body}\n\n`{p['task_id']}`"
    if kind == "message_send":
        return (f"{icon} *{node} \u2192 you*\n{p.get('body','')[:800]}"
                + (f"\n\n(re task `{p['task_id']}`)" if p.get("task_id") else ""))
    if kind == "task_update":
        bits = []
        if p.get("status"):
            bits.append(f"status \u2192 *{p['status']}*")
        if p.get("note"):
            bits.append(p["note"])
        if p.get("result"):
            bits.append(f"result: {p['result'][:400]}")
        title = (task or {}).get("title", p.get("task_id"))
        return f"{icon} *{node}* updated *{title}*\n" + "\n".join(bits)
    return f"{icon} *{node}*: {json.dumps(p)[:500]}"


def run_once(cfg: Config) -> dict:
    core.init_db(cfg.db_path)
    if not cfg.notify.enabled or cfg.notify.provider == "none":
        return {"ok": True, "node": cfg.node_id, "sent": 0, "disabled": True}
    if cfg.notify.provider != "telegram":
        return {"ok": False, "reason": f"unknown notify provider {cfg.notify.provider!r}"}
    token, chat = cfg.notify.token, cfg.notify.chat_id
    if not token or not chat:
        return {"ok": False, "reason": "notify token/chat_id not configured"}

    node = cfg.node_id
    st = core.project(cfg.db_path)
    delivered = st["delivered"]
    sent = 0
    for ev in core.get_events(cfg.db_path, limit=1000000):
        if ev["node_id"] != node or ev["event_id"] in delivered:
            continue
        p = ev["payload"]
        vis = p.get("visibility")
        if vis is None:
            tid = p.get("task_id")
            vis = (st["tasks"].get(tid, {}) or {}).get("visibility") if tid else None
        if vis not in ("notify", "ask"):
            continue
        if ev["kind"] not in ("task_create", "task_update", "message_send"):
            continue
        task = st["tasks"].get(p.get("task_id")) if p.get("task_id") else None
        if _send_telegram(token, chat, _format(ev, task), cfg.notify.thread_id):
            core.append_event(cfg.db_path, node, "notify_delivered", {"ref_event_id": ev["event_id"]})
            sent += 1
    return {"ok": True, "node": node, "sent": sent}


def main() -> None:
    print(json.dumps(run_once(load_config())))


if __name__ == "__main__":
    main()
