from __future__ import annotations

from pathlib import Path
from typing import Any

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

    def upload_file(self, path: str | Path) -> dict[str, Any]:
        file_path = Path(path).expanduser()
        with file_path.open("rb") as fh:
            return self._request(
                "POST",
                "/api/v1/local-bridge/upload",
                files={"file": (file_path.name, fh, "application/octet-stream")},
            )
