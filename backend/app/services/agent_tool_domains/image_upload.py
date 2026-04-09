"""Image upload domain — ImageKit CDN upload."""

import logging
import uuid
from pathlib import Path

from sqlalchemy import select

from app.database import async_session

logger = logging.getLogger(__name__)


async def _upload_image(agent_id: uuid.UUID, ws: Path, arguments: dict) -> str:
    """Upload an image to ImageKit CDN and return the public URL.

    Credential resolution order:
    1. Global tool config (admin-set, shared by all agents)
    2. Per-agent tool config override (agent-specific)
    """
    import httpx
    import base64

    file_path = arguments.get("file_path")
    url = arguments.get("url")
    file_name = arguments.get("file_name")
    folder = arguments.get("folder", "/hive")

    if not file_path and not url:
        return "❌ Please provide either 'file_path' (workspace path) or 'url' (public image URL)"

    # ── Load ImageKit credentials (global → per-agent fallback) ──
    private_key = ""
    url_endpoint = ""
    try:
        from app.models.tool import Tool, AgentTool
        async with async_session() as db:
            # Global config
            r = await db.execute(select(Tool).where(Tool.name == "upload_image"))
            tool = r.scalar_one_or_none()
            if tool and tool.config:
                private_key = tool.config.get("private_key", "")
                url_endpoint = tool.config.get("url_endpoint", "")

            # Per-agent override (if global key is empty)
            if not private_key and tool:
                r2 = await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.tool_id == tool.id,
                    )
                )
                agent_tool = r2.scalar_one_or_none()
                if agent_tool and agent_tool.config:
                    private_key = agent_tool.config.get("private_key", "") or private_key
                    url_endpoint = agent_tool.config.get("url_endpoint", "") or url_endpoint
    except Exception as e:
        logger.error(f"[UploadImage] Config load error: {e}")

    if not private_key:
        return "❌ ImageKit Private Key not configured. Ask your admin to configure it in Enterprise Settings → Tools → Upload Image, or set it in your agent's tool config."

    # ── Prepare the file ──
    form_data = {}
    file_content = None

    if file_path:
        # Read from workspace
        full_path = (ws / file_path).resolve()
        if not str(full_path).startswith(str(ws)):
            return "❌ Access denied: path is outside the workspace"
        if not full_path.exists():
            return f"❌ File not found: {file_path}"
        if not full_path.is_file():
            return f"❌ Not a file: {file_path}"

        # Check file size (max 25MB for free plan)
        size_mb = full_path.stat().st_size / (1024 * 1024)
        if size_mb > 25:
            return f"❌ File too large ({size_mb:.1f}MB). Maximum is 25MB."

        file_content = full_path.read_bytes()
        if not file_name:
            file_name = full_path.name
    elif url:
        # Pass URL directly to ImageKit
        form_data["file"] = url
        if not file_name:
            from urllib.parse import urlparse
            file_name = urlparse(url).path.split("/")[-1] or "image.jpg"

    if not file_name:
        file_name = "image.png"

    form_data["fileName"] = file_name
    form_data["folder"] = folder
    form_data["useUniqueFileName"] = "true"

    # ── Upload to ImageKit V2 ──
    auth_string = base64.b64encode(f"{private_key}:".encode()).decode()

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            if file_content:
                # Binary upload via multipart
                files = {"file": (file_name, file_content)}
                resp = await client.post(
                    "https://upload.imagekit.io/api/v2/files/upload",
                    headers={"Authorization": f"Basic {auth_string}"},
                    data=form_data,
                    files=files,
                )
            else:
                # URL upload via form data
                resp = await client.post(
                    "https://upload.imagekit.io/api/v2/files/upload",
                    headers={"Authorization": f"Basic {auth_string}"},
                    data=form_data,
                )

        if resp.status_code in (200, 201):
            result = resp.json()
            cdn_url = result.get("url", "")
            file_id = result.get("fileId", "")
            size = result.get("size", 0)
            size_str = f"{size / 1024:.1f}KB" if size < 1024 * 1024 else f"{size / (1024 * 1024):.1f}MB"
            return (
                f"✅ Image uploaded successfully!\n\n"
                f"**CDN URL**: {cdn_url}\n"
                f"**File ID**: {file_id}\n"
                f"**Size**: {size_str}\n"
                f"**Name**: {result.get('name', file_name)}"
            )
        else:
            error_detail = resp.text[:300]
            return f"❌ Upload failed (HTTP {resp.status_code}): {error_detail}"

    except httpx.TimeoutException:
        return "❌ Upload timed out after 60s. The file may be too large or the network is slow."
    except Exception as e:
        return f"❌ Upload error: {type(e).__name__}: {str(e)[:300]}"
