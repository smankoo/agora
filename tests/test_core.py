"""Core CRDT tests: convergence, claim semantics, state LWW, idempotent merge."""
import os
import tempfile

from agora import core


def _fresh(n):
    d = tempfile.mkdtemp()
    dbs = [os.path.join(d, f"{i}.db") for i in range(n)]
    for db in dbs:
        core.init_db(db)
    return dbs


def _union(dbs):
    allev = []
    for db in dbs:
        allev += core.get_events(db)
    for db in dbs:
        core.ingest_events(db, "x", allev)


def test_concurrent_claim_earliest_wins():
    a, b, c = _fresh(3)
    core.append_event(a, "n1", "task_create", {"task_id": "t1", "title": "job", "assignee": "any"})
    _union([a, b, c])
    core.append_event(b, "n2", "task_claim", {"task_id": "t1", "claimant": "n2"})
    import time as _t
    _t.sleep(0.01)
    core.append_event(c, "n3", "task_claim", {"task_id": "t1", "claimant": "n3"})
    _union([a, b, c])
    winners = {core.project(db)["tasks"]["t1"]["claimed_by"] for db in (a, b, c)}
    assert winners == {"n2"}, f"expected n2 (earliest), got {winners}"


def test_no_steal_then_unclaim_reclaim():
    a, b, c = _fresh(3)
    core.append_event(a, "n1", "task_create", {"task_id": "t1", "title": "job"})
    _union([a, b, c])
    core.append_event(b, "n2", "task_claim", {"task_id": "t1", "claimant": "n2"})
    _union([a, b, c])
    # n3 tries to steal -> must fail
    core.append_event(c, "n3", "task_claim", {"task_id": "t1", "claimant": "n3"})
    _union([a, b, c])
    assert core.project(a)["tasks"]["t1"]["claimed_by"] == "n2"
    # n2 releases, n3 reclaims -> n3 gets it
    core.append_event(b, "n2", "task_unclaim", {"task_id": "t1", "claimant": "n2"})
    _union([a, b, c])
    core.append_event(c, "n3", "task_claim", {"task_id": "t1", "claimant": "n3"})
    _union([a, b, c])
    assert {core.project(db)["tasks"]["t1"]["claimed_by"] for db in (a, b, c)} == {"n3"}


def test_state_lww_converges():
    a, b = _fresh(2)
    core.append_event(a, "n1", "state_set", {"key": "owner", "value": "n1"})
    core.append_event(b, "n2", "state_set", {"key": "owner", "value": "n2"})
    _union([a, b])
    va = core.project(a)["state"]["owner"]["value"]
    vb = core.project(b)["state"]["owner"]["value"]
    assert va == vb


def test_idempotent_resync_no_dupes():
    a, b = _fresh(2)
    core.append_event(a, "n1", "task_create", {"task_id": "t1", "title": "x"})
    _union([a, b])
    before = len(core.get_events(a))
    _union([a, b])
    _union([a, b])
    assert len(core.get_events(a)) == before


def test_pending_replies_and_ack():
    a, b = _fresh(2)
    core.append_event(a, "n1", "message_send", {
        "message_id": "m1", "from": "n1", "to": "n2", "body": "hi",
        "needs_reply": True, "thread_id": "th1"})
    _union([a, b])
    assert len(core.pending_replies(b, "n2")) == 1
    assert len(core.pending_replies(b, "n1")) == 0  # not addressed to n1
    core.append_event(b, "n2", "message_ack", {"message_id": "m1", "by": "n2"})
    assert len(core.pending_replies(b, "n2")) == 0


def test_message_thread_assembly():
    a, = _fresh(1)
    core.append_event(a, "n1", "message_send", {
        "message_id": "m1", "from": "n1", "to": "n2", "body": "q", "thread_id": "T"})
    core.append_event(a, "n2", "message_send", {
        "message_id": "m2", "from": "n2", "to": "n1", "body": "a",
        "thread_id": "T", "reply_to": "m1"})
    msgs = [m for m in core.project(a)["messages"].values() if m["thread_id"] == "T"]
    assert len(msgs) == 2


def _heartbeats(db):
    return [e for e in core.get_events(db, limit=None) if e["kind"] == "heartbeat"]


def test_heartbeats_keep_only_latest_per_node():
    (a,) = _fresh(1)
    for i in range(5):
        core.append_event(a, "n1", "heartbeat", {"node": "n1", "status": "online", "detail": str(i)})
        core.append_event(a, "n1", "heartbeat", {"node": "n2", "status": "online", "detail": str(i)})
    hbs = _heartbeats(a)
    assert sorted(e["payload"]["node"] for e in hbs) == ["n1", "n2"]
    assert {e["payload"]["detail"] for e in hbs} == {"4"}
    assert core.project(a)["presence"]["n1"]["detail"] == "4"


def test_stale_heartbeat_not_resurrected_by_peer_push():
    a, b = _fresh(2)
    old = core.append_event(a, "n1", "heartbeat", {"node": "n1", "status": "online"})
    core.ingest_events(b, "n2", [old])
    core.append_event(a, "n1", "heartbeat", {"node": "n1", "status": "busy"})
    _union([a, b])
    # b still remembers `old` only if it failed to prune; a legacy peer re-pushing it must be refused.
    assert core.ingest_events(a, "n1", [old]) == 0
    for db in (a, b):
        hbs = _heartbeats(db)
        assert len(hbs) == 1 and hbs[0]["payload"]["status"] == "busy"


def test_compact_heartbeats_on_legacy_log():
    import json as _json
    import sqlite3 as _sq
    (a,) = _fresh(1)
    core.append_event(a, "n1", "task_create", {"task_id": "t1", "title": "keep me"})
    conn = _sq.connect(a)  # simulate a pre-compaction log: raw inserts, no pruning
    for i in range(100):
        for node in ("n1", "n2"):
            conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                         (f"{node}-{i}", f"{i:015d}:000000:{node}", node, "heartbeat",
                          _json.dumps({"node": node, "status": "online", "detail": str(i)}), "t"))
    conn.commit()
    conn.close()
    assert core.compact_heartbeats(a) == 198
    assert len(_heartbeats(a)) == 2
    assert core.project(a)["presence"]["n2"]["detail"] == "99"
    assert "t1" in core.project(a)["tasks"]
    assert core.compact_heartbeats(a) == 0


def test_get_events_unlimited():
    (a,) = _fresh(1)
    for i in range(30):
        core.append_event(a, "n1", "state_set", {"key": f"k{i}", "value": i})
    assert len(core.get_events(a, limit=10)) == 10
    assert len(core.get_events(a, limit=None)) == 30
