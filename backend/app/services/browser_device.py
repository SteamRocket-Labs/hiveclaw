"""Browser primitives over private pipes; no model-selected program or raw CDP."""

import asyncio
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from app.config import get_settings
from app.services.browser_pipe import BrowserLaunchSpec, BrowserPipe, BrowserPipeError
from app.services.execution_environments import EnvironmentError
from app.services.runtime_task_fence import StaleRuntimeTaskFenceError
from sqlalchemy.exc import SQLAlchemyError


def browser_device_spec(resource_id, tenant_id, agent_id):
    settings = get_settings()
    if settings.BROWSER_DEVICE_PROFILE != "trusted_host":
        raise BrowserPipeError("browser_device_not_configured", "not_started")
    if not settings.BROWSER_DEVICE_EXECUTABLE or not settings.BROWSER_PRIVATE_PROFILE_DIR:
        raise BrowserPipeError("browser_device_not_configured", "not_started")
    root = Path(settings.BROWSER_PRIVATE_PROFILE_DIR).expanduser()
    agents = Path(settings.AGENT_DATA_DIR).expanduser().resolve()
    if not root.is_absolute() or root.resolve() != root or root.is_relative_to(agents) or agents.is_relative_to(root):
        raise BrowserPipeError("browser_private_storage_invalid", "not_started")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise BrowserPipeError("browser_private_storage_unavailable", "not_started") from exc
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise BrowserPipeError("browser_private_storage_permissions", "not_started")
    home = root / str(tenant_id) / str(agent_id) / str(resource_id)
    if home.resolve() != home:
        raise BrowserPipeError("browser_private_storage_invalid", "not_started")
    temporary = home / "tmp"
    try:
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise BrowserPipeError("browser_private_storage_unavailable", "not_started") from exc
    return BrowserLaunchSpec(
        executable=Path(settings.BROWSER_DEVICE_EXECUTABLE).expanduser(),
        arguments=(
            "--remote-debugging-pipe",
            "--headless=new",
            f"--user-data-dir={home / 'profile'}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ),
        environment={
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "PATH": "/usr/bin:/bin",
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_CACHE_HOME": str(home / "cache"),
        },
        cwd=home,
    )


class BrowserPage:
    def __init__(self, pipe: BrowserPipe):
        self.pipe = pipe
        self.target_id = None
        self.session_id = None

    @classmethod
    async def start(cls, spec):
        pipe = await BrowserPipe.start(spec)
        page = cls(pipe)
        try:
            target = await pipe.call("Target.createTarget", {"url": "about:blank"})
            await page.select(target["targetId"])
        except BaseException as exc:
            await pipe.close()
            if isinstance(exc, (BrowserPipeError, asyncio.CancelledError)):
                exc.resource_closed = True
            if isinstance(exc, (KeyError, ValueError, TypeError)):
                error = BrowserPipeError("browser_protocol_invalid", "unknown")
                error.resource_closed = True
                raise error from exc
            raise
        return page

    async def select(self, target_id):
        targets = (await self.pipe.call("Target.getTargets", {}))["targetInfos"]
        if not any(target.get("targetId") == target_id and target.get("type") == "page" for target in targets):
            raise BrowserPipeError("browser_page_unavailable", "not_started")
        attached = await self.pipe.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        self.target_id, self.session_id = target_id, attached["sessionId"]
        await self.pipe.call("Page.enable", {}, session_id=self.session_id)

    async def observe(self):
        targets = (await self.pipe.call("Target.getTargets", {}))["targetInfos"]
        snapshot = await self.pipe.call(
            "DOMSnapshot.captureSnapshot", {"computedStyles": []}, session_id=self.session_id
        )
        protected = set()
        try:
            strings = snapshot["strings"]
            for document in snapshot["documents"]:
                dom = document["nodes"]
                identities = dom.get("backendNodeId", [])
                names = dom.get("nodeName", [])
                attributes = dom.get("attributes", [[] for _ in identities])
                for node_id, name, pairs in zip(identities, names, attributes, strict=True):
                    if strings[name].lower() == "input":
                        attrs = {
                            strings[key].lower(): strings[value]
                            for key, value in zip(pairs[::2], pairs[1::2], strict=True)
                        }
                        if attrs.get("type", "").lower() == "password":
                            protected.add(node_id)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise BrowserPipeError("browser_observation_schema_invalid", "unknown") from exc
        tree = await self.pipe.call("Accessibility.getFullAXTree", {}, session_id=self.session_id)
        by_id = {node["nodeId"]: node for node in tree.get("nodes", [])}
        protected_roots = {node["nodeId"] for node in by_id.values() if node.get("backendDOMNodeId") in protected}
        protected_descendants = set()
        pending = list(protected_roots)
        while pending:
            for child in by_id.get(pending.pop(), {}).get("childIds", []):
                if child not in protected_descendants and child not in protected_roots:
                    protected_descendants.add(child)
                    pending.append(child)
        # Only exact password-input values cross this redaction boundary. The
        # raw DOM snapshot is not returned, logged or placed in a tool receipt.
        nodes = []
        for original in tree.get("nodes", []):
            node = dict(original)
            if node["nodeId"] in protected_roots | protected_descendants:
                node.pop("value", None)
                node["properties"] = [p for p in node.get("properties", []) if p.get("name") != "valuetext"]
            if node["nodeId"] in protected_descendants:
                node.pop("name", None)
                node.pop("description", None)
            nodes.append(node)
        metrics = await self.pipe.call("Page.getLayoutMetrics", {}, session_id=self.session_id)
        visual = metrics.get("cssVisualViewport") or {}
        viewport = {
            "width": visual.get("clientWidth"),
            "height": visual.get("clientHeight"),
            "offsetX": visual.get("offsetX"),
            "offsetY": visual.get("offsetY"),
        }
        if (
            not all(type(value) in {float, int} and math.isfinite(value) for value in viewport.values())
            or viewport["width"] <= 0
            or viewport["height"] <= 0
        ):
            viewport = None
        screenshot = await self.pipe.call(
            "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}, session_id=self.session_id
        )
        return {
            "target_id": self.target_id,
            "pages": [
                {key: target[key] for key in ("targetId", "url", "title") if key in target}
                for target in targets
                if target.get("type") == "page"
            ],
            "accessibility": nodes,
            "screenshot": screenshot["data"],
            "media_type": "image/png",
            "viewport": viewport,
        }

    async def act(self, action, arguments, *, before_effect):
        acknowledged_effect = False

        async def effect(method, parameters):
            nonlocal acknowledged_effect
            try:
                try:
                    await before_effect()
                except EnvironmentError as exc:
                    raise BrowserPipeError(exc.code, "not_started") from exc
                except (StaleRuntimeTaskFenceError, SQLAlchemyError) as exc:
                    raise BrowserPipeError("browser_authority_unavailable", "not_started") from exc
                result = await self.pipe.call(method, parameters, session_id=self.session_id)
            except BrowserPipeError as exc:
                if acknowledged_effect:
                    exc.effect = "unknown"
                raise
            acknowledged_effect = True
            return result

        if action == "select_page":
            await self.select(arguments["target_id"])
            return
        if action == "navigate":
            try:
                url = urlsplit(arguments["url"])
            except ValueError as exc:
                raise BrowserPipeError("browser_navigation_url_invalid", "not_started") from exc
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise BrowserPipeError("browser_navigation_url_invalid", "not_started")
            result = await effect("Page.navigate", {"url": arguments["url"]})
            if result.get("errorText"):
                raise BrowserPipeError("browser_navigation_failed", "unknown")
        elif action in {"click", "scroll"}:
            coordinates = {key: arguments.get(key, 0) for key in ("x", "y")}
            if not all(type(value) in {float, int} and math.isfinite(value) for value in coordinates.values()):
                raise BrowserPipeError("browser_coordinates_invalid", "not_started")
            if action == "scroll":
                delta = {key: arguments[key] for key in ("deltaX", "deltaY")}
                if not all(type(value) in {float, int} and math.isfinite(value) for value in delta.values()):
                    raise BrowserPipeError("browser_coordinates_invalid", "not_started")
                await effect(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseWheel", **coordinates, **delta},
                )
            else:
                for phase in ("mousePressed", "mouseReleased"):
                    await effect(
                        "Input.dispatchMouseEvent",
                        {"type": phase, **coordinates, "button": "left", "clickCount": 1},
                    )
        elif action == "type":
            await effect("Input.insertText", {"text": arguments["text"]})
        elif action == "key":
            for phase in ("keyDown", "keyUp"):
                await effect(
                    "Input.dispatchKeyEvent",
                    {"type": phase, "key": arguments["key"], "modifiers": arguments.get("modifiers", 0)},
                )
        else:
            raise BrowserPipeError("browser_action_unsupported", "not_started")

    async def close(self):
        if not self.pipe.closed:
            try:
                await self.pipe.call("Browser.close", {}, timeout=2)
            except BrowserPipeError:
                pass  # The bridge still has to prove owned-process termination.
        await self.pipe.close()
