---
name: hive-bridge
description: Install Hive Bridge, connect this local agent to Hive, and use Hive MCP tools for messages, file upload, and work requests.
---

# Hive Bridge Skill

Use this skill when the user asks you to connect this local agent to Hive.

## Goal

Install `hive-bridge`, configure it as a stdio MCP server, run device-flow login, and verify the connection.

## Install

Try these in order. Stop after the first successful install.

```bash
python3 -m pip install -e ./local_bridge
hive-bridge status
```

```bash
python3 -m pip install --user "git+https://github.com/<org>/<repo>.git#subdirectory=local_bridge"
hive-bridge status
```

```bash
pipx install "git+https://github.com/<org>/<repo>.git#subdirectory=local_bridge"
hive-bridge status
```

## Configure MCP

Add a stdio MCP server with this command:

```bash
hive-bridge mcp
```

The MCP server exposes:

- `hive_status`
- `hive_poll_inbox`
- `hive_send_message`
- `hive_report_result`
- `hive_upload_file`

## Login

Run:

```bash
hive-bridge login
```

Open the activation URL, log in to Hive, choose the target agent, and approve the Local Agent Link card.

Then verify:

```bash
hive-bridge status
```

## Unattended Work Requests

For cloud-to-local work requests, run:

```bash
hive-bridge run
```

To dispatch work requests to a local command adapter:

```bash
hive-bridge run --command-adapter <command> [args...]
```

The runner uses outbound HTTPS polling only. Do not expose a local port, reverse proxy, ngrok, Tailscale, or Cloudflare Tunnel.
