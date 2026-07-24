"""Short-lived channel download tokens for agent workspace files."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from jose import JWTError, jwt

from app.config import get_settings

CHANNEL_FILE_DOWNLOAD_PURPOSE = "channel_file_download"
DEFAULT_CHANNEL_FILE_DOWNLOAD_TOKEN_EXPIRE_SECONDS = 24 * 60 * 60
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class NotChannelFileDownloadToken(Exception):
    """Raised when a JWT is valid but is not a channel file token."""


class InvalidChannelFileDownloadToken(Exception):
    """Raised when a channel file token is invalid for the requested file."""


class ChannelFileContentChanged(Exception):
    """Raised when a path no longer contains the bytes authorized by a token."""


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
    content_sha256: str | None = None,
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
    if content_sha256 is not None:
        digest = str(content_sha256).strip().casefold()
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        payload["content_sha256"] = digest
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
    content_sha256 = payload.get("content_sha256")
    if content_sha256 is not None and not _SHA256_RE.fullmatch(str(content_sha256)):
        raise InvalidChannelFileDownloadToken("Channel file token has an invalid content binding")
    return payload


def snapshot_channel_file_content(
    *,
    path: str | Path,
    payload: dict,
) -> tuple[TemporaryDirectory[str] | None, Path]:
    """Copy, verify, and return the exact bytes authorized by a channel token."""

    expected = payload.get("content_sha256")
    if expected is None:
        return None, Path(path)
    source_path = Path(path)
    snapshot_dir: TemporaryDirectory[str] = TemporaryDirectory(
        prefix="hive-channel-download-",
    )
    snapshot_path = Path(snapshot_dir.name) / source_path.name
    digest = hashlib.sha256()
    try:
        with source_path.open("rb") as source, snapshot_path.open("xb") as snapshot:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
                snapshot.write(chunk)
    except BaseException:
        snapshot_dir.cleanup()
        raise
    if not hmac.compare_digest(digest.hexdigest(), str(expected)):
        snapshot_dir.cleanup()
        raise ChannelFileContentChanged
    return snapshot_dir, snapshot_path


def build_channel_file_download_url(
    *,
    agent_id: uuid.UUID,
    path: str,
    content_sha256: str | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """Build an externally usable signed download URL for channel fallback delivery."""
    settings = get_settings()
    base_url = (getattr(settings, "PUBLIC_BASE_URL", "") or getattr(settings, "BASE_URL", "") or "").rstrip("/")
    if not base_url:
        raise ValueError("PUBLIC_BASE_URL or BASE_URL is required for channel file download links")

    token = make_channel_file_download_token(
        agent_id=agent_id,
        path=path,
        content_sha256=content_sha256,
        expires_delta=expires_delta,
    )
    query = urlencode({"path": path, "token": token})
    return f"{base_url}/api/agents/{agent_id}/files/download?{query}"
