"""
agora.server — the MCP coordination toolset a node exposes to its own agent.

Runs locally on every node, reading/writing ONLY that node's local event log,
so a node coordinates fully offline. A separate reconciler unions logs across
the mesh when peers are reachable.

Transport: streamable-http at ``/mcp``. Also mounts plain-HTTP peer-sync routes:
  GET  /agora/health              -> {ok, node, events, hlc}
  GET  /agora/events?since=<hlc>  -> {events:[...]}   (pull feed for reconciler)
  POST /agora/events              -> {events:[...]} ingested (push)

The recipient vocabulary (which node names are valid for `to`/`assignee`) comes
from config: the node itself, its configured peers, plus 'all' (broadcast) and
the human-recipient escalation target. Nothing is hardcoded.
"""
from __future__ import annotations

import json
import uuid

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import core
from .config import Config, load_config


def build_server(cfg: Config) -> FastMCP:
    core.init_db(cfg.db_path)
    NODE = cfg.node_id
    DB = cfg.db_path
    RECIPIENTS = {NODE, core.BROADCAST, cfg.human_recipient, *cfg.peers.keys()}

    mcp = FastMCP(name="agora")
    mcp.settings.host = cfg.server.host
    mcp.settings.port = cfg.server.port
    # DNS-rebinding host check 421s any non-localhost Host header, which breaks
    # peer access by IP/hostname. Only disable it when the operator has declared
    # the network trusted (e.g. a firewalled private overlay like Tailscale).
    if cfg.server.trust_network:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )

    def emit(kind: str, payload: dict) -> dict:
        return core.append_event(DB, NODE, kind, payload)

    def state() -> dict:
        return core.project(DB)

    def task_view(t: dict) -> dict:
        return {k: t.get(k) for k in (
            "task_id", "title", "description", "status", "assignee", "claimed_by",
            "created_by", "priority", "tags", "visibility", "due", "result", "created_at",
        )}

    def _valid_recipient(name: str) -> bool:
        return name.lower() in RECIPIENTS

    # ============================================================ TASK TOOLS ==
    @mcp.tool()
    def agora_task_create(title: str, assignee: str = "any", description: str = "",
                          priority: str = "normal", tags: list[str] | None = None,
                          visibility: str = "summary", due: str = "",
                          context: str = "") -> str:
        """Create a coordination task for the mesh.

        assignee: a peer node name, this node's name, or 'any' (whoever grabs it first).
        priority: 'low' | 'normal' | 'high' | 'urgent'.
        visibility: how much the human operator sees — 'silent' (nodes only),
          'summary' (in the digest), 'notify' (ping now), 'ask' (a node must ask
          the human before/while doing it). Returns the new task_id.
        """
        if visibility not in core.VALID_VISIBILITY:
            return f"error: visibility must be one of {core.VALID_VISIBILITY}"
        a = assignee.lower()
        if a != "any" and not _valid_recipient(a):
            return f"error: unknown assignee {assignee!r}; valid: {sorted(RECIPIENTS | {'any'})}"
        tid = "task_" + uuid.uuid4().hex[:12]
        emit("task_create", {
            "task_id": tid, "title": title, "description": description,
            "created_by": NODE, "assignee": a, "priority": priority,
            "tags": tags or [], "visibility": visibility, "due": due, "context": context,
        })
        return json.dumps({"ok": True, "task_id": tid, "created_by": NODE,
                           "assignee": a, "visibility": visibility})

    @mcp.tool()
    def agora_task_claim(task_id: str) -> str:
        """Claim a task so peers know THIS node owns it (prevents double work).
        The deterministic fold grants ownership to the first claimant in logical-clock
        order; call agora_task_get after the next sync to confirm you won it."""
        st = state()
        t = st["tasks"].get(task_id)
        if not t:
            return f"error: unknown task_id {task_id}"
        if t.get("status") in ("done", "cancelled"):
            return f"error: task is {t['status']}, cannot claim"
        emit("task_claim", {"task_id": task_id, "claimant": NODE})
        t2 = core.project(DB)["tasks"].get(task_id, {})
        won = t2.get("claimed_by") == NODE
        return json.dumps({"ok": True, "task_id": task_id, "claimed_by": t2.get("claimed_by"),
                           "you_won": won,
                           "note": None if won else "another node holds this claim locally; may change after sync"})

    @mcp.tool()
    def agora_task_unclaim(task_id: str) -> str:
        """Release a task you claimed so another node can pick it up."""
        emit("task_unclaim", {"task_id": task_id, "claimant": NODE})
        return json.dumps({"ok": True, "task_id": task_id, "released_by": NODE})

    @mcp.tool()
    def agora_task_update(task_id: str, status: str = "", note: str = "",
                          result: str = "", assignee: str = "", priority: str = "",
                          visibility: str = "") -> str:
        """Update a task's status/result/assignment and append a note to its history.
        status: open|claimed|in_progress|blocked|done|cancelled. Any node may update
        any task (shared board). Use result='...' when marking done."""
        if status and status not in core.VALID_STATUS:
            return f"error: status must be one of {core.VALID_STATUS}"
        if visibility and visibility not in core.VALID_VISIBILITY:
            return f"error: visibility must be one of {core.VALID_VISIBILITY}"
        p: dict = {"task_id": task_id}
        for k, v in (("status", status), ("note", note), ("result", result),
                     ("assignee", assignee.lower() if assignee else ""),
                     ("priority", priority), ("visibility", visibility)):
            if v:
                p[k] = v
        emit("task_update", p)
        return json.dumps({"ok": True, "task_id": task_id, "updated": [k for k in p if k != "task_id"]})

    @mcp.tool()
    def agora_task_get(task_id: str) -> str:
        """Fetch one task's full current view (folded state) including history."""
        t = state()["tasks"].get(task_id)
        if not t:
            return f"error: unknown task_id {task_id}"
        v = task_view(t)
        v["history"] = t.get("history", [])
        return json.dumps(v, indent=2)

    @mcp.tool()
    def agora_task_list(status: str = "", assignee: str = "", mine: bool = False,
                        include_done: bool = False) -> str:
        """List tasks on the shared board, newest first. Filters: status, assignee,
        mine=True (assigned to or claimed by THIS node). Done/cancelled hidden unless
        include_done=True."""
        out = []
        for t in state()["tasks"].values():
            if not include_done and t.get("status") in ("done", "cancelled"):
                continue
            if status and t.get("status") != status:
                continue
            if assignee and t.get("assignee") != assignee.lower():
                continue
            if mine and NODE not in (t.get("assignee"), t.get("claimed_by")):
                continue
            out.append(task_view(t))
        out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return json.dumps({"count": len(out), "node": NODE, "tasks": out}, indent=2)

    # =========================================================== STATE TOOLS ==
    @mcp.tool()
    def agora_state_set(key: str, value: str) -> str:
        """Set a shared key/value fact all nodes can read. Last write wins
        deterministically across nodes. Store JSON as a string for structure."""
        emit("state_set", {"key": key, "value": value})
        return json.dumps({"ok": True, "key": key, "value": value, "by": NODE})

    @mcp.tool()
    def agora_state_get(key: str = "") -> str:
        """Get one shared state key, or all shared state if key omitted."""
        st = state()["state"]
        if key:
            v = st.get(key)
            return json.dumps({"key": key, **v} if v else {"key": key, "value": None})
        return json.dumps(st, indent=2)

    # ========================================================= MESSAGE TOOLS ==
    @mcp.tool()
    def agora_message_send(to: str, body: str, task_id: str = "",
                           needs_reply: bool = False, visibility: str = "summary",
                           thread_id: str = "") -> str:
        """Send a message to a peer node (or 'all') — the primitive for talking, not
        just assigning tasks. needs_reply=True asks the recipient to answer (it stays
        in their inbox until they reply, and a responder-enabled node will answer
        autonomously). thread_id continues an existing conversation. visibility: see
        agora_task_create ('ask' pings the human because YOU need their input)."""
        t = to.lower()
        if not _valid_recipient(t):
            return f"error: unknown recipient {to!r}; valid: {sorted(RECIPIENTS)}"
        mid = "msg_" + uuid.uuid4().hex[:12]
        emit("message_send", {"message_id": mid, "from": NODE, "to": t, "body": body,
                              "task_id": task_id or None, "needs_reply": bool(needs_reply),
                              "visibility": visibility, "thread_id": thread_id or None})
        tid = core.project(DB)["messages"][mid]["thread_id"]
        return json.dumps({"ok": True, "message_id": mid, "thread_id": tid, "to": t,
                           "needs_reply": bool(needs_reply)})

    @mcp.tool()
    def agora_reply(message_id: str, body: str, needs_reply: bool = False,
                    visibility: str = "summary") -> str:
        """Reply to a specific message, staying in its thread and addressing its sender.
        Auto-acks the message you're replying to. Set needs_reply=True to bounce it
        back for more (a real back-and-forth negotiation)."""
        src = core.project(DB)["messages"].get(message_id)
        if not src:
            return f"error: unknown message_id {message_id}"
        emit("message_ack", {"message_id": message_id, "by": NODE})
        mid = "msg_" + uuid.uuid4().hex[:12]
        emit("message_send", {
            "message_id": mid, "from": NODE, "to": src["from"], "body": body,
            "task_id": src.get("task_id"), "thread_id": src.get("thread_id"),
            "reply_to": message_id, "needs_reply": bool(needs_reply), "visibility": visibility,
        })
        return json.dumps({"ok": True, "message_id": mid, "thread_id": src.get("thread_id"),
                           "to": src["from"], "in_reply_to": message_id})

    @mcp.tool()
    def agora_thread(thread_id: str) -> str:
        """Read a whole conversation thread in order (all messages sharing thread_id)."""
        msgs = [m for m in state()["messages"].values() if m.get("thread_id") == thread_id]
        msgs.sort(key=lambda x: x.get("hlc") or "")
        view = [{"message_id": m["message_id"], "from": m["from"], "to": m["to"],
                 "body": m["body"], "needs_reply": m.get("needs_reply"),
                 "reply_to": m.get("reply_to"), "at": m["created_at"]} for m in msgs]
        return json.dumps({"thread_id": thread_id, "count": len(view), "messages": view}, indent=2)

    @mcp.tool()
    def agora_pending_replies() -> str:
        """List messages addressed to THIS node awaiting a reply from it. Answer each
        with agora_reply."""
        out = []
        for m in state()["messages"].values():
            if not m.get("needs_reply"):
                continue
            if m["to"] not in (NODE, core.BROADCAST) or m["from"] == NODE:
                continue
            if NODE in m.get("acked_by", []):
                continue
            out.append({"message_id": m["message_id"], "from": m["from"], "body": m["body"],
                        "thread_id": m.get("thread_id"), "task_id": m.get("task_id"), "at": m["created_at"]})
        out.sort(key=lambda x: x.get("at") or "", reverse=True)
        return json.dumps({"node": NODE, "count": len(out), "pending": out}, indent=2)

    @mcp.tool()
    def agora_inbox(unacked_only: bool = True) -> str:
        """Read messages addressed to THIS node (or 'all'), newest first."""
        out = []
        for m in state()["messages"].values():
            if m["to"] not in (NODE, core.BROADCAST):
                continue
            if unacked_only and NODE in m.get("acked_by", []):
                continue
            out.append(m)
        out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return json.dumps({"count": len(out), "node": NODE, "messages": out}, indent=2)

    @mcp.tool()
    def agora_message_ack(message_id: str) -> str:
        """Mark a message handled by this node so it drops out of the inbox."""
        emit("message_ack", {"message_id": message_id, "by": NODE})
        return json.dumps({"ok": True, "message_id": message_id, "acked_by": NODE})

    # ======================================================== PRESENCE TOOLS ==
    @mcp.tool()
    def agora_heartbeat(status: str = "online", detail: str = "") -> str:
        """Record that THIS node is alive/working (also emitted by the reconciler
        each cycle). Use it to broadcast a status like 'busy: building X'."""
        emit("heartbeat", {"node": NODE, "status": status, "detail": detail})
        return json.dumps({"ok": True, "node": NODE, "status": status})

    @mcp.tool()
    def agora_presence() -> str:
        """Show each node's last-seen time and status (who's online right now)."""
        return json.dumps(state()["presence"], indent=2)

    @mcp.tool()
    def agora_board() -> str:
        """One-shot overview: active tasks, unread messages for this node, presence,
        and shared state. The 'what's going on' call."""
        st = state()
        active = [task_view(t) for t in st["tasks"].values()
                  if t.get("status") not in ("done", "cancelled")]
        active.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        inbox = [m for m in st["messages"].values()
                 if m["to"] in (NODE, core.BROADCAST) and NODE not in m.get("acked_by", [])]
        return json.dumps({
            "this_node": NODE, "members": sorted(cfg.members),
            "active_tasks": active, "unread_messages": len(inbox),
            "presence": st["presence"],
            "shared_state": {k: v.get("value") for k, v in st["state"].items()},
        }, indent=2)

    # =================================================== PEER-SYNC HTTP ROUTES =
    @mcp.custom_route("/agora/health", methods=["GET"])
    async def health(_req: Request) -> JSONResponse:
        evs = core.get_events(DB, limit=None)
        return JSONResponse({"ok": True, "node": NODE, "events": len(evs),
                             "hlc": evs[-1]["hlc"] if evs else None})

    @mcp.custom_route("/agora/events", methods=["GET"])
    async def events_feed(req: Request) -> JSONResponse:
        since = req.query_params.get("since")
        # No cap: the reconciler diffs the peer's full id set against this feed,
        # so a truncated feed makes every event past the cap look "missing"
        # and get re-pushed on every cycle.
        evs = core.get_events(DB, since_hlc=since, limit=None)
        return JSONResponse({"node": NODE, "count": len(evs), "events": evs})

    @mcp.custom_route("/agora/events", methods=["POST"])
    async def events_push(req: Request) -> JSONResponse:
        body = await req.json()
        n = core.ingest_events(DB, NODE, body.get("events", []))
        return JSONResponse({"ok": True, "ingested": n})

    return mcp


def main() -> None:
    import sys
    cfg = load_config()
    print(f"[agora] node={cfg.node_id} db={cfg.db_path} "
          f"bind={cfg.server.host}:{cfg.server.port} peers={list(cfg.peers)}", file=sys.stderr)
    build_server(cfg).run(transport="streamable-http")


if __name__ == "__main__":
    main()
