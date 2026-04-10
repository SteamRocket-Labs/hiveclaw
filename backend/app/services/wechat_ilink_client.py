"""iLink API HTTP client for personal WeChat integration.

Wraps the iLink Bot API (https://ilinkai.weixin.qq.com) for:
- QR code login (get_bot_qrcode / get_qrcode_status)
- Message polling (getupdates)
- Message sending (sendmessage) — text + media (image/file/video)
- Media upload/download with AES-128-ECB encryption
- Typing indicators (sendtyping)
- Bot config (getconfig)

All calls use httpx.AsyncClient with per-method timeouts.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from loguru import logger

# ── Constants ────────────────────────────────────────────

DEFAULT_ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_BOT_TYPE = "3"

# Timeouts (seconds)
QR_FETCH_TIMEOUT = 10.0
QR_POLL_TIMEOUT = 35.0
MESSAGE_POLL_TIMEOUT = 40.0  # slightly over server's 35s long-poll
SEND_TIMEOUT = 15.0
CONFIG_TIMEOUT = 10.0

# Session
QR_LOGIN_TTL_SECONDS = 300  # 5 minutes
MAX_QR_REFRESH = 3
POLL_BACKOFF_SECONDS = 1.0
MAX_CONSECUTIVE_ERRORS = 3
ERROR_BACKOFF_SECONDS = 30.0
TEXT_MESSAGE_MAX_LEN = 4000

# iLink error codes
ERRCODE_SESSION_TIMEOUT = -14

# CDN
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
CDN_UPLOAD_TIMEOUT = 60.0
CDN_DOWNLOAD_TIMEOUT = 60.0
CDN_MAX_RETRIES = 3

# Media types for getuploadurl
MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_FILE = 3
MEDIA_TYPE_VOICE = 4

# Message item types
ITEM_TYPE_TEXT = 1
ITEM_TYPE_IMAGE = 2
ITEM_TYPE_VOICE = 3
ITEM_TYPE_FILE = 4
ITEM_TYPE_VIDEO = 5


# ── AES-128-ECB helpers ─────────────────────────────────

def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _pkcs7_unpad(data: bytes) -> bytes:
    pad_len = data[-1]
    return data[:-pad_len]


def aes_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt with AES-128-ECB + PKCS7 padding."""
    padded = _pkcs7_pad(plaintext)
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def aes_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt AES-128-ECB + strip PKCS7 padding."""
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    return _pkcs7_unpad(padded)


def aes_ecb_padded_size(plaintext_size: int) -> int:
    """Calculate ciphertext size after AES-ECB + PKCS7 padding."""
    return ((plaintext_size + 15) // 16) * 16


def parse_aes_key(aes_key_b64: str) -> bytes:
    """Parse iLink aes_key (base64) into raw 16-byte key.

    Two formats: base64(raw 16 bytes) or base64(32-char hex string).
    """
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            hex_str = decoded.decode("ascii")
            return bytes.fromhex(hex_str)
        except (UnicodeDecodeError, ValueError) as e:
            logger.debug(f"[iLink] aes_key 32-byte hex decode failed: {e}")
    raise ValueError(f"Invalid aes_key: decoded length {len(decoded)}, expected 16 or 32")


# ── Data classes ─────────────────────────────────────────

@dataclass
class QRCodeResult:
    """Result from get_bot_qrcode."""
    qrcode: str  # token for status polling
    qrcode_img_url: str  # URL to render in frontend


@dataclass
class QRStatusResult:
    """Result from get_qrcode_status."""
    status: str  # wait | scaned | confirmed | expired
    bot_token: str | None = None
    ilink_bot_id: str | None = None
    base_url: str | None = None
    ilink_user_id: str | None = None


@dataclass
class MediaRef:
    """Reference to an encrypted media file on WeChat CDN."""
    encrypt_query_param: str
    aes_key: str  # base64-encoded
    encrypt_type: int = 1


@dataclass
class UploadResult:
    """Result from CDN upload pipeline."""
    download_param: str  # x-encrypted-param from CDN response
    aes_key_hex: str  # hex-encoded AES key
    plaintext_size: int
    ciphertext_size: int


@dataclass
class InboundMessage:
    """A single inbound message from getupdates."""
    seq: int
    message_id: int
    from_user_id: str
    to_user_id: str
    session_id: str
    create_time_ms: int
    context_token: str
    message_type: int
    text: str = ""
    # Media fields (populated when item contains media)
    image_media: MediaRef | None = None
    voice_text: str = ""  # transcription from voice_item.text
    file_name: str = ""
    file_media: MediaRef | None = None
    video_media: MediaRef | None = None
    raw_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GetUpdatesResult:
    """Result from getupdates."""
    messages: list[InboundMessage]
    sync_buf: str  # base64-encoded state for next call
    poll_timeout_ms: int = 35000


@dataclass
class BotConfig:
    """Result from getconfig."""
    typing_ticket: str = ""


# ── Client ───────────────────────────────────────────────

class ILinkClient:
    """Async HTTP client for the iLink Bot API."""

    def __init__(self, base_url: str = DEFAULT_ILINK_BASE_URL):
        self._base_url = base_url.rstrip("/")

    def _auth_headers(self, bot_token: str) -> dict[str, str]:
        """Build authentication headers for authorized API calls."""
        # X-WECHAT-UIN is a random identifier per request
        uin = secrets.token_urlsafe(6)
        return {
            "Authorization": f"Bearer {bot_token}",
            "AuthorizationType": "ilink_bot_token",
            "Content-Type": "application/json",
            "X-WECHAT-UIN": uin,
        }

    # ── QR Login ─────────────────────────────────────────

    async def fetch_qr_code(self, bot_type: str = DEFAULT_BOT_TYPE) -> QRCodeResult:
        """Fetch a fresh QR code for WeChat scan login.

        GET /ilink/bot/get_bot_qrcode?bot_type=3
        """
        url = f"{self._base_url}/ilink/bot/get_bot_qrcode"
        params = {"bot_type": bot_type}

        async with httpx.AsyncClient(timeout=QR_FETCH_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        qrcode = data.get("qrcode", "")
        img_url = data.get("qrcode_img_content", "")
        if not qrcode:
            raise ValueError("iLink returned empty qrcode token")

        logger.info(f"[iLink] QR code fetched, qrcode={qrcode[:8]}...")
        return QRCodeResult(qrcode=qrcode, qrcode_img_url=img_url)

    async def poll_qr_status(self, qrcode: str) -> QRStatusResult:
        """Long-poll the QR scan status.

        GET /ilink/bot/get_qrcode_status?qrcode=xxx
        Server holds connection for ~35s if status is 'wait'.
        """
        url = f"{self._base_url}/ilink/bot/get_qrcode_status"
        params = {"qrcode": qrcode}
        headers = {"iLink-App-ClientVersion": "1"}

        try:
            async with httpx.AsyncClient(timeout=QR_POLL_TIMEOUT) as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            logger.debug("[iLink] QR poll timed out, returning wait")
            return QRStatusResult(status="wait")

        status = data.get("status", "wait")
        return QRStatusResult(
            status=status,
            bot_token=data.get("bot_token"),
            ilink_bot_id=data.get("ilink_bot_id"),
            base_url=data.get("baseurl"),
            ilink_user_id=data.get("ilink_user_id"),
        )

    # ── Message Polling ──────────────────────────────────

    async def get_updates(
        self,
        bot_token: str,
        sync_buf: str = "",
        channel_version: str = "hiveclaw-1.0",
    ) -> GetUpdatesResult:
        """Long-poll for new inbound messages.

        POST /ilink/bot/getupdates
        """
        url = f"{self._base_url}/ilink/bot/getupdates"
        headers = self._auth_headers(bot_token)
        body: dict[str, Any] = {
            "base_info": {"channel_version": channel_version},
        }
        if sync_buf:
            body["get_updates_buf"] = sync_buf

        try:
            async with httpx.AsyncClient(timeout=MESSAGE_POLL_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            logger.debug("[iLink] get_updates timed out, returning empty")
            return GetUpdatesResult(messages=[], sync_buf=sync_buf)

        ret = data.get("ret", 0)
        if ret != 0:
            errcode = data.get("errcode", ret)
            errmsg = data.get("errmsg", "unknown")
            logger.warning(f"[iLink] get_updates error: ret={ret} errcode={errcode} errmsg={errmsg}")
            if errcode == ERRCODE_SESSION_TIMEOUT:
                raise ILinkSessionExpiredError(f"iLink session timeout: errcode={errcode}")
            raise ILinkAPIError(f"get_updates failed: ret={ret} errcode={errcode} errmsg={errmsg}")

        messages = []
        for msg in data.get("msgs", []):
            text = ""
            voice_text = ""
            image_media = None
            file_media = None
            file_name = ""
            video_media = None

            for item in msg.get("item_list", []):
                item_type = item.get("type", 0)

                if item_type == ITEM_TYPE_TEXT and "text_item" in item:
                    text = item["text_item"].get("text", "")

                elif item_type == ITEM_TYPE_IMAGE and "image_item" in item:
                    media = item["image_item"].get("media", {})
                    if media.get("encrypt_query_param"):
                        image_media = MediaRef(
                            encrypt_query_param=media["encrypt_query_param"],
                            aes_key=media.get("aes_key", ""),
                            encrypt_type=media.get("encrypt_type", 1),
                        )

                elif item_type == ITEM_TYPE_VOICE and "voice_item" in item:
                    voice_text = item["voice_item"].get("text", "")
                    # Voice media available but transcription is primary use

                elif item_type == ITEM_TYPE_FILE and "file_item" in item:
                    fi = item["file_item"]
                    file_name = fi.get("file_name", "")
                    media = fi.get("media", {})
                    if media.get("encrypt_query_param"):
                        file_media = MediaRef(
                            encrypt_query_param=media["encrypt_query_param"],
                            aes_key=media.get("aes_key", ""),
                            encrypt_type=media.get("encrypt_type", 1),
                        )

                elif item_type == ITEM_TYPE_VIDEO and "video_item" in item:
                    media = item["video_item"].get("media", {})
                    if media.get("encrypt_query_param"):
                        video_media = MediaRef(
                            encrypt_query_param=media["encrypt_query_param"],
                            aes_key=media.get("aes_key", ""),
                            encrypt_type=media.get("encrypt_type", 1),
                        )

            messages.append(InboundMessage(
                seq=msg.get("seq", 0),
                message_id=msg.get("message_id", 0),
                from_user_id=msg.get("from_user_id", ""),
                to_user_id=msg.get("to_user_id", ""),
                session_id=msg.get("session_id", ""),
                create_time_ms=msg.get("create_time_ms", 0),
                context_token=msg.get("context_token", ""),
                message_type=msg.get("message_type", 0),
                text=text,
                voice_text=voice_text,
                image_media=image_media,
                file_name=file_name,
                file_media=file_media,
                video_media=video_media,
                raw_items=msg.get("item_list", []),
            ))

        new_buf = data.get("get_updates_buf", sync_buf)
        poll_timeout = data.get("longpolling_timeout_ms", 35000)

        if messages:
            logger.info(f"[iLink] get_updates: {len(messages)} message(s)")

        return GetUpdatesResult(
            messages=messages,
            sync_buf=new_buf,
            poll_timeout_ms=poll_timeout,
        )

    # ── Message Sending ──────────────────────────────────

    async def send_message(
        self,
        bot_token: str,
        to_user_id: str,
        context_token: str,
        text: str,
        channel_version: str = "hiveclaw-1.0",
    ) -> dict[str, Any]:
        """Send a text message to a WeChat user.

        POST /ilink/bot/sendmessage
        The context_token MUST be echoed from the inbound message.
        """
        url = f"{self._base_url}/ilink/bot/sendmessage"
        headers = self._auth_headers(bot_token)

        # Truncate text to max length
        if len(text) > TEXT_MESSAGE_MAX_LEN:
            text = text[:TEXT_MESSAGE_MAX_LEN]

        client_id = f"hiveclaw-{secrets.token_hex(8)}"
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [
                    {
                        "type": 1,
                        "text_item": {"text": text},
                    }
                ],
            },
            "base_info": {"channel_version": channel_version},
        }

        async with httpx.AsyncClient(timeout=SEND_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        ret = data.get("ret", 0)
        if ret != 0:
            logger.error(f"[iLink] sendmessage failed: ret={ret} data={data}")
            raise ILinkAPIError(f"sendmessage failed: ret={ret}")

        logger.debug(f"[iLink] Message sent to {to_user_id[:12]}...")
        return data

    # ── Typing Indicator ─────────────────────────────────

    async def send_typing(
        self,
        bot_token: str,
        ilink_user_id: str,
        typing_ticket: str,
        status: int = 1,
    ) -> None:
        """Send typing indicator.

        POST /ilink/bot/sendtyping
        status: 1=typing, 2=cancel
        """
        url = f"{self._base_url}/ilink/bot/sendtyping"
        headers = self._auth_headers(bot_token)
        body = {
            "ilink_user_id": ilink_user_id,
            "typing_ticket": typing_ticket,
            "status": status,
        }

        try:
            async with httpx.AsyncClient(timeout=CONFIG_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=body)
                resp.raise_for_status()
        except Exception as e:
            # Typing is best-effort, don't crash on failure
            logger.debug(f"[iLink] sendtyping failed (non-fatal): {e}")

    # ── Bot Config ───────────────────────────────────────

    async def get_config(
        self,
        bot_token: str,
        ilink_user_id: str,
    ) -> BotConfig:
        """Fetch bot config for a user (includes typing_ticket).

        POST /ilink/bot/getconfig
        """
        url = f"{self._base_url}/ilink/bot/getconfig"
        headers = self._auth_headers(bot_token)
        body = {"ilink_user_id": ilink_user_id}

        async with httpx.AsyncClient(timeout=CONFIG_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        return BotConfig(
            typing_ticket=data.get("typing_ticket", ""),
        )

    # ── Media Upload ─────────────────────────────────────

    async def upload_media(
        self,
        bot_token: str,
        to_user_id: str,
        file_data: bytes,
        media_type: int = MEDIA_TYPE_FILE,
        cdn_base_url: str = DEFAULT_CDN_BASE_URL,
        channel_version: str = "hiveclaw-1.0",
    ) -> UploadResult:
        """Upload a file to WeChat CDN with AES-128-ECB encryption.

        1. Generate random AES key + filekey
        2. Call getuploadurl to get pre-signed CDN URL
        3. Encrypt file with AES-128-ECB
        4. Upload ciphertext to CDN
        5. Return UploadResult with download param
        """
        # Generate per-file key and filekey
        aes_key_bytes = secrets.token_bytes(16)
        aes_key_hex = aes_key_bytes.hex()
        filekey = secrets.token_hex(16)

        # Calculate sizes
        raw_size = len(file_data)
        raw_md5 = hashlib.md5(file_data).hexdigest()
        cipher_size = aes_ecb_padded_size(raw_size)

        # Step 1: Get upload URL
        url = f"{self._base_url}/ilink/bot/getuploadurl"
        headers = self._auth_headers(bot_token)
        body = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": raw_size,
            "rawfilemd5": raw_md5,
            "filesize": cipher_size,
            "no_need_thumb": True,
            "aeskey": aes_key_hex,
            # Provide zero thumbs even if no_need_thumb (iLink may still expect these)
            "thumb_rawsize": 0,
            "thumb_rawfilemd5": "",
            "thumb_filesize": 0,
            "base_info": {"channel_version": channel_version},
        }

        logger.info(f"[iLink] getuploadurl request: media_type={media_type} rawsize={raw_size} cipher_size={cipher_size} to={to_user_id[:12]}...")

        async with httpx.AsyncClient(timeout=SEND_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        logger.info(f"[iLink] getuploadurl response: {data}")

        ret = data.get("ret", 0)
        if ret != 0:
            errcode = data.get("errcode", ret)
            errmsg = data.get("errmsg", "unknown")
            raise ILinkAPIError(f"getuploadurl failed: ret={ret} errcode={errcode} errmsg={errmsg}")

        upload_param = data.get("upload_param", "")
        if not upload_param:
            raise ILinkAPIError(f"getuploadurl returned empty upload_param, full response: {data}")

        # Step 2: Encrypt file
        ciphertext = aes_ecb_encrypt(file_data, aes_key_bytes)

        # Step 3: Upload to CDN
        cdn_url = f"{cdn_base_url}/upload?encrypted_query_param={quote(upload_param)}&filekey={quote(filekey)}"
        download_param = ""

        for attempt in range(CDN_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=CDN_UPLOAD_TIMEOUT) as client:
                    resp = await client.post(
                        cdn_url,
                        content=ciphertext,
                        headers={"Content-Type": "application/octet-stream"},
                    )
                    if resp.status_code >= 400 and resp.status_code < 500:
                        raise ILinkAPIError(f"CDN upload client error: {resp.status_code}")
                    resp.raise_for_status()
                    download_param = resp.headers.get("x-encrypted-param", "")
                    break
            except ILinkAPIError:
                raise
            except Exception as e:
                if attempt == CDN_MAX_RETRIES - 1:
                    raise ILinkAPIError(f"CDN upload failed after {CDN_MAX_RETRIES} retries: {e}")
                logger.warning(f"[iLink] CDN upload attempt {attempt + 1} failed: {e}")

        if not download_param:
            raise ILinkAPIError("CDN upload succeeded but no x-encrypted-param in response")

        logger.info(f"[iLink] Media uploaded: {raw_size}B → {cipher_size}B, type={media_type}")
        return UploadResult(
            download_param=download_param,
            aes_key_hex=aes_key_hex,
            plaintext_size=raw_size,
            ciphertext_size=cipher_size,
        )

    async def download_media(
        self,
        media_ref: MediaRef,
        cdn_base_url: str = DEFAULT_CDN_BASE_URL,
    ) -> bytes:
        """Download and decrypt a media file from WeChat CDN.

        1. Build download URL from encrypt_query_param
        2. Fetch ciphertext
        3. Parse aes_key and decrypt
        """
        cdn_url = f"{cdn_base_url}/download?encrypted_query_param={quote(media_ref.encrypt_query_param)}"

        async with httpx.AsyncClient(timeout=CDN_DOWNLOAD_TIMEOUT) as client:
            resp = await client.get(cdn_url)
            resp.raise_for_status()
            ciphertext = resp.content

        aes_key = parse_aes_key(media_ref.aes_key)
        plaintext = aes_ecb_decrypt(ciphertext, aes_key)

        logger.info(f"[iLink] Media downloaded: {len(ciphertext)}B → {len(plaintext)}B")
        return plaintext

    async def send_media_message(
        self,
        bot_token: str,
        to_user_id: str,
        context_token: str,
        upload: UploadResult,
        media_type: int = MEDIA_TYPE_FILE,
        file_name: str = "",
        channel_version: str = "hiveclaw-1.0",
    ) -> dict[str, Any]:
        """Send a media message (image/file/video) using a completed upload.

        POST /ilink/bot/sendmessage with media item_list.
        """
        url = f"{self._base_url}/ilink/bot/sendmessage"
        headers = self._auth_headers(bot_token)
        client_id = f"hiveclaw-{secrets.token_hex(8)}"

        # Build aes_key as base64(hex_string) for outbound
        aes_key_b64 = base64.b64encode(bytes.fromhex(upload.aes_key_hex)).decode()

        media_ref = {
            "encrypt_query_param": upload.download_param,
            "aes_key": aes_key_b64,
            "encrypt_type": 1,
        }

        # Build item based on media_type
        if media_type == MEDIA_TYPE_IMAGE:
            item = {
                "type": ITEM_TYPE_IMAGE,
                "image_item": {"media": media_ref, "mid_size": upload.ciphertext_size},
            }
        elif media_type == MEDIA_TYPE_VIDEO:
            item = {
                "type": ITEM_TYPE_VIDEO,
                "video_item": {"media": media_ref, "video_size": upload.ciphertext_size},
            }
        else:
            item = {
                "type": ITEM_TYPE_FILE,
                "file_item": {"media": media_ref, "file_name": file_name, "len": str(upload.plaintext_size)},
            }

        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [item],
            },
            "base_info": {"channel_version": channel_version},
        }

        async with httpx.AsyncClient(timeout=SEND_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        ret = data.get("ret", 0)
        if ret != 0:
            logger.error(f"[iLink] send_media_message failed: ret={ret}")
            raise ILinkAPIError(f"send_media_message failed: ret={ret}")

        logger.info(f"[iLink] Media message sent to {to_user_id[:12]}..., type={media_type}")
        return data


# ── Exceptions ───────────────────────────────────────────

class ILinkAPIError(Exception):
    """General iLink API error."""


class ILinkSessionExpiredError(ILinkAPIError):
    """iLink session/token expired (errcode=-14)."""
