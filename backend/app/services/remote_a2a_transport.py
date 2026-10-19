"""A2A 1.0 JSON-RPC over administrator-configured, governed endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
import binascii
import json
from typing import Any
from uuid import uuid4

import httpx

from app.services.exact_secret_boundary import ExactSecretBoundary
from app.services.governed_egress import EgressLimits, GovernedEgressError, request_governed_http


class RemoteA2AError(RuntimeError):
    """A protocol observation, never proof that remote effects did not happen."""

    def __init__(
        self,
        code: str,
        *,
        execution_status: str = "not_started",
        remote_code: int | None = None,
        observation: dict | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.execution_status = execution_status
        self.remote_code = remote_code
        self.observation = observation


def _same_endpoint(left: str, right: str) -> bool:
    try:
        return httpx.URL(left) == httpx.URL(right)
    except httpx.InvalidURL:
        return False


@dataclass(frozen=True)
class RemoteA2AEndpoint:
    card_url: str
    rpc_url: str
    bearer_token: str | None = field(default=None, repr=False)
    allowed_networks: tuple[str, ...] = ()
    max_response_bytes: int = 16 * 1024 * 1024
    timeout_seconds: float = 20.0

    def _boundary(self) -> ExactSecretBoundary:
        return ExactSecretBoundary.from_pairs(
            (("remote-a2a://credential/bearer", self.bearer_token),) if self.bearer_token else ()
        )

    def redact_payload(self, payload):
        boundary = self._boundary()

        def redact(value):
            if isinstance(value, list):
                return [redact(item) for item in value]
            if not isinstance(value, dict):
                return value
            result = {key: redact(item) for key, item in value.items()}
            if isinstance(result.get("raw"), str):
                try:
                    body = base64.b64decode(result["raw"], validate=True)
                except (ValueError, binascii.Error):
                    return result
                if boundary.match_binary_chunks((body,)):
                    result["raw"] = base64.b64encode(b"[REDACTED_SECRET]").decode()
                    metadata = result.get("metadata")
                    result["metadata"] = {**(metadata if isinstance(metadata, dict) else {}),
                        "hiveRemoteContentStatus": "credential_redacted"}
            return result

        return redact(boundary.redact_payload_with_evidence(payload).value)

    async def _request(self, method: str, url: str, *, payload: dict | None = None) -> dict:
        headers = {"Accept": "application/json", "A2A-Version": "1.0"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        try:
            response = await request_governed_http(
                method,
                url,
                headers=headers,
                json_body=payload,
                allowed_networks=self.allowed_networks,
                secret_values=(self.bearer_token,) if self.bearer_token else (),
                limits=EgressLimits(
                    max_redirects=0,
                    max_wire_bytes=self.max_response_bytes,
                    max_decoded_bytes=self.max_response_bytes,
                    total_timeout_seconds=self.timeout_seconds,
                    require_tls_for_credentials=True,
                ),
            )
        except GovernedEgressError as exc:
            raise RemoteA2AError(exc.code, execution_status="unknown" if method == "POST" else "unavailable") from None
        except (httpx.HTTPError, OSError, ValueError):
            raise RemoteA2AError(
                "remote_a2a_transport_unavailable", execution_status="unknown" if method == "POST" else "unavailable"
            ) from None
        if not 200 <= response.status_code < 300:
            raise RemoteA2AError(
                f"remote_a2a_http_{response.status_code}",
                execution_status="unknown" if method == "POST" else "unavailable",
            )
        try:
            value = response.json()
        except (ValueError, UnicodeError):
            raise RemoteA2AError(
                "remote_a2a_invalid_json", execution_status="unknown" if method == "POST" else "unavailable"
            ) from None
        if not isinstance(value, dict):
            raise RemoteA2AError(
                "remote_a2a_invalid_response", execution_status="unknown" if method == "POST" else "unavailable"
            )
        # A remote endpoint may echo its credential. Only exact active secret
        # bytes are redacted; all other model-authored content stays intact.
        return self.redact_payload(value)

    async def discover(self) -> dict[str, Any]:
        from a2a.types import a2a_pb2 as protocol
        from google.protobuf.json_format import ParseDict, ParseError

        card = await self._request("GET", self.card_url)
        try:
            parsed = ParseDict(card, protocol.AgentCard(), ignore_unknown_fields=True)
        except (ParseError, TypeError, ValueError):
            raise RemoteA2AError("remote_a2a_invalid_card") from None
        if not parsed.name or not any(
            _same_endpoint(interface.url, self.rpc_url)
            and interface.protocol_binding == "JSONRPC"
            and interface.protocol_version == "1.0"
            for interface in parsed.supported_interfaces
        ):
            raise RemoteA2AError("remote_a2a_configured_interface_not_supported")
        # Discovery is a remote declaration. It cannot grant Hive authority or
        # attest action interception, cancellation or complete cost metering.
        return card

    async def rpc(self, method: str, params: dict, *, request_id: str | None = None) -> dict:
        from a2a.types import a2a_pb2 as protocol
        from google.protobuf.json_format import ParseDict, ParseError

        schemas = {
            "SendMessage": (protocol.SendMessageRequest, protocol.SendMessageResponse),
            "GetTask": (protocol.GetTaskRequest, protocol.Task),
            "CancelTask": (protocol.CancelTaskRequest, protocol.Task),
        }
        if method not in schemas:
            raise RemoteA2AError("remote_a2a_method_not_supported")
        if self.redact_payload(params) != params:
            raise RemoteA2AError("remote_a2a_credential_in_request")
        request_schema, response_schema = schemas[method]
        try:
            parsed_request = ParseDict(params, request_schema(), ignore_unknown_fields=False)
            if method == "SendMessage":
                message = parsed_request.message
                if not message.message_id or not message.role or not message.parts:
                    raise ValueError("Message identity, role and parts are required")
            elif not parsed_request.id:
                raise ValueError("Task identity is required")
            # Confirm the body is JSON before entering the external effect.
            json.dumps(params, allow_nan=False)
        except (ParseError, TypeError, ValueError):
            raise RemoteA2AError("remote_a2a_invalid_request") from None
        nonce = request_id or str(uuid4())
        response = await self._request(
            "POST", self.rpc_url, payload={"jsonrpc": "2.0", "id": nonce, "method": method, "params": params}
        )
        if (
            response.get("jsonrpc") != "2.0"
            or response.get("id") != nonce
            or (("result" in response) == ("error" in response))
        ):
            raise RemoteA2AError(
                "remote_a2a_response_identity_mismatch", execution_status="unknown", observation=response
            )
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise RemoteA2AError(
                "remote_a2a_reported_error",
                execution_status="unknown",
                remote_code=code if type(code) is int else None,
                observation=response,
            )
        result = response["result"]
        try:
            parsed = ParseDict(result, response_schema(), ignore_unknown_fields=True)
            if method == "SendMessage":
                kind = parsed.WhichOneof("payload")
                if (
                    kind is None
                    or (kind == "task" and not parsed.task.id)
                    or (kind == "message" and not parsed.message.message_id)
                ):
                    raise ValueError("Response payload is missing")
                if (
                    kind == "task"
                    and parsed_request.message.task_id
                    and parsed.task.id != parsed_request.message.task_id
                ):
                    raise RemoteA2AError(
                        "remote_a2a_task_identity_mismatch", execution_status="unknown", observation=response
                    )
                if (
                    parsed_request.message.context_id
                    and getattr(parsed, kind).context_id != parsed_request.message.context_id
                ):
                    raise RemoteA2AError(
                        "remote_a2a_context_identity_mismatch", execution_status="unknown", observation=response
                    )
            elif not parsed.id:
                raise ValueError("Task identity is missing")
            elif parsed.id != parsed_request.id:
                raise RemoteA2AError(
                    "remote_a2a_task_identity_mismatch", execution_status="unknown", observation=response
                )
        except (ParseError, TypeError, ValueError):
            raise RemoteA2AError(
                "remote_a2a_invalid_result", execution_status="unknown", observation=response
            ) from None
        return result
