"""Feishu API and CardKit integration service."""

from __future__ import annotations

import json
from collections import OrderedDict

import httpx

from app.config import get_settings

try:
    import lark_oapi as lark

    _HAS_LARK = True
except ImportError:
    lark = None  # type: ignore
    _HAS_LARK = False

settings = get_settings()

FEISHU_TOKEN_URL_V2 = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
FEISHU_SEND_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"


class FeishuService:
    """Service for Feishu message send/patch/resource/CardKit operations."""

    _LARK_CLIENT_CACHE_MAX = 50

    def __init__(self):
        self.app_id = settings.FEISHU_APP_ID
        self.app_secret = settings.FEISHU_APP_SECRET
        self._app_access_token: str | None = None
        self._lark_clients: OrderedDict[str, object] = OrderedDict()

    @staticmethod
    def _parse_api_response(
        resp: httpx.Response,
        *,
        stage: str,
        message_id: str | None = None,
    ) -> dict:
        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Feishu {stage} returned invalid JSON") from exc

        if resp.status_code >= 400:
            raise RuntimeError(f"Feishu {stage} HTTP {resp.status_code}")

        if data.get("code") not in (None, 0):
            raise RuntimeError(
                f"Feishu {stage} failed: code={data.get('code')}, msg={data.get('msg', '')}, message_id={message_id}"
            )

        return data

    async def get_app_access_token(self) -> str:
        """Backward-compatible wrapper around tenant/app token fetch."""
        return await self.get_tenant_access_token(self.app_id, self.app_secret)

    async def get_tenant_access_token(self, app_id: str | None = None, app_secret: str | None = None) -> str:
        """Get tenant_access_token, falling back to app_access_token when needed."""
        target_app_id = app_id or self.app_id
        target_app_secret = app_secret or self.app_secret
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                FEISHU_APP_TOKEN_URL,
                json={"app_id": target_app_id, "app_secret": target_app_secret},
            )
            data = self._parse_api_response(resp, stage="get_tenant_access_token")
        token = data.get("tenant_access_token") or data.get("app_access_token", "")
        if app_id is None:
            self._app_access_token = token
        return token

    async def exchange_code_for_user(self, code: str) -> dict:
        """Exchange OAuth authorization code for user info.

        Returns dict with: open_id, union_id, user_id, name, email, avatar_url
        """
        async with httpx.AsyncClient() as client:
            # OAuth v2: exchange code with client credentials directly
            token_resp = await client.post(FEISHU_TOKEN_URL_V2, json={
                "grant_type": "authorization_code",
                "client_id": settings.FEISHU_APP_ID,
                "client_secret": settings.FEISHU_APP_SECRET,
                "code": code,
                "redirect_uri": settings.FEISHU_REDIRECT_URI or "",
            })
            token_data = self._parse_api_response(token_resp, stage="exchange_code_for_token")
            # v2 returns access_token at top level
            user_access_token = token_data.get("access_token") or token_data.get("data", {}).get("access_token", "")

            # Get user info
            info_resp = await client.get(FEISHU_USER_INFO_URL, headers={
                "Authorization": f"Bearer {user_access_token}",
            })
            info_data = self._parse_api_response(info_resp, stage="exchange_code_for_user_info").get("data", {})

            return {
                "open_id": info_data.get("open_id"),
                "union_id": info_data.get("union_id"),
                "user_id": info_data.get("user_id"),
                "name": info_data.get("name", ""),
                "email": info_data.get("email", ""),
                "avatar_url": info_data.get("avatar_url", ""),
            }

    async def send_message(
        self,
        app_id: str,
        app_secret: str,
        receive_id: str,
        msg_type: str,
        content: str,
        receive_id_type: str = "open_id",
        *,
        stage: str = "send_message",
    ) -> dict:
        """Send a message via a specific Feishu bot (per-agent credentials).

        Args:
            app_id: The Feishu app's App ID (per-agent)
            app_secret: The Feishu app's App Secret (per-agent)
            receive_id: Target user's open_id
            msg_type: "text", "interactive", etc.
            content: JSON string of message content
            receive_id_type: "open_id" or "chat_id"
        """
        async with httpx.AsyncClient() as client:
            app_token = await self.get_tenant_access_token(app_id, app_secret)

            resp = await client.post(
                f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
                json={
                    "receive_id": receive_id,
                    "msg_type": msg_type,
                    "content": content,
                },
                headers={"Authorization": f"Bearer {app_token}"},
            )
            return self._parse_api_response(resp, stage=stage)

    async def patch_message(
        self,
        app_id: str,
        app_secret: str,
        message_id: str,
        content: str,
        *,
        stage: str = "patch_message",
    ) -> dict:
        """Patch an existing message (e.g. updating an interactive card for streaming)."""
        async with httpx.AsyncClient() as client:
            app_token = await self.get_tenant_access_token(app_id, app_secret)

            resp = await client.patch(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
                json={
                    "content": content,
                },
                headers={"Authorization": f"Bearer {app_token}"},
            )
            return self._parse_api_response(resp, stage=stage, message_id=message_id)

    async def resolve_open_id(self, app_id: str, app_secret: str,
                               email: str | None = None, mobile: str | None = None) -> str | None:
        """Resolve a user's open_id for a specific app using email or mobile.

        Each Feishu app gets a unique open_id per user. This method looks up the
        correct open_id for the given app's credentials.
        """
        if not email and not mobile:
            return None

        async with httpx.AsyncClient() as client:
            app_token = await self.get_tenant_access_token(app_id, app_secret)

            body: dict = {}
            if email:
                body["emails"] = [email]
            if mobile:
                body["mobiles"] = [mobile]

            resp = await client.post(
                "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
                json=body,
                headers={"Authorization": f"Bearer {app_token}"},
                params={"user_id_type": "open_id"},
            )
            data = self._parse_api_response(resp, stage="resolve_open_id")
            if data.get("code") != 0:
                return None

            user_list = data.get("data", {}).get("user_list", [])
            for u in user_list:
                oid = u.get("user_id")
                if oid:
                    return oid
            return None

    async def resolve_user_id(self, app_id: str, app_secret: str,
                               email: str | None = None, mobile: str | None = None) -> str | None:
        """Resolve a user's tenant-level user_id using email or mobile.

        Unlike open_id, user_id is stable across all apps within the same tenant.
        Requires contact:user.employee_id:readonly permission.
        """
        if not email and not mobile:
            return None

        async with httpx.AsyncClient() as client:
            app_token = await self.get_tenant_access_token(app_id, app_secret)

            body: dict = {}
            if email:
                body["emails"] = [email]
            if mobile:
                body["mobiles"] = [mobile]

            resp = await client.post(
                "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
                json=body,
                headers={"Authorization": f"Bearer {app_token}"},
                params={"user_id_type": "user_id"},
            )
            data = self._parse_api_response(resp, stage="resolve_user_id")
            if data.get("code") != 0:
                return None

            user_list = data.get("data", {}).get("user_list", [])
            for u in user_list:
                uid = u.get("user_id")
                if uid:
                    return uid
            return None

    async def get_contact_user_by_open_id(
        self,
        app_id: str,
        app_secret: str,
        open_id: str,
        *,
        stage: str = "get_contact_user_by_open_id",
    ) -> dict:
        """Resolve a Feishu contact record from app-scoped open_id."""
        if not open_id:
            return {}

        async with httpx.AsyncClient(timeout=15) as client:
            app_token = await self.get_tenant_access_token(app_id, app_secret)
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/contact/v3/users/{open_id}",
                params={"user_id_type": "open_id"},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            data = self._parse_api_response(resp, stage=stage)
        return data.get("data", {}).get("user", {}) or {}

    async def send_approval_card(
        self,
        app_id: str,
        app_secret: str,
        creator_receive_id: str,
        receive_id_type: str,
        agent_name: str,
        action_type: str,
        details: str,
        approval_id: str,
        callback_url: str = "",
    ) -> dict:
        """Send an interactive approval card with approve/reject buttons to the agent creator via Feishu.

        The card includes action buttons that POST back to the card-callback endpoint.
        """
        import json

        # Truncate details for display
        display_details = details[:300] + ("..." if len(details) > 300 else "")

        card_json = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "orange",
                "title": {"tag": "plain_text", "content": f"🔴 [{agent_name}] 请求审批"},
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**操作类型**\n{action_type}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**审批 ID**\n{approval_id[:8]}..."}},
                    ],
                },
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**详情**\n{display_details}"},
                },
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✅ 批准"},
                            "type": "primary",
                            "value": json.dumps({"action": "approve", "approval_id": approval_id}),
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "❌ 拒绝"},
                            "type": "danger",
                            "value": json.dumps({"action": "reject", "approval_id": approval_id}),
                        },
                    ],
                },
            ],
        }

        content = json.dumps(card_json)
        return await self.send_message(
            app_id,
            app_secret,
            creator_receive_id,
            "interactive",
            content,
            receive_id_type=receive_id_type,
        )

    async def download_message_resource(self, app_id: str, app_secret: str,
                                         message_id: str, file_key: str,
                                         resource_type: str = "file") -> bytes:
        """Download a file or image from a Feishu message.

        Args:
            resource_type: "file" or "image"
        Returns raw file bytes.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            app_token = await self.get_tenant_access_token(app_id, app_secret)
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
                params={"type": resource_type},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            resp.raise_for_status()
            return resp.content

    async def upload_and_send_file(self, app_id: str, app_secret: str,
                                    receive_id: str, file_path,
                                    receive_id_type: str = "open_id",
                                    accompany_msg: str = "") -> dict:
        """Upload a local file to Feishu and send it as a file message.

        Returns the send_message response dict.
        """
        import json as _json
        from pathlib import Path as _Path
        fp = _Path(file_path)
        async with httpx.AsyncClient(timeout=60) as client:
            app_token = await self.get_tenant_access_token(app_id, app_secret)
            headers = {"Authorization": f"Bearer {app_token}"}

            # Upload file
            with open(fp, "rb") as f:
                file_bytes = f.read()
            # Determine file type for Feishu upload
            ext = fp.suffix.lower()
            feishu_file_type = "stream"  # generic binary
            if ext in (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md"):
                feishu_file_type = "stream"
            upload_resp = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/files",
                files={"file": (fp.name, file_bytes, "application/octet-stream")},
                data={"file_type": feishu_file_type, "file_name": fp.name},
                headers=headers,
            )
            upload_data = self._parse_api_response(upload_resp, stage="upload_file")
            file_key = upload_data["data"]["file_key"]

            # Send text accompany message first if provided
            if accompany_msg:
                await client.post(
                    f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
                    json={"receive_id": receive_id, "msg_type": "text",
                          "content": _json.dumps({"text": accompany_msg})},
                    headers=headers,
                )

            # Send file message
            resp = await client.post(
                f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
                json={"receive_id": receive_id, "msg_type": "file",
                      "content": _json.dumps({"file_key": file_key})},
                headers=headers,
            )
            return self._parse_api_response(resp, stage="send_file")

    async def create_approval_instance(
        self,
        app_id: str,
        app_secret: str,
        approval_code: str,
        user_id: str,
        form_data: str,
    ) -> dict:
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/approval/v4/instances",
                json={
                    "approval_code": approval_code,
                    "open_id": user_id,
                    "form": form_data,
                },
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
        return self._parse_api_response(resp, stage="create_approval_instance").get("data", {})

    async def query_approval_instances(
        self,
        app_id: str,
        app_secret: str,
        approval_code: str,
        status: str | None = None,
    ) -> dict:
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        body: dict[str, str] = {"approval_code": approval_code}
        if status:
            body["instance_status"] = status
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/approval/v4/instances/query",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
        return self._parse_api_response(resp, stage="query_approval_instances").get("data", {})

    async def get_approval_instance(self, app_id: str, app_secret: str, instance_id: str) -> dict:
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/approval/v4/instances/{instance_id}",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
        return self._parse_api_response(resp, stage="get_approval_instance").get("data", {})

    def _get_lark_client(self, app_id: str, app_secret: str):
        if not _HAS_LARK:
            raise RuntimeError("lark-oapi package is not installed. Install with: pip install lark-oapi")
        cache_key = f"{app_id}:{app_secret}"
        client = self._lark_clients.get(cache_key)
        if client is None:
            if len(self._lark_clients) >= self._LARK_CLIENT_CACHE_MAX:
                self._lark_clients.popitem(last=False)
            client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
            self._lark_clients[cache_key] = client
        else:
            self._lark_clients.move_to_end(cache_key)
        return client

    async def create_card_entity(self, app_id: str, app_secret: str, card_dict: dict) -> str:
        from lark_oapi.api.cardkit.v1.model import CreateCardRequest, CreateCardRequestBody

        client = self._get_lark_client(app_id, app_secret)
        body = CreateCardRequestBody.builder().type("card_json").data(json.dumps(card_dict)).build()
        request = CreateCardRequest.builder().request_body(body).build()
        resp = await client.cardkit.v1.card.acreate(request)
        if not resp.success():
            raise RuntimeError(f"Feishu CardKit create_card_entity failed: code={resp.code}, msg={resp.msg}")
        return resp.data.card_id

    async def send_card_by_card_id(
        self,
        app_id: str,
        app_secret: str,
        receive_id: str,
        card_id: str,
        receive_id_type: str = "open_id",
    ) -> dict:
        content = json.dumps({"type": "card", "data": {"card_id": card_id}})
        return await self.send_message(
            app_id,
            app_secret,
            receive_id,
            "interactive",
            content,
            receive_id_type=receive_id_type,
            stage="send_card_by_card_id",
        )

    async def stream_card_content(
        self,
        app_id: str,
        app_secret: str,
        card_id: str,
        element_id: str,
        content: str,
        sequence: int,
    ) -> None:
        from lark_oapi.api.cardkit.v1.model import ContentCardElementRequest, ContentCardElementRequestBody

        client = self._get_lark_client(app_id, app_secret)
        body = ContentCardElementRequestBody.builder().content(content).sequence(sequence).build()
        request = ContentCardElementRequest.builder().card_id(card_id).element_id(element_id).request_body(body).build()
        resp = await client.cardkit.v1.card_element.acontent(request)
        if not resp.success():
            raise RuntimeError(f"Feishu CardKit stream_card_content failed: code={resp.code}, msg={resp.msg}")

    async def set_card_streaming_mode(
        self,
        app_id: str,
        app_secret: str,
        card_id: str,
        streaming_mode: int,
        sequence: int,
    ) -> None:
        from lark_oapi.api.cardkit.v1.model import SettingsCardRequest, SettingsCardRequestBody

        client = self._get_lark_client(app_id, app_secret)
        body = SettingsCardRequestBody.builder().settings(json.dumps({"streaming_mode": streaming_mode})).sequence(sequence).build()
        request = SettingsCardRequest.builder().card_id(card_id).request_body(body).build()
        resp = await client.cardkit.v1.card.asettings(request)
        if not resp.success():
            raise RuntimeError(f"Feishu CardKit set_card_streaming_mode failed: code={resp.code}, msg={resp.msg}")

    async def update_cardkit_card(
        self,
        app_id: str,
        app_secret: str,
        card_id: str,
        card_dict: dict,
        sequence: int,
    ) -> None:
        from lark_oapi.api.cardkit.v1.model import Card, UpdateCardRequest, UpdateCardRequestBody

        client = self._get_lark_client(app_id, app_secret)
        card = Card.builder().type("card_json").data(json.dumps(card_dict)).build()
        body = UpdateCardRequestBody.builder().card(card).sequence(sequence).build()
        request = UpdateCardRequest.builder().card_id(card_id).request_body(body).build()
        resp = await client.cardkit.v1.card.aupdate(request)
        if not resp.success():
            raise RuntimeError(f"Feishu CardKit update_cardkit_card failed: code={resp.code}, msg={resp.msg}")


feishu_service = FeishuService()
