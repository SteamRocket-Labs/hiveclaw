from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from app.runtime.session import SessionContext


class SessionLiveBufferOverflow(RuntimeError):
    """Live delivery exceeded its bounded pre-ready buffer; DB replay owns recovery."""


WEB_CHAT_SOCKET_SEND_TIMEOUT_SECONDS = 1.0


class WebChatBroker:
    """Session-scoped WebSocket broadcaster for web chat runs."""

    def __init__(
        self,
        *,
        live_buffer_limit: int = 10_000,
        send_timeout_seconds: float = WEB_CHAT_SOCKET_SEND_TIMEOUT_SECONDS,
    ) -> None:
        self.active_connections: dict[str, list[tuple[Any, str | None]]] = {}
        self._runtime_sessions: dict[str, SessionContext] = {}
        self._runtime_session_order: list[str] = []
        self._lock = asyncio.Lock()
        self._subscription_buffers: dict[int, list[dict[str, Any]]] = {}
        self._subscription_draining: set[int] = set()
        self._subscription_overflowed: set[int] = set()
        self._subscription_storage_offsets: dict[int, int] = {}
        self._live_buffer_limit = max(1, int(live_buffer_limit))
        self._send_timeout_seconds = max(0.01, float(send_timeout_seconds))

    @staticmethod
    def _runtime_session_key(agent_id: str, session_id: str | None) -> str | None:
        if not session_id:
            return None
        return f"{agent_id}:{session_id}"

    async def connect(self, agent_id: str, websocket: Any, session_id: str | None = None) -> None:
        await websocket.accept()
        async with self._lock:
            self.active_connections.setdefault(agent_id, []).append((websocket, session_id))

    async def disconnect(self, agent_id: str, websocket: Any) -> None:
        async with self._lock:
            if agent_id not in self.active_connections:
                return
            self.active_connections[agent_id] = [
                (ws, sid) for ws, sid in self.active_connections[agent_id] if ws != websocket
            ]
            self._subscription_buffers.pop(id(websocket), None)
            self._subscription_draining.discard(id(websocket))
            self._subscription_overflowed.discard(id(websocket))
            self._subscription_storage_offsets.pop(id(websocket), None)

    async def begin_session_subscription(
        self,
        agent_id: str,
        websocket: Any,
        session_id: str,
    ) -> None:
        """Register a socket in buffering mode before reading its DB watermark."""

        async with self._lock:
            connections = self.active_connections.setdefault(agent_id, [])
            if not any(ws is websocket for ws, _sid in connections):
                connections.append((websocket, session_id))
            self._subscription_buffers[id(websocket)] = []
            self._subscription_overflowed.discard(id(websocket))
            self._subscription_storage_offsets.pop(id(websocket), None)

    async def activate_session_subscription(
        self,
        websocket: Any,
        *,
        delivered_through_sequence: int,
        delivered_through_delivery_sequence: int | None = None,
    ) -> None:
        """Drain watermark-newer live frames, then atomically switch to live mode."""

        socket_key = id(websocket)
        if socket_key in self._subscription_overflowed:
            raise SessionLiveBufferOverflow("session_live_buffer_overflow")
        self._subscription_draining.add(socket_key)
        cursor = int(delivered_through_sequence)
        delivery_cursor = (
            cursor if delivered_through_delivery_sequence is None else int(delivered_through_delivery_sequence)
        )
        storage_offset = cursor - delivery_cursor
        self._subscription_storage_offsets[socket_key] = storage_offset
        while True:
            async with self._lock:
                pending = self._subscription_buffers.get(socket_key)
                if pending is None:
                    self._subscription_draining.discard(socket_key)
                    return
                if socket_key in self._subscription_overflowed:
                    raise SessionLiveBufferOverflow("session_live_buffer_overflow")
                batch = list(pending)
                pending.clear()
                if not batch:
                    self._subscription_buffers.pop(socket_key, None)
                    self._subscription_draining.discard(socket_key)
                    return
            canonical = [
                frame
                for frame in batch
                if frame.get("schema") == "hive.session_event"
                and isinstance(frame.get("sequence"), int)
                and int(frame["sequence"]) > cursor
            ]
            passthrough = [frame for frame in batch if frame.get("schema") != "hive.session_event"]
            canonical.sort(key=lambda frame: int(frame["sequence"]))
            seen_event_ids_by_sequence: dict[int, str] = {}
            for frame in canonical:
                sequence = int(frame["sequence"])
                event_id = str(frame.get("event_id") or "")
                if seen_event_ids_by_sequence.get(sequence) == event_id:
                    continue
                seen_event_ids_by_sequence[sequence] = event_id
                if storage_offset > 0:
                    from app.services.session_delivery_cursor import project_future_session_event_for_delivery

                    frame = project_future_session_event_for_delivery(
                        frame,
                        storage_sequence=sequence,
                        storage_offset=storage_offset,
                    )
                await websocket.send_json(frame)
                cursor = max(cursor, sequence)
            for frame in passthrough:
                await websocket.send_json(frame)

    async def send_message(self, agent_id: str, message: dict[str, Any]) -> None:
        await self.send_session_message(agent_id, None, message, include_agent_wide=True)

    async def send_session_message(
        self,
        agent_id: str,
        session_id: str | None,
        message: dict[str, Any],
        *,
        include_agent_wide: bool = False,
    ) -> None:
        dead: list[tuple[Any, str | None]] = []
        async with self._lock:
            if agent_id not in self.active_connections:
                return
            direct: list[tuple[Any, str | None, dict[str, Any]]] = []
            for ws, sid in list(self.active_connections[agent_id]):
                if session_id is not None and sid != session_id:
                    continue
                if session_id is None and not include_agent_wide and sid is not None:
                    continue
                try:
                    buffer = self._subscription_buffers.get(id(ws))
                    if buffer is not None:
                        if len(buffer) >= self._live_buffer_limit:
                            self._subscription_overflowed.add(id(ws))
                        else:
                            buffer.append(dict(message))
                    else:
                        projected = message
                        storage_offset = self._subscription_storage_offsets.get(id(ws), 0)
                        if (
                            storage_offset > 0
                            and message.get("schema") == "hive.session_event"
                            and isinstance(message.get("sequence"), int)
                        ):
                            from app.services.session_delivery_cursor import project_future_session_event_for_delivery

                            projected = project_future_session_event_for_delivery(
                                message,
                                storage_sequence=int(message["sequence"]),
                                storage_offset=storage_offset,
                            )
                        direct.append((ws, sid, projected))
                except Exception:  # noqa: BLE001 - one dead socket must not block the session.
                    dead.append((ws, sid))
            if direct:

                async def bounded_send(websocket: Any, payload: dict[str, Any]) -> Exception | None:
                    try:
                        await asyncio.wait_for(
                            websocket.send_json(payload),
                            timeout=self._send_timeout_seconds,
                        )
                    except Exception as exc:  # noqa: BLE001 - durable cursor owns transport recovery.
                        return exc
                    return None

                outcomes = await asyncio.gather(*(bounded_send(ws, payload) for ws, _sid, payload in direct))
                dead.extend(
                    (connection[0], connection[1])
                    for connection, outcome in zip(direct, outcomes)
                    if outcome is not None
                )
            for item in dead:
                self.active_connections[agent_id] = [
                    connection for connection in self.active_connections[agent_id] if connection != item
                ]
        if not dead:
            return

        logger.warning(
            "[WebChatBroker] Removed {} stalled or failed websocket connection(s); "
            "durable Session cursor owns recovery",
            len(dead),
        )

        async def close_connection(websocket: Any) -> None:
            close = getattr(websocket, "close", None)
            if not callable(close):
                return
            try:
                await asyncio.wait_for(close(code=1011), timeout=self._send_timeout_seconds)
            except Exception:  # noqa: BLE001 - connection is already removed from live delivery.
                return

        await asyncio.gather(*(close_connection(ws) for ws, _sid in dead))

    async def get_active_session_ids(self, agent_id: str) -> list[str]:
        async with self._lock:
            if agent_id not in self.active_connections:
                return []
            return list({sid for _ws, sid in self.active_connections[agent_id] if sid})

    async def get_or_create_runtime_session(self, agent_id: str, session_id: str | None) -> SessionContext:
        if not session_id:
            return SessionContext(source="web", channel="web")

        async with self._lock:
            key = self._runtime_session_key(agent_id, session_id)
            assert key is not None
            session = self._runtime_sessions.get(key)
            if session is None:
                session = SessionContext(session_id=session_id, source="web", channel="web")
                self._runtime_sessions[key] = session
                self._runtime_session_order.append(key)
                if len(self._runtime_session_order) > 200:
                    evict_key = self._runtime_session_order.pop(0)
                    self._runtime_sessions.pop(evict_key, None)
            else:
                if key in self._runtime_session_order:
                    self._runtime_session_order.remove(key)
                self._runtime_session_order.append(key)
            return session


web_chat_broker = WebChatBroker()
