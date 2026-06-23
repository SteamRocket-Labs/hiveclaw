# Hive Bridge Skill Package

This repository is the standard Skills CLI entrypoint for installing the Hive Bridge skill into local agent tools.

## Install

Install the skill:

```bash
npx skills add https://github.com/rocky2431/hive-bridge-skill --skill hive-bridge
```

If your agent supports global skill installation and you want the Skills CLI to target detected agents:

```bash
npx skills add https://github.com/rocky2431/hive-bridge-skill --skill hive-bridge -g
```

## What This Installs

The Skills CLI installs only the `hive-bridge` skill instructions. The skill then guides the local agent to install and operate the CLI:

```bash
npm install -g @hiveclaw243/hive-bridge
hive-bridge login
hive-bridge status
hive-bridge run --transport websocket
```

The npm package is the executable bridge. This GitHub repository is the agent skill package.

`hive-bridge login` creates a long-lived binding. `hive-bridge run --transport websocket` is the online runner and reconnects after transient WebSocket failures.
