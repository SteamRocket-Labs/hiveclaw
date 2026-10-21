"""Run the existing storage entry against an owned loopback application."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys


async def retire_references_through_ui(d, client, factory, actor_id):
    from app.config import get_settings

    root = Path(__file__).resolve().parents[3]
    work = d.root.parent / "reference-ui-runtime"
    work.mkdir()
    (work / "reference_runtime.py").write_text("from fastapi import FastAPI\nfrom app.api.enterprise import router\n"
        "from app.core.tenant_middleware import TenantMiddleware\napp=FastAPI()\n"
        "app.add_middleware(TenantMiddleware)\napp.include_router(router,prefix='/api')\n")
    values = {}
    for key in ("SECRET_KEY", "JWT_SECRET_KEY", "BLOB_STORAGE_PROVIDER", "BLOB_S3_ENDPOINT", "BLOB_S3_REGION",
        "BLOB_S3_BUCKET", "BLOB_S3_ACCESS_KEY_ID", "BLOB_S3_SECRET_ACCESS_KEY", "BLOB_S3_SESSION_TOKEN"):
        value = getattr(get_settings(), key)
        values[key] = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    environment = {**os.environ, **values, "DATABASE_URL": factory.kw["bind"].url.render_as_string(hide_password=False),
        "AGENT_DATA_DIR": str(d.root.parent), "PYTHONPATH": str(work) + os.pathsep + os.environ.get("PYTHONPATH", str(root / "backend"))}
    process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "uvicorn", "reference_runtime:app",
        "--host", "127.0.0.1", "--port", str(port), "--no-access-log", cwd=root / "backend", env=environment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    lines = []
    try:
        async def started():
            while line := await process.stdout.readline():
                lines.append(line)
                if b"Uvicorn running on" in line:
                    return
            raise AssertionError("Owned reference API exited before startup")

        await asyncio.wait_for(started(), 20)
        profile, output = work / "profile.json", work / "result.json"
        profile.write_text(json.dumps({"origin": f"http://127.0.0.1:{port}", "tenant": str(d.tenant),
            "actor": str(actor_id), "token": client.headers["Authorization"].removeprefix("Bearer "), "output": str(output)}))
        profile.chmod(0o600)
        with (work / "frontend.log").open("wb") as log:
            node = await asyncio.create_subprocess_exec(str(root / "frontend/node_modules/.bin/vitest"), "run",
                "src/pages/workspace/OriginalReferenceRetirement.native.test.tsx", cwd=root / "frontend",
                env={**os.environ, "HIVE_REFERENCE_RETIREMENT_BRIDGE": str(profile), "NODE_OPTIONS": "--no-experimental-webstorage"},
                stdout=log, stderr=asyncio.subprocess.STDOUT)
            try:
                code = await asyncio.wait_for(node.wait(), 90)
            finally:
                if node.returncode is None:
                    node.terminate()
                    await asyncio.wait_for(node.wait(), 10)
        assert code == 0, (work / "frontend.log").read_text()[-4500:]
        return json.loads(output.read_text())
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 10)
        lines.append(await process.stdout.read())
        (work / "server.log").write_bytes(b"".join(lines))
