---
name: hive-connect
description: Install Hive Connect, connect this local agent to the user's Hive Local Agent Channel, and keep the Hive Connect background service online for chat, file transfer, and delegated work.
---

# Hive Connect

Use this skill when the user asks you to install Hive Connect, connect a local agent to Hive, or keep a local agent online for Hive cloud chat and delegated work.

## Goal

Install the `hive-connect` CLI, complete browser-based login, verify the user-scoped Local Agent Channel connection, and install the outbound background service.

Login creates a long-lived binding. Do not ask the user to log in again just because the computer slept, restarted, or the background service disconnected; restart the background service instead.

## Install The CLI

Use the Skill and CLI install commands returned by the Hive Local Agent install
guide. The deployment must configure approved sources through
`HIVE_CONNECT_SKILL_REPO_URL` and `HIVE_CONNECT_NPM_PACKAGE`. If either command is
missing, stop and ask the Hive operator for it; do not guess a repository, npm
scope, or package name.

After installation, require the `hive-connect` binary to be available on `PATH`.

Optionally check the current login state:

```bash
hive-connect status
```

If it says Hive Connect is not logged in, continue to login.

## Login

Run:

```bash
hive-connect login
```

The browser opens Hive. Let the user log in there. Hive should automatically approve the local agent authentication from the `user_code` in the URL; do not ask the user to copy a code into Hive manually.

For self-hosted or test Hive environments only, ask the user for the Hive URL and run:

```bash
hive-connect login --hive-url <your Hive URL>
```

Then verify:

```bash
hive-connect status
```

## Keep The Local Agent Online

For cloud-to-local chat, file transfer, and delegated work, install and start the background service:

```bash
hive-connect daemon install --config ~/.hive-connect/config.toml --force
hive-connect daemon status
```

This service uses outbound HTTPS/WebSocket connections only. Do not expose a local port, reverse proxy, tunnel, or public callback server.

The background service keeps one WebSocket session open for consecutive cloud messages and reconnects after transient WebSocket failures. It streams progress back to Hive before the final result. Treat online/offline as runtime presence only; it is separate from the long-lived login binding.

## Upload A Local File To Hive

If the installed CLI exposes an upload command, run:

```bash
hive-connect upload <path>
```

If upload is not available, use the Hive Local Agent page's Workspace upload control and report the uploaded workspace path back to the user.
