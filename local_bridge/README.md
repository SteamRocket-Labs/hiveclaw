# Hive Bridge

Hive Bridge connects a local agent runtime to the user's Hive Local Agent Channel.

## Install The Agent Skill

Use the standard Skills CLI entrypoint:

```bash
npx skills add https://github.com/rocky2431/hive-bridge-skill --skill hive-bridge
```

The skill is the local agent's operation guide. It tells Codex, Claude Code, Cursor, or another local agent how to install and run the bridge.

## Install The CLI Directly

```bash
npm install -g @hiveclaw243/hive-bridge
```

## Commands

```bash
hive-bridge login
hive-bridge status
hive-bridge upload ./report.md
hive-bridge run --transport websocket
```

## Roles

- Skill: installation and operation instructions for the local agent.
- CLI: login, token storage, upload, and channel runner.
- WebSocket runner: long-lived outbound channel for cloud-to-local chat, file transfer, and work requests.

`hive-bridge login` creates a long-lived binding. `hive-bridge run --transport websocket` is the online runner: it keeps one WebSocket session open for consecutive cloud messages, streams command stdout/stderr as `delta` events, and reconnects after transient WebSocket failures.

Completed local executions are recorded before their result is sent. The npm runner uses a locked, fsync-backed append-only ledger at `.hive/local-agent-channel/execution-receipts.jsonl`; replaying the same server-issued `replay_key` returns the original result without running the local command again. Corrupt rows and stale locks are quarantined/recovered, while receipt persistence failure is reported as `requires_reconciliation` instead of silently risking duplicate execution.

The GitHub skill package and the npm CLI package are intentionally separate:

- `https://github.com/rocky2431/hive-bridge-skill`: installed by `npx skills add`.
- `@hiveclaw243/hive-bridge`: installed by the skill with `npm install -g`.
