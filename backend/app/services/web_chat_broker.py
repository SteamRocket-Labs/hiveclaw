from __future__ import annotations

import asyncio
from typing import Any

from app.runtime.session import SessionContext


class WebChatBroker:
    """Session-scoped WebSocket broadcaster for web chat runs."""

    def __init__(self) -> None:
        self.active_connections: dict[str, list[tuple[Any, str | None]]] = {}
        self._runtime_sessions: dict[str, SessionContext] = {}
        self._runtime_session_order: list[str] = []
        self._lock = asyncio.Lock()

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
        async with self._lock:
            if agent_id not in self.active_connections:
                return
            dead: list[tuple[Any, str | None]] = []
            for ws, sid in list(self.active_connections[agent_id]):
                if session_id is not None and sid != session_id:
                    continue
                if session_id is None and not include_agent_wide and sid is not None:
                    continue
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.append((ws, sid))
            for item in dead:
                self.active_connections[agent_id] = [
                    connection for connection in self.active_connections[agent_id] if connection != item
                ]

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
