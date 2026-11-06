# Installing Agora on a node

Two processes per node: **`agora serve`** (always up) and **`agora tick`** on a
~60s timer. Below is the recommended layout using a dedicated virtualenv so
Agora's pinned `mcp<2.0` never collides with another app on the box.

## 1. Install into a dedicated venv

```bash
python3 -m venv ~/.local/agora-venv
~/.local/agora-venv/bin/pip install "agora-mesh"
```

> **Why a dedicated venv?** The server builds on the FastMCP that ships vendored
> inside `mcp` 1.x. If Agora shared a venv with an app that later bumps `mcp` to
> 2.x (which removed vendored FastMCP), the server would break. Its own venv
> makes Agora immune to any host app's dependency churn.

## 2. Configure

```bash
mkdir -p ~/.config/agora ~/.local/share/agora
cp config.example.toml ~/.config/agora/config.toml
$EDITOR ~/.config/agora/config.toml     # set node_id, peers, server.host/port, responder, notify
~/.local/agora-venv/bin/agora config    # sanity-check the resolved config
```

Secrets (notify token, AWS creds) go in the environment, not the TOML. For
systemd, use an `EnvironmentFile`; for launchd, put them in the plist's
`EnvironmentVariables` or a wrapper.

## 3a. Linux (systemd --user)

```bash
mkdir -p ~/.config/systemd/user
cp systemd/agora-server.service systemd/agora-tick.service systemd/agora-tick.timer ~/.config/systemd/user/
# edit the ExecStart paths if your venv/config differ from the defaults
loginctl enable-linger "$USER"          # so it runs without an active login session
systemctl --user daemon-reload
systemctl --user enable --now agora-server.service
systemctl --user enable --now agora-tick.timer
systemctl --user status agora-server.service
```

If peers connect over a private overlay, open the port on that interface only,
e.g. with ufw: `sudo ufw allow in on tailscale0 to any port 3130 proto tcp`.

## 3b. macOS (launchd)

```bash
cp launchd/sh.agora.server.plist launchd/sh.agora.tick.plist ~/Library/LaunchAgents/
# replace USERNAME and paths in both plists
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/sh.agora.server.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/sh.agora.tick.plist
```

**Reloading after a plist edit:** `launchctl bootout gui/$(id -u)/sh.agora.tick`
then `bootstrap` again. `launchctl kickstart -k` only reruns the *cached*
definition and will **not** pick up plist changes.

## 4. Verify

```bash
~/.local/agora-venv/bin/agora health          # {node, events, hlc}
curl -s http://<this-node>:3130/agora/health
~/.local/agora-venv/bin/agora board           # once peers are configured on both sides
```

Create a task on one node, wait one tick (~60s) or run `agora sync` by hand, and
confirm it appears on a peer's `agora board`.
