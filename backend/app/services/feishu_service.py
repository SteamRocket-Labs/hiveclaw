"""Feishu (Lark) OAuth and API integration service."""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import create_access_token, hash_password
from app.models.user import User

settings = get_settings()

FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
FEISHU_SEND_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"


class FeishuService:
    """Service for Feishu OAuth login and message API."""

    def __init__(self):
        self.app_id = settings.FEISHU_APP_ID
        self.app_secret = settings.FEISHU_APP_SECRET
        self._app_access_token: str | None = None

    async def get_app_access_token(self) -> str:
        """Get or refresh the app-level access token."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            })
            data = resp.json()
            self._app_access_token = data.get("app_access_token", "")
            return self._app_access_token

    async def exchange_code_for_user(self, code: str) -> dict:
        """Exchange OAuth authorization code for user info.

        Returns dict with: open_id, union_id, user_id, name, email, avatar_url
        """
        app_token = await self.get_app_access_token()

        async with httpx.AsyncClient() as client:
            # Get user access token
            token_resp = await client.post(FEISHU_TOKEN_URL, json={
                "grant_type": "authorization_code",
                "code": code,
            }, headers={"Authorization": f"Bearer {app_token}"})
            token_data = token_resp.json()
            user_access_token = token_data.get("data", {}).get("access_token", "")

            # Get user info
            info_resp = await client.get(FEISHU_USER_INFO_URL, headers={
                "Authorization": f"Bearer {user_access_token}",
            })
            info_data = info_resp.json().get("data", {})

            return {
                "open_id": info_data.get("open_id"),
                "union_id": info_data.get("union_id"),
                "user_id": info_data.get("user_id"),
                "name": info_data.get("name", ""),
                "email": info_data.get("email", ""),
                "avatar_url": info_data.get("avatar_url", ""),
            }

    async def login_or_register(self, db: AsyncSession, feishu_user: dict) -> tuple[User, str]:
        """Login existing user or register new one via Feishu SSO.

        Returns (user, jwt_token)
        """
        open_id = feishu_user["open_id"]
        user_id = feishu_user.get("user_id", "")

        # Prefer user_id (tenant-stable) for user lookup
        user = None
        if user_id:
            result = await db.execute(select(User).where(User.feishu_user_id == user_id))
            user = result.scalar_one_or_none()
        if not user and open_id:
            result = await db.execute(select(User).where(User.feishu_open_id == open_id))
            user = result.scalar_one_or_none()

        if user:
            # Existing user — update info
            user.avatar_url = feishu_user.get("avatar_url") or user.avatar_url
            # Always update IDs to latest values
            if open_id:
                user.feishu_open_id = open_id
            if user_id:
                user.feishu_user_id = user_id
            token = create_access_token(str(user.id), user.role, tenant_id=str(user.tenant_id) if user.tenant_id else None)
            return user, token

        # New user — create account
        username = feishu_user.get("email", "").split("@")[0] or f"feishu_{open_id[:8]}"
        email = feishu_user.get("email") or f"{username}@feishu.local"

        # Ensure unique username
        existing = await db.execute(select(User).where(User.username == username))
        if existing.scalar_one_or_none():
            username = f"{username}_{open_id[:6]}"

        user = User(
            username=username,
            email=email,
            password_hash=hash_password(open_id),  # placeholder password
            display_name=feishu_user.get("name", username),
            avatar_url=feishu_user.get("avatar_url"),
            feishu_open_id=open_id,
            feishu_union_id=feishu_user.get("union_id"),
            feishu_user_id=feishu_user.get("user_id"),
        )
        db.add(user)
        await db.flush()

        token = create_access_token(str(user.id), user.role)
        return user, token

    async def bind_feishu(self, db: AsyncSession, user: User, code: str) -> User:
        """Bind Feishu account to existing user."""
        feishu_user = await self.exchange_code_for_user(code)
        user.feishu_open_id = feishu_user["open_id"]
        user.feishu_union_id = feishu_user.get("union_id")
        user.feishu_user_id = feishu_user.get("user_id")
        await db.flush()
        return user

    async def send_message(self, app_id: str, app_secret: str, receive_id: str,
                           msg_type: str, content: str, receive_id_type: str = "open_id") -> dict:
        """Send a message via a specific Feishu bot (per-agent credentials).

        Args:
            app_id: The Feishu app's App ID (per-agent)
            app_secret: The Feishu app's App Secret (per-agent)
            receive_id: Target user's open_id
            msg_type: "text", "interactive", etc.
            content: JSON string of message content
            receive_id_type: "open_id" or "chat_id"
        """
        # Get app access token for this specific agent's bot
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id,
                "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")

            resp = await client.post(
                f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
                json={
                    "receive_id": receive_id,
                    "msg_type": msg_type,
                    "content": content,
                },
                headers={"Authorization": f"Bearer {app_token}"},
            )
            return resp.json()

    async def patch_message(self, app_id: str, app_secret: str, message_id: str, content: str) -> dict:
        """Patch an existing message (e.g. updating an interactive card for streaming)."""
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id,
                "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")

            resp = await client.patch(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
                json={
                    "content": content,
                },
                headers={"Authorization": f"Bearer {app_token}"},
            )
            return resp.json()

    async def resolve_open_id(self, app_id: str, app_secret: str,
                               email: str | None = None, mobile: str | None = None) -> str | None:
        """Resolve a user's open_id for a specific app using email or mobile.

        Each Feishu app gets a unique open_id per user. This method looks up the
        correct open_id for the given app's credentials.
        """
        if not email and not mobile:
            return None

        async with httpx.AsyncClient() as client:
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id,
                "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")

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
            data = resp.json()
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
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id,
                "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")

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
            data = resp.json()
            if data.get("code") != 0:
                return None

            user_list = data.get("data", {}).get("user_list", [])
            for u in user_list:
                uid = u.get("user_id")
                if uid:
                    return uid
            return None

    async def send_approval_card(self, app_id: str, app_secret: str,
                                  creator_open_id: str, agent_name: str,
                                  action_type: str, details: str, approval_id: str,
                                  callback_url: str = "") -> dict:
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
            app_id, app_secret, creator_open_id, "interactive", content,
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
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id,
                "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")
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
            # Get token
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id, "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")
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
            upload_data = upload_resp.json()
            if upload_data.get("code") != 0:
                raise RuntimeError(f"Feishu file upload failed: {upload_data.get('msg')}")
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
            return resp.json()


feishu_service = FeishuService()
