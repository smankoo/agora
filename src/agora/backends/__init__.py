"""agora.backends — pluggable LLM backends for the autonomous responder.

Each backend is a callable ``complete(system, user, *, max_tokens) -> str | None``.
The responder picks one by name from config (``responder.provider``). Adding a
new backend = add a function here and a branch in ``get_backend``.
"""
from __future__ import annotations

import json
import sys
import urllib.request


def ollama_backend(url: str, model: str):
    """Local Ollama (OpenAI-ish /api/chat). No creds; runs fully offline."""
    def complete(system: str, user: str, *, max_tokens: int = 800) -> str | None:
        try:
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "stream": False,
                "format": "json",
                "options": {"num_predict": max_tokens},
            }).encode()
            req = urllib.request.Request(f"{url.rstrip('/')}/api/chat", data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.loads(r.read().decode())
            return (out.get("message") or {}).get("content")
        except Exception as e:
            print(f"[agora-responder] ollama failed: {e}", file=sys.stderr)
            return None
    return complete


def get_backends(cfg) -> list:
    """Return an ordered list of backends to try, based on responder config."""
    r = cfg.responder
    chain = []
    if r.provider == "ollama":
        chain.append(ollama_backend(r.ollama_url, r.ollama_model))
    return chain
