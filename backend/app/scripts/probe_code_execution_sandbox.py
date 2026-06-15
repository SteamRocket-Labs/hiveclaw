"""Probe the configured code-execution sandbox and emit auditable evidence.

Usage:
    python -m app.scripts.probe_code_execution_sandbox
    python -m app.scripts.probe_code_execution_sandbox --output sandbox-probe.json
    python -m app.scripts.probe_code_execution_sandbox --persist --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.services.code_execution.probe import (
    persisted_sandbox_probe_summary,
    run_code_execution_sandbox_probe,
    store_latest_sandbox_probe_evidence,
)


async def _main_async() -> int:
    parser = argparse.ArgumentParser(description="Probe the configured Hive code-execution sandbox provider.")
    parser.add_argument("--timeout", type=int, default=20, help="Per-check timeout in seconds.")
    parser.add_argument("--output", default=None, help="Optional JSON artifact path.")
    parser.add_argument("--persist", action="store_true", help="Persist latest evidence into system_settings.")
    parser.add_argument("--confirm", action="store_true", help="Required with --persist.")
    args = parser.parse_args()

    if args.persist and not args.confirm:
        raise SystemExit("--persist requires --confirm")

    report = await run_code_execution_sandbox_probe(timeout=max(1, args.timeout))
    if args.persist:
        report["latest_setting"] = persisted_sandbox_probe_summary(await store_latest_sandbox_probe_evidence(report))

    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report.get("passed") else 1


def main() -> None:
    raise SystemExit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
