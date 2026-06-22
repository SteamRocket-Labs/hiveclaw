from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from hive_bridge.client import HiveBridgeClient
from hive_bridge.mcp_server import HiveBridgeMCPServer
from hive_bridge.poller import HiveBridgeRunner
from hive_bridge.runtime import create_default_runtime_registry
from hive_bridge.token_store import BridgeConfig, FileTokenStore


DEFAULT_BASE_URL = "https://try.hive.ai"


def _device_fingerprint() -> str:
    return f"{platform.system()}:{platform.machine()}:{socket.gethostname()}"


def _load_client(args: argparse.Namespace) -> HiveBridgeClient:
    store = FileTokenStore(config_path=Path(args.config).expanduser() if getattr(args, "config", None) else None)
    config = store.load()
    if config is None:
        raise SystemExit("Hive Bridge is not logged in. Run `hive-bridge login` first.")
    return HiveBridgeClient(base_url=args.base_url or config.base_url, token=config.token)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_login(args: argparse.Namespace) -> int:
    store = FileTokenStore(config_path=Path(args.config).expanduser() if args.config else None)
    base_url = args.base_url.rstrip("/")
    client = HiveBridgeClient(base_url=base_url)
    init = client.init_pairing(
        device_name=args.device_name or socket.gethostname(),
        client_kind=args.client_kind,
        device_fingerprint=args.device_fingerprint or _device_fingerprint(),
        scopes=[],
    )
    print(f"Pairing code: {init['user_code']}")
    print("Open this Hive activation URL and approve the Local Agent Link:")
    print(init["verification_uri_complete"])
    if not args.no_browser:
        webbrowser.open(init["verification_uri_complete"])

    deadline = time.monotonic() + int(init.get("expires_in", 900))
    interval = int(init.get("interval", 3))
    device_code = init["device_code"]
    while time.monotonic() < deadline:
        exchanged = client.exchange_pairing(device_code)
        if exchanged.get("status") == "pending":
            time.sleep(interval)
            continue
        token = exchanged["access_token"]
        store.save(
            BridgeConfig(
                base_url=base_url,
                token=token,
                connection_id=exchanged.get("connection_id"),
                agent_id=exchanged.get("agent_id"),
                tenant_id=exchanged.get("tenant_id"),
            )
        )
        _print_json({"status": "connected", "agent_id": exchanged.get("agent_id")})
        return 0
    raise SystemExit("Pairing timed out before approval.")


def cmd_status(args: argparse.Namespace) -> int:
    client = _load_client(args)
    _print_json(client.status())
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    store = FileTokenStore(config_path=Path(args.config).expanduser() if args.config else None)
    store.clear()
    print("Hive Bridge local token removed.")
    return 0


def cmd_mcp(_args: argparse.Namespace) -> int:
    HiveBridgeMCPServer().serve()
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    client = _load_client(args)
    _print_json(client.upload_file(args.path))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    client = _load_client(args)
    registry = create_default_runtime_registry()
    command = args.command or args.command_adapter
    runtime = args.runtime or ("command" if command else "noop")
    adapter = registry.create(runtime, command=command, timeout_seconds=args.timeout, work_dir=args.work_dir)
    runner = HiveBridgeRunner(client=client, adapter=adapter)
    if args.once:
        processed = runner.run_once()
        _print_json({"status": "ok", "processed": processed})
        return 0
    print("Hive Bridge runner started. Press Ctrl-C to stop.", file=sys.stderr)
    while True:
        runner.run_once()
        time.sleep(args.interval)


def cmd_service(args: argparse.Namespace) -> int:
    if args.action == "status":
        print("Hive Bridge P0 service mode is available through `hive-bridge run` foreground mode.")
        return 0
    print(
        "P0 service installer is not enabled in this package yet. "
        "Use `hive-bridge run` for foreground polling.",
        file=sys.stderr,
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hive-bridge")
    parser.add_argument("--config", default=None, help="Path to Hive Bridge config JSON.")
    parser.add_argument("--base-url", default=None, help="Hive base URL. Defaults to saved config or try.hive.ai.")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login")
    login.add_argument("--base-url", default=DEFAULT_BASE_URL)
    login.add_argument("--device-name", default=None)
    login.add_argument("--client-kind", default="generic_mcp_stdio")
    login.add_argument("--device-fingerprint", default=None)
    login.add_argument("--no-browser", action="store_true")
    login.set_defaults(func=cmd_login)

    status_cmd = sub.add_parser("status")
    status_cmd.set_defaults(func=cmd_status)

    logout = sub.add_parser("logout")
    logout.set_defaults(func=cmd_logout)

    mcp = sub.add_parser("mcp")
    mcp.set_defaults(func=cmd_mcp)

    upload = sub.add_parser("upload")
    upload.add_argument("path")
    upload.set_defaults(func=cmd_upload)

    run = sub.add_parser("run")
    run.add_argument("--interval", type=float, default=3.0)
    run.add_argument("--runtime", choices=["noop", "command", "acp"], default=None)
    run.add_argument("--command", nargs="+", default=None)
    run.add_argument("--command-adapter", nargs="+", default=None, help="Backward-compatible alias for --runtime command --command.")
    run.add_argument("--work-dir", default=".")
    run.add_argument("--timeout", type=int, default=600)
    run.add_argument("--once", action="store_true")
    run.set_defaults(func=cmd_run)

    service = sub.add_parser("service")
    service.add_argument("action", choices=["install", "start", "status", "stop"])
    service.set_defaults(func=cmd_service)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.base_url is None and args.command != "login":
        args.base_url = None
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
