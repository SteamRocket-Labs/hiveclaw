"""Feishu WebSocket Long Connection Manager."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict
import uuid

from loguru import logger

try:
    import lark_oapi as lark
    import lark_oapi.ws as ws
    import lark_oapi.ws.client as _lark_ws_client

    _HAS_LARK = True
except ImportError:
    lark = None  # type: ignore
    ws = None  # type: ignore
    _lark_ws_client = None  # type: ignore
    _HAS_LARK = False

if _HAS_LARK:
    try:
        import websockets as _websockets

        _orig_websockets_connect = _websockets.connect
        _PROXY_PATCH_AVAILABLE = True
    except ImportError:
        _PROXY_PATCH_AVAILABLE = False
else:
    _PROXY_PATCH_AVAILABLE = False

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.channel_config import ChannelConfig
from app.services.tenant_resolver import resolve_tenant_for_agent
from sqlalchemy import select


if not _HAS_LARK:
    logger.warning(
        "[Feishu WS] lark-oapi package not installed. "
        "Feishu WebSocket features will be disabled. "
        "Install with: pip install lark-oapi"
    )


_FEISHU_WS_INITIAL_RETRY_DELAY_SECONDS = 5
_FEISHU_WS_MAX_RETRY_DELAY_SECONDS = 60
_FEISHU_WS_CONNECTION_HEALTHCHECK_SECONDS = 30
_FEISHU_WS_INVALID_CREDENTIALS_CODE = 1000040345


def _is_non_retryable_feishu_ws_error(exc: Exception) -> bool:
    """Classify Feishu SDK errors that require config changes instead of reconnect retries."""
    code = getattr(exc, "code", None)
    if code == _FEISHU_WS_INVALID_CREDENTIALS_CODE or str(code) == str(_FEISHU_WS_INVALID_CREDENTIALS_CODE):
        return True

    message = str(exc).lower()
    return "app_id or app_secret is invalid" in message


def _describe_client_connection_state(client: Any) -> str:
    conn = getattr(client, "_conn", None)
    if conn is None:
        return "missing"
    if getattr(conn, "closed", False):
        return "closed"
    state = getattr(conn, "state", None)
    state_name = getattr(state, "name", None)
    if state_name:
        return str(state_name).lower()
    if state is not None:
        return str(state).lower()
    return "open"


def _is_client_connection_alive(client: Any) -> bool:
    state = _describe_client_connection_state(client)
    return state not in {"missing", "closed", "closing"}


def _is_expected_lark_receive_loop_disconnect(exc: Exception) -> bool:
    """Return true for normal SDK websocket close signals handled by our reconnect loop."""
    exc_name = type(exc).__name__
    if isinstance(exc, (ConnectionError, ConnectionResetError)):
        return True
    if "ConnectionClosed" in exc_name:
        return True

    message = str(exc).lower()
    return (
        "no close frame received or sent" in message
        or "connection reset by peer" in message
        or "connection is closed" in message
    )


def _patch_lark_receive_loop_for_supervised_reconnect(client: Any, agent_id: uuid.UUID) -> None:
    """Prevent SDK receive-loop task exceptions from escaping our outer reconnect supervisor."""
    if getattr(client, "_hive_receive_loop_patched", False):
        return
    if not callable(getattr(client, "_receive_message_loop", None)):
        return

    async def _receive_message_loop() -> None:
        try:
            while True:
                conn = getattr(client, "_conn", None)
                if conn is None:
                    raise ConnectionError("connection is closed")

                msg = await conn.recv()
                sdk_loop = getattr(_lark_ws_client, "loop", None)
                loop = sdk_loop or asyncio.get_running_loop()
                loop.create_task(client._handle_message(msg))
        except Exception as exc:
            try:
                await client._disconnect()
            except Exception as disconnect_exc:
                logger.debug(
                    "[Feishu WS] Failed to disconnect after receive loop ended for "
                    f"{agent_id}: {type(disconnect_exc).__name__}: {disconnect_exc}"
                )

            if _is_expected_lark_receive_loop_disconnect(exc):
                logger.info(
                    "[Feishu WS] Receive loop ended for "
                    f"{agent_id}; reconnect supervisor will retry: {type(exc).__name__}: {exc}"
                )
                return

            logger.opt(exception=True).error(f"[Feishu WS] Receive loop crashed for {agent_id}: {exc}")
            raise

    client._receive_message_loop = _receive_message_loop
    client._hive_receive_loop_patched = True


def _bind_lark_ws_client_loop_to_current_loop() -> None:
    """Ensure the SDK schedules its receive loop on uvicorn's running event loop."""
    if _lark_ws_client is None:
        return
    running_loop = asyncio.get_running_loop()
    if getattr(_lark_ws_client, "loop", None) is not running_loop:
        _lark_ws_client.loop = running_loop
        logger.debug("[Feishu WS] Bound lark SDK websocket loop to current running loop")


def _connect_supports_proxy_kwarg(orig_connect) -> bool:
    """Return true only when the websockets connect callable explicitly accepts proxy."""
    import inspect

    try:
        return "proxy" in inspect.signature(orig_connect).parameters
    except (TypeError, ValueError):
        return False


def _make_no_proxy_connect(orig_connect):
    """Temporarily force `proxy=None` during the WebSocket handshake only."""
    import contextlib

    supports_proxy_kwarg = _connect_supports_proxy_kwarg(orig_connect)

    class _NoProxyConnect:
        def __init__(self, *args, **kwargs):
            if supports_proxy_kwarg:
                kwargs.setdefault("proxy", None)
            else:
                kwargs.pop("proxy", None)
            self._coro = orig_connect(*args, **kwargs)
            self._ws = None

        def __await__(self):
            return self._coro.__await__()

        async def __aenter__(self):
            self._ws = await self._coro
            return self._ws

        async def __aexit__(self, *exc):
            if self._ws:
                await self._ws.close()
            return False

    @contextlib.asynccontextmanager
    async def _scoped_no_proxy():
        if not _PROXY_PATCH_AVAILABLE:
            yield
            return

        previous_connect = _websockets.connect
        _websockets.connect = _NoProxyConnect
        logger.debug("[Feishu WS] Scoped websockets proxy bypass: active")
        try:
            yield
        finally:
            _websockets.connect = previous_connect
            logger.debug("[Feishu WS] Scoped websockets proxy bypass: restored")

    return _scoped_no_proxy


class FeishuWSManager:
    """Manages Feishu WebSocket clients for all agents."""

    def __init__(self):
        self._clients: Dict[uuid.UUID, ws.Client] = {}
        # Tasks for reconnection or ping loops if we want to cancel them later
        self._tasks: Dict[uuid.UUID, asyncio.Task] = {}

    async def _mark_channel_status(
        self,
        agent_id: uuid.UUID,
        *,
        is_connected: bool,
        connection_status: str,
        error: str | None = None,
        is_configured: bool | None = None,
    ) -> None:
        """Persist Feishu websocket health so bad credentials do not restart forever."""
        now = datetime.now(timezone.utc)
        try:
            # Daemon/stream context — no request GUC. Resolve the owning tenant so
            # the SELECT+UPDATE works after the stage-3 non-owner role flip
            # (a bare session fail-closes and would silently stop persisting health).
            tid = await resolve_tenant_for_agent(agent_id)
            async with tenant_scoped_session(tid) as db:
                result = await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == "feishu",
                    )
                )
                config = result.scalar_one_or_none()
                if not config:
                    return

                config.is_connected = is_connected
                config.last_tested_at = now
                if is_configured is not None:
                    config.is_configured = is_configured

                extra = dict(config.extra_config or {})
                extra["connection_status"] = connection_status
                extra["last_connection_checked_at"] = now.isoformat()
                if error:
                    extra["last_connection_error"] = error[:500]
                    extra["last_connection_error_at"] = now.isoformat()
                else:
                    extra.pop("last_connection_error", None)
                    extra.pop("last_connection_error_at", None)
                config.extra_config = extra
                await db.commit()
        except Exception as exc:
            logger.debug(f"[Feishu WS] Failed to persist channel status for {agent_id}: {exc}")

    def _create_event_handler(self, agent_id: uuid.UUID) -> lark.EventDispatcherHandler:
        """Create an event dispatcher for a specific agent."""

        def handle_message(data: Any) -> None:
            """Handle im.message.receive_v1 events from Feishu WebSocket."""
            try:
                # The data object carries the raw event body
                raw_body = getattr(data, "raw_body", None)
                if not raw_body:
                    # Some SDK versions pass the dict directly
                    if isinstance(data, dict):
                        body_dict = data
                    else:
                        # Handle lark_oapi.event.custom.CustomizedEvent
                        body_dict = {}
                        if hasattr(data, "header"):
                            header_obj = data.header
                            body_dict["header"] = (
                                vars(header_obj)
                                if hasattr(header_obj, "__dict__")
                                else {
                                    "event_type": getattr(header_obj, "event_type", "im.message.receive_v1"),
                                    "event_id": getattr(header_obj, "event_id", ""),
                                    "create_time": getattr(header_obj, "create_time", ""),
                                }
                            )
                            # Ensure event_type is present as it's required downstream
                            if "event_type" not in body_dict["header"]:
                                body_dict["header"]["event_type"] = getattr(
                                    header_obj, "event_type", "im.message.receive_v1"
                                )
                        else:
                            body_dict["header"] = {"event_type": "im.message.receive_v1"}

                        if hasattr(data, "event"):
                            body_dict["event"] = data.event
                        elif hasattr(data, "content") and isinstance(getattr(data, "content"), str):
                            try:
                                body_dict["event"] = json.loads(data.content)
                            except json.JSONDecodeError:
                                body_dict["event"] = {"content": data.content}

                        if not hasattr(data, "header") and not hasattr(data, "event"):
                            logger.warning(
                                f"[Feishu WS] Unexpected event data type with no recognizable fields: {type(data)}"
                            )
                            return
                else:
                    body_dict = json.loads(raw_body.decode("utf-8"))

                loop = asyncio.get_running_loop()
                loop.create_task(self._async_handle_message(agent_id, data))
            except RuntimeError:
                try:
                    # If no running loop in this thread, try to find the main event loop
                    # This is a heuristic and might need adjustment depending on the exact async framework setup
                    main_loop = [t for t in asyncio.all_tasks() if t.get_name() != "feishu-ws"][0].get_loop()
                    asyncio.run_coroutine_threadsafe(self._async_handle_message(agent_id, data), main_loop)
                except Exception as e:
                    logger.opt(exception=True).error(f"[Feishu WS] Could not dispatch event to main loop: {e}")

        def _noop_handler(data: Any) -> None:
            """Ack events we don't need to process (suppresses 'processor not found' errors)."""
            pass

        def handle_card_action(data: Any) -> None:
            """Handle card.action.trigger events from Feishu WebSocket."""
            try:
                logger.info(f"[Feishu WS] Card action received for agent {agent_id}")
                coro = self._async_handle_card_action(agent_id, data)
                loop = asyncio.get_running_loop()
                if hasattr(loop, "create_task"):
                    loop.create_task(coro)
                else:
                    asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError:
                try:
                    main_loop = [t for t in asyncio.all_tasks() if t.get_name() != "feishu-ws"][0].get_loop()
                    asyncio.run_coroutine_threadsafe(self._async_handle_card_action(agent_id, data), main_loop)
                except Exception as e:
                    logger.opt(exception=True).error(f"[Feishu WS] Could not dispatch card action to main loop: {e}")
            except Exception as e:
                logger.opt(exception=True).error(f"[Feishu WS] Could not dispatch card action: {e}")

        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_customized_event("im.message.receive_v1", handle_message)
            .register_p2_customized_event("card.action.trigger", handle_card_action)
            .register_p2_customized_event("im.chat.access_event.bot_p2p_chat_entered_v1", _noop_handler)
            .register_p2_customized_event("im.message.message_read_v1", _noop_handler)
            .build()
        )
        return dispatcher

    async def _async_handle_message(self, agent_id: uuid.UUID, data: Dict[str, Any]) -> None:
        """Handle im.message.receive_v1 events from Feishu WebSocket asynchronously."""
        try:
            # The data object carries the raw event body
            raw_body = getattr(data, "raw_body", None)
            if not raw_body:
                # Some SDK versions pass the dict directly
                if isinstance(data, dict):
                    body_dict = data
                else:
                    # Handle lark_oapi.event.custom.CustomizedEvent
                    body_dict = {}
                    if hasattr(data, "header"):
                        header_obj = data.header
                        body_dict["header"] = (
                            vars(header_obj)
                            if hasattr(header_obj, "__dict__")
                            else {
                                "event_type": getattr(header_obj, "event_type", "im.message.receive_v1"),
                                "event_id": getattr(header_obj, "event_id", ""),
                                "create_time": getattr(header_obj, "create_time", ""),
                            }
                        )
                        if "event_type" not in body_dict["header"]:
                            body_dict["header"]["event_type"] = getattr(
                                header_obj, "event_type", "im.message.receive_v1"
                            )
                    else:
                        body_dict["header"] = {"event_type": "im.message.receive_v1"}

                    if hasattr(data, "event"):
                        body_dict["event"] = data.event
                    elif hasattr(data, "content") and isinstance(getattr(data, "content"), str):
                        try:
                            body_dict["event"] = json.loads(data.content)
                        except json.JSONDecodeError:
                            body_dict["event"] = {"content": data.content}

                    if not hasattr(data, "header") and not hasattr(data, "event"):
                        logger.warning(
                            f"[Feishu WS] Unexpected event data type with no recognizable fields: {type(data)}"
                        )
                        return
            else:
                body_dict = json.loads(raw_body.decode("utf-8"))

            event_type = body_dict.get("header", {}).get("event_type", "unknown")
            logger.info(f"[Feishu WS] Event received for agent {agent_id}: {event_type}")

            # Import here to avoid circular dependencies
            from app.api.feishu import process_feishu_event

            tid = await resolve_tenant_for_agent(agent_id)
            async with tenant_scoped_session(tid) as db:
                await process_feishu_event(agent_id, body_dict, db)

        except Exception as e:
            logger.opt(exception=True).error(f"[Feishu WS] Error processing event for {agent_id}: {e}")

    async def _async_handle_card_action(self, agent_id: uuid.UUID, data: Dict[str, Any]) -> None:
        """Handle card.action.trigger events from Feishu WebSocket."""
        try:
            import json as _json_ca

            if isinstance(data, dict):
                body_dict = data
            else:
                raw_body = getattr(data, "raw_body", None)
                if raw_body:
                    body_dict = _json_ca.loads(raw_body.decode("utf-8"))
                elif hasattr(data, "event"):
                    body_dict = {"event": data.event}
                    if hasattr(data, "header"):
                        body_dict["header"] = vars(data.header) if hasattr(data.header, "__dict__") else {}
                else:
                    logger.warning(f"[Feishu WS] Unrecognized card action data type: {type(data)}")
                    return

            # Forward to the card callback handler
            from app.api.feishu import feishu_card_callback

            class _CardRequest:
                async def json(self):
                    return body_dict.get("event", body_dict)

            tid = await resolve_tenant_for_agent(agent_id)
            async with tenant_scoped_session(tid) as db:
                await feishu_card_callback(_CardRequest(), db)

        except Exception as e:
            logger.opt(exception=True).error(f"[Feishu WS] Error processing card action for {agent_id}: {e}")

    async def start_client(
        self,
        agent_id: uuid.UUID,
        app_id: str,
        app_secret: str,
        stop_existing: bool = True,
    ):
        """Spawns a WebSocket client fully asynchronously inside FastAPI's loop."""
        if not _HAS_LARK:
            logger.warning("[Feishu WS] lark-oapi not installed, cannot start client")
            return
        if not app_id or not app_secret:
            logger.warning(f"[Feishu WS] Missing app_id or app_secret for {agent_id}, skipping")
            return

        logger.info(f"[Feishu WS] Starting async WS client for agent {agent_id} (App ID: {app_id})")

        # Stop existing client task if any
        if stop_existing and agent_id in self._tasks:
            old_task = self._tasks.pop(agent_id, None)
            if old_task and not old_task.done():
                old_task.cancel()
                logger.info(f"[Feishu WS] Cancelled old WS task for {agent_id}")

        try:
            event_handler = self._create_event_handler(agent_id)
        except Exception as e:
            logger.opt(exception=True).error(f"[Feishu WS] Failed to create event handler for {agent_id}: {e}")
            return

        # Instantiate Client
        client = ws.Client(
            app_id,
            app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=False,
        )
        _patch_lark_receive_loop_for_supervised_reconnect(client, agent_id)
        self._clients[agent_id] = client
        no_proxy_ctx = _make_no_proxy_connect(_orig_websockets_connect) if _PROXY_PATCH_AVAILABLE else None

        # Direct Async runner bypassing the faulty client.start()
        async def _run_async_client():
            retry_delay = _FEISHU_WS_INITIAL_RETRY_DELAY_SECONDS
            try:
                while True:
                    try:
                        # Internally _connect() opens socket and drops _receive_message_loop() onto the global loop
                        _bind_lark_ws_client_loop_to_current_loop()
                        if no_proxy_ctx:
                            async with no_proxy_ctx():
                                await client._connect()
                        else:
                            await client._connect()
                        await self._mark_channel_status(
                            agent_id,
                            is_connected=True,
                            connection_status="connected",
                        )
                        retry_delay = _FEISHU_WS_INITIAL_RETRY_DELAY_SECONDS
                        # Start ping loop natively
                        asyncio.create_task(client._ping_loop())

                        # Keep this task alive so it doesn't get canceled, and handle reconnections
                        while True:
                            await asyncio.sleep(_FEISHU_WS_CONNECTION_HEALTHCHECK_SECONDS)
                            if not _is_client_connection_alive(client):
                                state = _describe_client_connection_state(client)
                                raise ConnectionError(f"Feishu SDK websocket is no longer open ({state})")
                    except Exception as e:
                        if _is_non_retryable_feishu_ws_error(e):
                            error = f"{type(e).__name__}: {e}"
                            logger.error(
                                "[Feishu WS] Async client configuration error for "
                                f"{agent_id}; disabling until credentials are updated: {error}"
                            )
                            await self._mark_channel_status(
                                agent_id,
                                is_connected=False,
                                is_configured=False,
                                connection_status="invalid_credentials",
                                error=error,
                            )
                            await client._disconnect()
                            self._clients.pop(agent_id, None)
                            self._tasks.pop(agent_id, None)
                            return

                        error = f"{type(e).__name__}: {e}"
                        logger.warning(
                            "[Feishu WS] Async client connection failed for "
                            f"{agent_id}; retrying in {retry_delay}s: {error}"
                        )
                        await self._mark_channel_status(
                            agent_id,
                            is_connected=False,
                            connection_status="transient_error",
                            error=error,
                        )
                        await client._disconnect()
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, _FEISHU_WS_MAX_RETRY_DELAY_SECONDS)
                        self._clients[agent_id] = client
            except asyncio.CancelledError:
                logger.info(f"[Feishu WS] Async client task cancelled for {agent_id}")
                await client._disconnect()
            except Exception as e:
                logger.opt(exception=True).error(f"[Feishu WS] Async client fatal exception for {agent_id}: {e}")
                await client._disconnect()
                self._clients.pop(agent_id, None)

        task = asyncio.create_task(_run_async_client(), name=f"feishu-ws-async-{str(agent_id)[:8]}")
        self._tasks[agent_id] = task
        logger.info(f"[Feishu WS] Async WS task scheduled for agent {agent_id}")

    async def stop_client(self, agent_id: uuid.UUID):
        """Stops an actively running WebSocket client for an agent."""
        if agent_id in self._tasks:
            task = self._tasks.pop(agent_id)
            if not task.done():
                task.cancel()
                logger.info(f"[Feishu WS] Stopped client task for agent {agent_id}")
        if agent_id in self._clients:
            client = self._clients.pop(agent_id)
            try:
                await client._disconnect()
            except Exception as e:
                logger.error(f"[Feishu WS] Error disconnecting client for {agent_id}: {e}")

    async def start_all(self):
        """Start WS clients for all configured Feishu agents."""
        if not _HAS_LARK:
            logger.info("[Feishu WS] lark-oapi not installed, skipping Feishu WS initialization")
            return
        logger.info("[Feishu WS] Initializing all active Feishu channels...")
        async with (
            async_session() as db,
            enter_rls_bypass(db, reason="feishu start_all — enumerate all configured channels across tenants"),
        ):
            result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.is_configured,
                    ChannelConfig.channel_type == "feishu",
                )
            )
            configs = result.scalars().all()

        for config in configs:
            extra = config.extra_config or {}
            mode = extra.get("connection_mode", "webhook")
            if mode == "websocket":
                if config.app_id and config.app_secret:
                    await self.start_client(config.agent_id, config.app_id, config.app_secret, stop_existing=False)
                else:
                    logger.warning(f"[Feishu WS] Skipping agent {config.agent_id}: missing credentials")

    def status(self) -> dict:
        """Return status of all active WS tasks."""
        return {str(aid): not self._tasks[aid].done() for aid in self._tasks}


feishu_ws_manager = FeishuWSManager()
