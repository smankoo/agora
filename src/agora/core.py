"""
agora.core — the distributed, offline-first coordination engine.

An append-only, event-sourced CRDT:

- Every action is an immutable EVENT with a UUID id and a Hybrid Logical Clock
  (HLC) timestamp. Events are never mutated or deleted.
- Current state (tasks, shared key/value state, messages, presence) is a
  PROJECTION computed by folding events in HLC order. Last-writer-wins per
  field, resolved deterministically by (hlc, node_id).
- Because events are immutable and keyed by UUID, merging two nodes' logs is a
  pure SET UNION, so any node converges to identical state given the same events.
- No hub. Each node keeps its own SQLite log and works fully offline; a
  full-mesh reconciler pulls/pushes events when peers are reachable and the fold
  re-converges deterministically everywhere.

Dependency-free (stdlib only) so it runs identically anywhere Python 3.10+ does.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any, Iterable

SCHEMA_VERSION = 1

# event kinds:
#   task_create, task_update, task_claim, task_unclaim
#   state_set
#   message_send, message_ack
#   heartbeat
#   notify_delivered   (dedup marker: a node delivered a notify/ask to the human)

VALID_VISIBILITY = ("silent", "summary", "notify", "ask")
VALID_STATUS = ("open", "claimed", "in_progress", "blocked", "done", "cancelled")

BROADCAST = "all"


def _connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db(db_path: str) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id   TEXT PRIMARY KEY,
                hlc        TEXT NOT NULL,
                node_id    TEXT NOT NULL,
                kind       TEXT NOT NULL,
                payload    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_hlc ON events(hlc);
            CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
            CREATE TABLE IF NOT EXISTS hlc_state (
                id       INTEGER PRIMARY KEY CHECK (id = 1),
                last_ms  INTEGER NOT NULL,
                counter  INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO hlc_state (id, last_ms, counter) VALUES (1, 0, 0);
            -- Node-LOCAL dedup for reply-handling side effects (waking a responder,
            -- etc). Deliberately NOT an event: it must never propagate/replay.
            CREATE TABLE IF NOT EXISTS woken (
                message_id TEXT PRIMARY KEY,
                woken_at   TEXT NOT NULL
            );
            """
        )
    finally:
        conn.close()


# ---- Hybrid Logical Clock --------------------------------------------------
# hlc string form: "<15-digit ms>:<6-digit counter>:<node_id>"  -> lexicographically sortable.

def _parse_hlc(hlc: str) -> tuple[int, int]:
    ms, counter, _node = hlc.split(":", 2)
    return int(ms), int(counter)


def _fmt_hlc(ms: int, counter: int, node_id: str) -> str:
    return f"{ms:015d}:{counter:06d}:{node_id}"


def next_hlc(conn: sqlite3.Connection, node_id: str, observed_hlc: str | None = None) -> str:
    """Advance the local HLC (optionally merging an observed remote HLC) and return it."""
    now_ms = int(time.time() * 1000)
    row = conn.execute("SELECT last_ms, counter FROM hlc_state WHERE id=1").fetchone()
    last_ms, counter = (row["last_ms"], row["counter"]) if row else (0, 0)

    obs_ms, obs_counter = _parse_hlc(observed_hlc) if observed_hlc else (0, 0)

    new_ms = max(last_ms, obs_ms, now_ms)
    if new_ms == last_ms and new_ms == obs_ms:
        counter = max(counter, obs_counter) + 1
    elif new_ms == last_ms:
        counter = counter + 1
    elif new_ms == obs_ms:
        counter = obs_counter + 1
    else:  # now_ms advanced past both
        counter = 0

    conn.execute("UPDATE hlc_state SET last_ms=?, counter=? WHERE id=1", (new_ms, counter))
    return _fmt_hlc(new_ms, counter, node_id)


def observe_hlc(conn: sqlite3.Connection, node_id: str, remote_hlc: str) -> None:
    """Merge an incoming event's HLC into the local clock without emitting."""
    row = conn.execute("SELECT last_ms, counter FROM hlc_state WHERE id=1").fetchone()
    last_ms, counter = (row["last_ms"], row["counter"]) if row else (0, 0)
    obs_ms, obs_counter = _parse_hlc(remote_hlc)
    now_ms = int(time.time() * 1000)
    new_ms = max(last_ms, obs_ms, now_ms)
    if new_ms == last_ms and new_ms == obs_ms:
        counter = max(counter, obs_counter)
    elif new_ms == obs_ms:
        counter = obs_counter
    conn.execute("UPDATE hlc_state SET last_ms=?, counter=? WHERE id=1", (new_ms, counter))


# ---- append / ingest -------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def append_event(db_path: str, node_id: str, kind: str, payload: dict[str, Any],
                 observed_hlc: str | None = None) -> dict[str, Any]:
    """Emit a new local event and persist it. Returns the event row."""
    conn = _connect(db_path)
    try:
        hlc = next_hlc(conn, node_id, observed_hlc)
        ev = {
            "event_id": str(uuid.uuid4()),
            "hlc": hlc,
            "node_id": node_id,
            "kind": kind,
            "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "created_at": _now_iso(),
        }
        conn.execute(
            "INSERT INTO events (event_id,hlc,node_id,kind,payload,created_at) VALUES (?,?,?,?,?,?)",
            (ev["event_id"], ev["hlc"], ev["node_id"], ev["kind"], ev["payload"], ev["created_at"]),
        )
        if kind == "heartbeat":
            _prune_heartbeats(conn, payload.get("node"))
        ev["payload"] = payload
        return ev
    finally:
        conn.close()


# ---- Heartbeat compaction ---------------------------------------------------
# Presence only ever reads the highest-HLC heartbeat per node (see project()),
# so older heartbeats carry no information. Keeping them made the log grow by
# one event per node per reconcile tick, forever, and every sync re-shipped all
# of them. Each node therefore keeps just the latest heartbeat per node and
# refuses to re-ingest older ones, so peers can't push pruned rows back. The
# kept row is the same on every node (max HLC), so the union still converges.

def _latest_heartbeat_hlc(conn: sqlite3.Connection, node: str | None) -> str | None:
    row = conn.execute(
        "SELECT MAX(hlc) FROM events WHERE kind='heartbeat' AND json_extract(payload,'$.node') IS ?",
        (node,),
    ).fetchone()
    return row[0] if row else None


def _prune_heartbeats(conn: sqlite3.Connection, node: str | None) -> int:
    latest = _latest_heartbeat_hlc(conn, node)
    if latest is None:
        return 0
    cur = conn.execute(
        "DELETE FROM events WHERE kind='heartbeat' AND json_extract(payload,'$.node') IS ? AND hlc < ?",
        (node, latest),
    )
    return cur.rowcount


def compact_heartbeats(db_path: str) -> int:
    """Drop every heartbeat that isn't the latest for its node. Returns #deleted."""
    conn = _connect(db_path)
    try:
        nodes = [r[0] for r in conn.execute(
            "SELECT DISTINCT json_extract(payload,'$.node') FROM events WHERE kind='heartbeat'")]
        return sum(_prune_heartbeats(conn, n) for n in nodes)
    finally:
        conn.close()


def ingest_events(db_path: str, node_id: str, events: Iterable[dict[str, Any]]) -> int:
    """Union foreign events into the local log (idempotent by event_id). Returns #new."""
    conn = _connect(db_path)
    new = 0
    try:
        for ev in events:
            exists = conn.execute("SELECT 1 FROM events WHERE event_id=?", (ev["event_id"],)).fetchone()
            if exists:
                continue
            payload = ev["payload"]
            if ev["kind"] == "heartbeat":
                p = payload if isinstance(payload, dict) else json.loads(payload)
                latest = _latest_heartbeat_hlc(conn, p.get("node"))
                if latest is not None and ev["hlc"] <= latest:
                    continue  # superseded (and possibly already pruned) — don't resurrect
            if isinstance(payload, (dict, list)):
                payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            conn.execute(
                "INSERT INTO events (event_id,hlc,node_id,kind,payload,created_at) VALUES (?,?,?,?,?,?)",
                (ev["event_id"], ev["hlc"], ev["node_id"], ev["kind"], payload, ev["created_at"]),
            )
            observe_hlc(conn, node_id, ev["hlc"])
            if ev["kind"] == "heartbeat":
                _prune_heartbeats(conn, p.get("node"))
            new += 1
        return new
    finally:
        conn.close()


def get_events(db_path: str, since_hlc: str | None = None,
               limit: int | None = 10000) -> list[dict[str, Any]]:
    """Events in HLC order. limit=None returns all of them."""
    conn = _connect(db_path)
    lim = -1 if limit is None else limit  # SQLite: LIMIT -1 means no limit
    try:
        if since_hlc:
            rows = conn.execute(
                "SELECT * FROM events WHERE hlc > ? ORDER BY hlc ASC LIMIT ?", (since_hlc, lim)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY hlc ASC LIMIT ?", (lim,)).fetchall()
        return [_row_to_event(r) for r in rows]
    finally:
        conn.close()


def _row_to_event(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": r["event_id"], "hlc": r["hlc"], "node_id": r["node_id"],
        "kind": r["kind"], "payload": json.loads(r["payload"]), "created_at": r["created_at"],
    }


def event_ids(db_path: str) -> set[str]:
    conn = _connect(db_path)
    try:
        return {r["event_id"] for r in conn.execute("SELECT event_id FROM events").fetchall()}
    finally:
        conn.close()


# ---- reply-handling support (node-local, non-propagating) ------------------

def pending_replies(db_path: str, node_id: str, include_woken: bool = False) -> list[dict[str, Any]]:
    """Messages addressed to THIS node that need a reply, aren't from this node,
    and aren't already acked by it. By default also excludes any already marked
    handled in the local `woken` table. Newest-first."""
    st = project(db_path)
    already: set[str] = set()
    if not include_woken:
        conn = _connect(db_path)
        try:
            already = {r["message_id"] for r in conn.execute("SELECT message_id FROM woken").fetchall()}
        finally:
            conn.close()
    out = []
    for m in st["messages"].values():
        if not m.get("needs_reply"):
            continue
        if m["to"] not in (node_id, BROADCAST):
            continue
        if m["from"] == node_id:
            continue
        if node_id in m.get("acked_by", []):
            continue
        if m["message_id"] in already:
            continue
        out.append(m)
    out.sort(key=lambda x: x.get("hlc") or "", reverse=True)
    return out


def mark_woken(db_path: str, message_ids: Iterable[str]) -> None:
    conn = _connect(db_path)
    try:
        for mid in message_ids:
            conn.execute("INSERT OR IGNORE INTO woken (message_id, woken_at) VALUES (?,?)",
                         (mid, _now_iso()))
    finally:
        conn.close()


# ---- projection (fold) -----------------------------------------------------

def _all_events_sorted(db_path: str) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM events ORDER BY hlc ASC").fetchall()
        return [_row_to_event(r) for r in rows]
    finally:
        conn.close()


def project(db_path: str) -> dict[str, Any]:
    """Fold the event log into current state. Deterministic given the same event set."""
    tasks: dict[str, dict[str, Any]] = {}
    task_field_hlc: dict[str, dict[str, str]] = {}
    state: dict[str, dict[str, Any]] = {}
    messages: dict[str, dict[str, Any]] = {}
    presence: dict[str, dict[str, Any]] = {}
    delivered: set[str] = set()

    def field_wins(tid: str, fld: str, hlc: str) -> bool:
        prev = task_field_hlc.setdefault(tid, {}).get(fld)
        if prev is None or hlc > prev:
            task_field_hlc[tid][fld] = hlc
            return True
        return False

    for ev in _all_events_sorted(db_path):
        k, p, hlc = ev["kind"], ev["payload"], ev["hlc"]
        if k == "task_create":
            tid = p["task_id"]
            t = tasks.setdefault(tid, {"task_id": tid, "status": "open", "claimed_by": None, "history": []})
            for f in ("title", "description", "created_by", "assignee", "priority",
                      "tags", "visibility", "due", "context"):
                if f in p and field_wins(tid, f, hlc):
                    t[f] = p[f]
            t.setdefault("status", "open")
            if field_wins(tid, "created_at", hlc):
                t["created_at"] = ev["created_at"]
            t["history"].append({"hlc": hlc, "node": ev["node_id"], "action": "created"})
        elif k == "task_update":
            tid = p["task_id"]
            t = tasks.setdefault(tid, {"task_id": tid, "status": "open", "claimed_by": None, "history": []})
            for f in ("title", "description", "assignee", "priority", "tags",
                      "visibility", "status", "result", "due", "context"):
                if f in p and field_wins(tid, f, hlc):
                    t[f] = p[f]
            t["history"].append({"hlc": hlc, "node": ev["node_id"], "action": "update", "note": p.get("note")})
        elif k == "task_claim":
            tid = p["task_id"]
            t = tasks.setdefault(tid, {"task_id": tid, "status": "open", "claimed_by": None, "history": []})
            # Events fold in strict HLC order, so the FIRST claim while unclaimed wins
            # (concurrent claims -> earliest HLC, node_id tiebreak); later claims can't
            # steal. After an unclaim, a fresh claim succeeds (ownership back to None).
            if t.get("claimed_by") is None:
                t["claimed_by"] = p["claimant"]
                if t.get("status") in (None, "open"):
                    t["status"] = "claimed"
            t["history"].append({"hlc": hlc, "node": ev["node_id"], "action": "claim",
                                 "claimant": p["claimant"], "won": t.get("claimed_by") == p["claimant"]})
        elif k == "task_unclaim":
            tid = p["task_id"]
            t = tasks.setdefault(tid, {"task_id": tid, "status": "open", "claimed_by": None, "history": []})
            if p.get("claimant") == t.get("claimed_by") and t.get("claimed_by") is not None:
                t["claimed_by"] = None
                if t.get("status") == "claimed":
                    t["status"] = "open"
        elif k == "state_set":
            key = p["key"]
            cur = state.get(key)
            if cur is None or hlc > cur["_hlc"]:
                state[key] = {"value": p["value"], "by": ev["node_id"], "_hlc": hlc,
                              "updated_at": ev["created_at"]}
        elif k == "message_send":
            messages[p["message_id"]] = {
                "message_id": p["message_id"], "from": p["from"], "to": p["to"],
                "body": p["body"], "task_id": p.get("task_id"),
                "thread_id": p.get("thread_id") or p.get("task_id") or p["message_id"],
                "reply_to": p.get("reply_to"),
                "needs_reply": bool(p.get("needs_reply", False)),
                "visibility": p.get("visibility", "summary"),
                "created_at": ev["created_at"], "hlc": hlc, "acked_by": [],
            }
        elif k == "message_ack":
            m = messages.get(p["message_id"])
            if m and p["by"] not in m["acked_by"]:
                m["acked_by"].append(p["by"])
        elif k == "heartbeat":
            n = p["node"]
            cur = presence.get(n)
            if cur is None or hlc > cur["_hlc"]:
                presence[n] = {"node": n, "status": p.get("status", "online"),
                               "detail": p.get("detail"), "_hlc": hlc, "last_seen": ev["created_at"]}
        elif k == "notify_delivered":
            delivered.add(p["ref_event_id"])

    for key in list(state.keys()):
        state[key].pop("_hlc", None)
    for n in list(presence.keys()):
        presence[n].pop("_hlc", None)

    return {"tasks": tasks, "state": state, "messages": messages,
            "presence": presence, "delivered": delivered}
