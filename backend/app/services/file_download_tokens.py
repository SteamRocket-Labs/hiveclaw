"""Short-lived channel download tokens for agent workspace files."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from jose import JWTError, jwt

from app.config import get_settings

CHANNEL_FILE_DOWNLOAD_PURPOSE = "channel_file_download"
DEFAULT_CHANNEL_FILE_DOWNLOAD_TOKEN_EXPIRE_SECONDS = 24 * 60 * 60


class NotChannelFileDownloadToken(Exception):
    """Raised when a JWT is valid but is not a channel file token."""


class InvalidChannelFileDownloadToken(Exception):
    """Raised when a channel file token is invalid for the requested file."""


def _expiry(expires_delta: timedelta | None = None) -> datetime:
    settings = get_settings()
    if expires_delta is None:
        seconds = int(
            getattr(
                settings,
                "CHANNEL_FILE_DOWNLOAD_TOKEN_EXPIRE_SECONDS",
                DEFAULT_CHANNEL_FILE_DOWNLOAD_TOKEN_EXPIRE_SECONDS,
            )
        )
        expires_delta = timedelta(seconds=seconds)
    return datetime.now(UTC) + expires_delta


def make_channel_file_download_token(
    *,
    agent_id: uuid.UUID,
    path: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a scoped token that can download exactly one agent workspace file."""
    settings = get_settings()
    payload = {
        "purpose": CHANNEL_FILE_DOWNLOAD_PURPOSE,
        "agent_id": str(agent_id),
        "path": path,
        "exp": _expiry(expires_delta),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def verify_channel_file_download_token(
    *,
    token: str,
    agent_id: uuid.UUID,
    path: str,
) -> dict:
    """Validate that token grants access to exactly agent_id/path."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise InvalidChannelFileDownloadToken("Invalid or expired channel file token") from exc

    if payload.get("purpose") != CHANNEL_FILE_DOWNLOAD_PURPOSE:
        raise NotChannelFileDownloadToken
    if payload.get("agent_id") != str(agent_id) or payload.get("path") != path:
        raise InvalidChannelFileDownloadToken("Channel file token does not match requested file")
    return payload


def build_channel_file_download_url(
    *,
    agent_id: uuid.UUID,
    path: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Build an externally usable signed download URL for channel fallback delivery."""
    settings = get_settings()
    base_url = (getattr(settings, "PUBLIC_BASE_URL", "") or getattr(settings, "BASE_URL", "") or "").rstrip("/")
    if not base_url:
        raise ValueError("PUBLIC_BASE_URL or BASE_URL is required for channel file download links")

    token = make_channel_file_download_token(agent_id=agent_id, path=path, expires_delta=expires_delta)
    query = urlencode({"path": path, "token": token})
    return f"{base_url}/api/agents/{agent_id}/files/download?{query}"
