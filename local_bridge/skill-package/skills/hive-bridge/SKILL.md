---
name: hive-bridge
description: Install Hive Bridge, connect this local agent to the user's Hive Local Agent Channel, and keep a cloud-to-local WebSocket runner online for chat, file transfer, and delegated work.
---

# Hive Bridge

Use this skill when the user asks you to install Hive Bridge, connect a local agent to Hive, or keep a local agent online for Hive cloud chat and delegated work.

## Goal

Install the `hive-bridge` CLI, complete browser-based login, verify the user-scoped Local Agent Channel connection, and start the outbound WebSocket runner.

Login creates a long-lived binding. Do not ask the user to log in again just because the computer slept, restarted, or the runner disconnected; restart the runner instead.

## Install The CLI

Run:

```bash
npm install -g @hiveclaw243/hive-bridge
hive-bridge status
```

If `hive-bridge status` says it is not logged in, continue to login.

## Login

Run:

```bash
hive-bridge login
```

Open the activation URL and let the user log in to Hive. The Hive page should automatically approve the local agent authentication from the `user_code` in the URL; do not ask the user to copy a code into Hive manually.

Then verify:

```bash
hive-bridge status
```

## Keep The Local Agent Online

For cloud-to-local chat, file transfer, and delegated work, run:

```bash
hive-bridge run --transport websocket
```

This runner uses outbound HTTPS/WebSocket connections only. Do not expose a local port, reverse proxy, tunnel, or public callback server.

The foreground runner keeps one WebSocket session open for consecutive cloud messages and reconnects after transient WebSocket failures. Command adapters stream stdout/stderr back to Hive as `delta` events before the final result. Treat online/offline as runtime presence only; it is separate from the long-lived login binding.

## Upload A Local File To Hive

Run:

```bash
hive-bridge upload <path>
```

Report the uploaded workspace path back to the user.
