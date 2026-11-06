"""
agora.reconcile — full-mesh, peer-to-peer event UNION (no hub).

Each cycle, for every configured peer:
  1. GET  <peer>/agora/events    (pull the peer's feed)
  2. ingest events we don't have  (idempotent by event_id)
  3. POST <peer>/agora/events     (push events the peer is missing)
Then emit a local heartbeat so presence stays fresh.

Because the log is an immutable event-set keyed by UUID and folded
deterministically, union in any order on any node converges to identical state.
An offline peer is skipped this cycle (silent) and catches up when it returns —
no node depends on any other to keep working.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from . import core
from .config import Config, load_config


def _get(url: str, timeout: float) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def _post(url: str, body: dict, timeout: float) -> dict | None:
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def reconcile_once(cfg: Config, verbose: bool = False) -> dict:
    core.init_db(cfg.db_path)
    # Cheap no-op once compact; on a pre-0.1.1 log this drops the backlog of
    # superseded heartbeats (one per node per tick since install).
    core.compact_heartbeats(cfg.db_path)
    report = {"node": cfg.node_id, "peers": {}}

    for name, base in cfg.peers.items():
        base = base.rstrip("/")
        feed = _get(f"{base}/agora/events", cfg.sync_timeout)
        if feed is None:
            report["peers"][name] = {"reachable": False}
            continue
        pulled = core.ingest_events(cfg.db_path, cfg.node_id, feed.get("events", []))
        peer_ids = {e["event_id"] for e in feed.get("events", [])}
        local = core.get_events(cfg.db_path, limit=None)
        missing = [e for e in local if e["event_id"] not in peer_ids]
        pushed = 0
        if missing:
            res = _post(f"{base}/agora/events", {"events": missing}, cfg.sync_timeout)
            pushed = (res or {}).get("ingested", 0)
        report["peers"][name] = {"reachable": True, "pulled": pulled, "pushed": pushed}

    core.append_event(cfg.db_path, cfg.node_id, "heartbeat",
                      {"node": cfg.node_id, "status": "online", "detail": "reconciled"})
    if verbose:
        print(json.dumps(report))
    return report


def main() -> None:
    cfg = load_config()
    loop = None
    import os
    if os.environ.get("AGORA_LOOP"):
        loop = float(os.environ["AGORA_LOOP"])
    if loop:
        while True:
            try:
                reconcile_once(cfg, verbose=True)
            except Exception as e:  # never die; retry next tick
                print(f"[agora-reconcile] error: {e}", file=sys.stderr)
            time.sleep(loop)
    else:
        reconcile_once(cfg, verbose=True)


if __name__ == "__main__":
    main()
