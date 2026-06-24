---
name: hive-bridge
description: Install Hive Bridge, connect this local agent to the user's Hive Local Agent Channel, and use the CLI/channel runner for messages, file transfer, and work requests.
---

# Hive Bridge Skill

Use this skill when the user asks you to connect this local agent to Hive.

## Goal

Install `hive-bridge`, run device-flow login, verify the user-scoped Local Agent Channel connection, and start the WebSocket runner.

Login creates a long-lived binding. Do not ask the user to log in again just because the computer slept, restarted, or the runner disconnected; restart the runner instead.

## Install

Install the published npm package:

```bash
npm install -g @hiveclaw243/hive-bridge
hive-bridge status
```

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

To upload a local file to Hive, run:

```bash
hive-bridge upload <path>
```

## Channel Runtime

For cloud-to-local chat, file transfer, and work requests, run the WebSocket channel runner:

```bash
hive-bridge run --transport websocket
```

To dispatch work requests to a local command adapter:

```bash
hive-bridge run --transport websocket --runtime command --command <command> [args...]
```

The runner uses outbound HTTPS/WebSocket connections only. Do not expose a local port, reverse proxy, ngrok, Tailscale, or Cloudflare Tunnel.

The foreground runner keeps one WebSocket session open for consecutive cloud messages and reconnects after transient WebSocket failures. Command adapters stream stdout/stderr back to Hive as `delta` events before the final result. Treat online/offline as runtime presence only; it is separate from the long-lived login binding.
