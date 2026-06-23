from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


class HiveBridgeClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def _url(self, path: str) -> str:
        if path.startswith("/"):
            path = path[1:]
        return f"{self.base_url}/{path}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._headers())
        response = self._client.request(method, self._url(path), headers=headers, **kwargs)
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()

    def init_pairing(
        self,
        *,
        device_name: str,
        client_kind: str,
        device_fingerprint: str,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/local-bridge/pairing/init",
            json={
                "device_name": device_name,
                "client_kind": client_kind,
                "device_fingerprint": device_fingerprint,
                "scopes": scopes or [],
            },
        )

    def exchange_pairing(self, device_code: str) -> dict[str, Any]:
        return self._request("POST", "/api/v1/local-bridge/pairing/exchange", json={"device_code": device_code})

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/local-bridge/status")

    def poll_inbox(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/gateway/poll")

    def send_message(
        self,
        *,
        target: str,
        content: str,
        channel: str | None = None,
        client_message_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"target": target, "content": content}
        if channel:
            payload["channel"] = channel
        if client_message_id:
            payload["client_message_id"] = client_message_id
        return self._request("POST", "/api/v1/gateway/send-message", json=payload)

    def report_result(
        self,
        *,
        message_id: str,
        result: str,
        attachments: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/gateway/report",
            json={
                "message_id": message_id,
                "result": result,
                "attachments": attachments or [],
                "metadata": metadata or {},
            },
        )

    def create_channel_ws_ticket(self) -> dict[str, Any]:
        return self._request("POST", "/api/v1/local-bridge/channel/ws-ticket")

    def channel_ws_url(self, ticket: str) -> str:
        if self.base_url.startswith("https://"):
            base = f"wss://{self.base_url[len('https://'):]}"
        elif self.base_url.startswith("http://"):
            base = f"ws://{self.base_url[len('http://'):]}"
        else:
            base = self.base_url
        return f"{base}/api/v1/local-bridge/channel/ws?ticket={quote(ticket)}"

    def poll_channel(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/local-bridge/channel/poll")

    def report_channel_event(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": session_id,
            "message_id": message_id,
            "type": event_type,
            "payload": payload or {},
        }
        return self._request("POST", "/api/v1/local-bridge/channel/events", json=body)

    def report_channel_result(
        self,
        *,
        session_id: str,
        message_id: str,
        output: str,
        status: str = "completed",
        artifacts: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/local-bridge/channel/report",
            json={
                "session_id": session_id,
                "message_id": message_id,
                "status": status,
                "output": output,
                "artifacts": artifacts or [],
                "metadata": metadata or {},
            },
        )

    def upload_file(self, path: str | Path) -> dict[str, Any]:
        file_path = Path(path).expanduser()
        with file_path.open("rb") as fh:
            return self._request(
                "POST",
                "/api/v1/local-bridge/upload",
                files={"file": (file_path.name, fh, "application/octet-stream")},
            )

    def download_channel_file(self, path: str, destination_dir: str | Path) -> dict[str, Any]:
        destination = Path(destination_dir).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        filename = Path(path.replace("\\", "/")).name or "download"
        save_path = destination / filename
        if save_path.exists():
            stem = save_path.stem
            suffix = save_path.suffix
            counter = 1
            while save_path.exists():
                save_path = destination / f"{stem}_{counter}{suffix}"
                counter += 1

        response = self._client.get(
            self._url("/api/v1/local-bridge/channel/workspace/download"),
            headers=self._headers(),
            params={"path": path},
        )
        response.raise_for_status()
        save_path.write_bytes(response.content)
        return {
            "path": str(save_path),
            "source_path": path,
            "size": save_path.stat().st_size,
        }
