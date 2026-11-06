#!/bin/bash
# rollout.sh — pull the latest Agora on every node and restart it, from the VPS.
# Usage: bash scripts/rollout.sh [--reinstall]
#   --reinstall  also re-run the editable pip install (use when deps changed)
#
# Rolls out VPS first, then the Mac nodes over SSH. Prints each node's health.
set -uo pipefail

REINSTALL=0
[ "${1:-}" = "--reinstall" ] && REINSTALL=1

AGORA_VENV="$HOME/.hermes/agora/venv"
MAC_NODES=("sumeet@100.116.71.40" "sumeet@100.119.136.41")   # angus, magnus
MAC_IPS=("100.116.71.40" "100.119.136.41")

echo "== VPS (hermes) =="
( cd "$HOME/agora" && git pull --ff-only )
if [ "$REINSTALL" = 1 ]; then
  uv pip install --python "$AGORA_VENV/bin/python" -e "$HOME/agora" >/dev/null
fi
systemctl --user restart agora-server.service
sleep 3
curl -s --max-time 5 http://127.0.0.1:3130/agora/health; echo

for i in "${!MAC_NODES[@]}"; do
  node="${MAC_NODES[$i]}"; ip="${MAC_IPS[$i]}"
  echo "== $node =="
  if ! ssh -o BatchMode=yes -o ConnectTimeout=6 "$node" 'echo ok' >/dev/null 2>&1; then
    echo "  unreachable (asleep?) — will catch up on next git pull"; continue
  fi
  ssh -o BatchMode=yes "$node" 'cd ~/agora && git pull --ff-only' 2>&1 | tail -1
  if [ "$REINSTALL" = 1 ]; then
    ssh -o BatchMode=yes "$node" 'uv pip install --python ~/.hermes/agora/venv/bin/python -e ~/agora >/dev/null 2>&1 || ~/.hermes/agora/venv/bin/python -m pip install -e ~/agora >/dev/null 2>&1'
  fi
  # kickstart -k picks up new CODE (editable install); plist changes need bootout/bootstrap.
  ssh -o BatchMode=yes "$node" 'launchctl kickstart -k gui/$(id -u)/ai.agora.server' 2>&1 | tail -1
  sleep 3
  ssh -o BatchMode=yes "$node" "curl -s --max-time 5 http://$ip:3130/agora/health"; echo
done
echo "== rollout done =="
