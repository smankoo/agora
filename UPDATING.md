# Iterating on Agora & rolling out updates

All development happens on the VPS clone at `/home/deploy/agora` (git remote:
`github.com/smankoo/agora`). Every node runs an **editable install**
(`pip install -e`) of its own clone, so updating a node is just `git pull` +
restart — no reinstall needed unless dependencies change.

## The loop

1. **Edit** on the VPS clone (`/home/deploy/agora`), run `pytest -q`, and test
   locally (`agora ...` against a scratch config).
2. **Commit & push** (authored as the repo owner, no AI attribution):
   ```bash
   cd /home/deploy/agora
   git commit -am "…"
   git push https://x-access-token:$(cat ~/.gh_token)@github.com/smankoo/agora.git main
   ```
   (Plain `git push` can't auth — the token-URL form is required. Verify with
   `git ls-remote`, not `git rev-parse origin/main`, since the raw-URL push
   leaves the local tracking ref stale.)
3. **Bump the version** in `pyproject.toml` + `src/agora/__init__.py` and add a
   `CHANGELOG.md` entry for anything user-visible; tag `vX.Y.Z` and
   `gh release create` when it's a real release.
4. **Roll out** to the nodes (see below).

## Rolling out to a node

Because installs are editable, a node update is:

```bash
# VPS (local)
cd /home/deploy/agora && git pull
systemctl --user restart agora-server.service      # tick picks up code on its next run

# a Mac node (over SSH from the VPS)
ssh sumeet@<node> 'cd ~/agora && git pull'
ssh sumeet@<node> 'launchctl kickstart -k gui/$(id -u)/ai.agora.server'
```

- **Code-only change** (no new dependency, no arg/behavior change to a running
  process): `git pull` + restart the **server**; the **tick** re-imports fresh
  each 60s run, so it needs nothing.
- **Dependency change** (edited `pyproject.toml` deps): re-run the editable
  install so the new dep lands:
  `uv pip install --python <agora-venv>/bin/python -e ~/agora`.
- **Config-schema change** (new `config.toml` key): update each node's
  `~/.config/agora/config.toml`. Defaults keep old configs working.
- **launchd plist change** (rare): edit the plist then reload with
  `launchctl bootout gui/$(id -u)/<label>` + `bootstrap` — `kickstart -k` only
  reruns the cached definition and won't pick up plist edits.

## Safe-rollout notes

- The **event log is forward-compatible**: new event kinds/fields are additive,
  and an older node ignores fields it doesn't know, so a mixed-version mesh
  keeps syncing during a staggered rollout. Avoid *changing the meaning* of an
  existing field without a migration.
- Roll out the **VPS first** (always-on, easiest to watch), then the Macs.
- After each node: `agora health` and confirm event counts stay within a cycle
  of each other (`agora board` shows members + active tasks).
- Never point two servers at the same `db_path`; each node owns its own log.
