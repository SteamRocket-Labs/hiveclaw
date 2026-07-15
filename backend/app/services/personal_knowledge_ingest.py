"""Source normalization, canonical paths, segmentation, and extraction accounting."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
WHITESPACE_RE = re.compile(r"\s+")
SUPPORTED_IMPORT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".csv",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
}
AUDIO_IMPORT_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg"}
VIDEO_IMPORT_EXTENSIONS = {".mp4", ".mov", ".webm"}
IMAGE_IMPORT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MEDIA_IMPORT_EXTENSIONS = AUDIO_IMPORT_EXTENSIONS | VIDEO_IMPORT_EXTENSIONS | IMAGE_IMPORT_EXTENSIONS
DEFAULT_EXTRACT_MAX_CONCURRENCY_PER_TENANT = 4
EXTRACTION_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_miss_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cached_tokens",
    "prompt_cache_hit_tokens",
)

_EXTRACT_SEMAPHORES: dict[uuid.UUID, asyncio.Semaphore] = {}


@dataclass(frozen=True)
class KnowledgeSegmentDraft:
    position: int
    heading_path: list[str]
    content: str
    segment_hash: str
    token_count: int


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_source_sha256(value: str) -> str:
    clean = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", clean):
        raise ValueError("source_sha256 must be a 64-character lowercase hex digest")
    return clean


def normalize_markdown(markdown: str) -> str:
    clean = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return f"{clean}\n" if clean else ""


def clean_title(title: str) -> str:
    clean = WHITESPACE_RE.sub(" ", str(title or "").strip())
    return clean or "Untitled knowledge document"


def rough_token_count(text: str) -> int:
    return max(1, len(WHITESPACE_RE.findall(text)) + 1) if text.strip() else 0


def personal_knowledge_artifact_path(
    data_root: str | Path,
    owner_user_id: uuid.UUID,
    source_sha256: str,
) -> Path:
    """Return the canonical Markdown artifact path for one person-scope source."""

    source_hash = validate_source_sha256(source_sha256)
    return Path(data_root) / "persons" / str(owner_user_id) / "kb" / "documents" / source_hash[:2] / f"{source_hash}.md"


def personal_knowledge_root(data_root: str | Path, owner_user_id: uuid.UUID) -> Path:
    return Path(data_root) / "persons" / str(owner_user_id) / "kb"


def personal_knowledge_import_spool_path(
    data_root: str | Path,
    owner_user_id: uuid.UUID,
    source_sha256: str,
    filename: str,
) -> Path:
    """Return the durable payload spool path for an async personal import job."""

    source_hash = validate_source_sha256(source_sha256)
    return (
        Path(data_root) / "persons" / str(owner_user_id) / "kb" / "imports" / source_hash[:2] / safe_filename(filename)
    )


def safe_filename(filename: str) -> str:
    normalized = str(filename or "").replace("\\", "/")
    safe_name = Path(normalized).name.strip()
    if safe_name in {"", ".", ".."}:
        raise ValueError("filename is required")
    return safe_name


def extension_for_filename(filename: str) -> str:
    return Path(filename).suffix.lower()


def media_kind_for_extension(ext: str) -> str | None:
    if ext in AUDIO_IMPORT_EXTENSIONS:
        return "audio"
    if ext in VIDEO_IMPORT_EXTENSIONS:
        return "video"
    if ext in IMAGE_IMPORT_EXTENSIONS:
        return "image"
    return None


def title_from_url(url: str) -> str:
    path_name = Path(urlparse(url).path).name
    if path_name:
        return path_name
    return urlparse(url).netloc or "Imported URL"


def title_from_filename_or_uri(
    filename: str,
    source_uri: str | None,
    explicit_title: str | None = None,
) -> str:
    if explicit_title and str(explicit_title).strip():
        return clean_title(explicit_title)
    safe_name = safe_filename(filename)
    if safe_name:
        return clean_title(Path(safe_name).stem or safe_name)
    if source_uri:
        return clean_title(title_from_url(source_uri))
    return "Imported knowledge source"


def split_content(content: str, *, max_segment_chars: int, overlap_chars: int) -> list[str]:
    clean = content.strip()
    if not clean:
        return []
    if len(clean) <= max_segment_chars:
        return [clean]

    paragraphs = [part.strip() for part in re.split(r"\n{2,}", clean) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if not current:
            current = paragraph
            continue
        candidate = f"{current}\n\n{paragraph}"
        if len(candidate) <= max_segment_chars:
            current = candidate
            continue
        chunks.append(current)
        overlap = current[-overlap_chars:].strip() if overlap_chars > 0 else ""
        current = f"{overlap}\n\n{paragraph}".strip() if overlap else paragraph
    if current:
        chunks.append(current)

    split_chunks: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_segment_chars:
            split_chunks.append(chunk)
            continue
        start = 0
        while start < len(chunk):
            end = min(len(chunk), start + max_segment_chars)
            split_chunks.append(chunk[start:end].strip())
            if end == len(chunk):
                break
            start = max(0, end - overlap_chars)
    return [chunk for chunk in split_chunks if chunk]


def segment_markdown(
    markdown: str,
    *,
    max_segment_chars: int = 3600,
    overlap_chars: int = 400,
) -> list[KnowledgeSegmentDraft]:
    """Split Markdown into stable retrieval segments while preserving heading paths."""

    normalized = normalize_markdown(markdown)
    headings: list[str] = []
    section_lines: list[str] = []
    section_heading_path: list[str] = []
    drafts: list[KnowledgeSegmentDraft] = []

    def flush_section() -> None:
        content = "\n".join(section_lines).strip()
        if not content:
            return
        for chunk in split_content(content, max_segment_chars=max_segment_chars, overlap_chars=overlap_chars):
            position = len(drafts)
            heading_path = list(section_heading_path)
            segment_hash = sha256_text("\n".join([*heading_path, chunk]))
            drafts.append(
                KnowledgeSegmentDraft(
                    position=position,
                    heading_path=heading_path,
                    content=chunk,
                    segment_hash=segment_hash,
                    token_count=rough_token_count(chunk),
                )
            )

    for line in normalized.splitlines():
        match = HEADING_RE.match(line)
        if match:
            flush_section()
            section_lines = []
            level = len(match.group(1))
            title = clean_title(match.group(2))
            headings = headings[: level - 1]
            headings.append(title)
            section_heading_path = list(headings)
            continue
        section_lines.append(line)

    flush_section()
    if drafts:
        return drafts

    fallback = normalized.strip()
    if not fallback:
        return []
    return [
        KnowledgeSegmentDraft(
            position=0,
            heading_path=[],
            content=fallback,
            segment_hash=sha256_text(fallback),
            token_count=rough_token_count(fallback),
        )
    ]


def extract_semaphore_for_tenant(tenant_id: uuid.UUID) -> asyncio.Semaphore:
    limit = max(1, int(DEFAULT_EXTRACT_MAX_CONCURRENCY_PER_TENANT or 1))
    semaphore = _EXTRACT_SEMAPHORES.get(tenant_id)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        _EXTRACT_SEMAPHORES[tenant_id] = semaphore
    return semaphore


def new_extraction_usage_summary() -> dict[str, Any]:
    return {
        "segment_count": 0,
        "segments_with_usage": 0,
        "tokens": 0,
        "provider_usage": {},
    }


def usage_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def record_extraction_usage(summary: dict[str, Any], extraction: Any) -> None:
    summary["segment_count"] = int(summary.get("segment_count", 0) or 0) + 1
    usage = getattr(extraction, "usage", None)
    if isinstance(usage, dict):
        summary["segments_with_usage"] = int(summary.get("segments_with_usage", 0) or 0) + 1
        provider_usage = dict(summary.get("provider_usage") or {})
        for key in EXTRACTION_USAGE_KEYS:
            if key in usage:
                provider_usage[key] = usage_int(provider_usage.get(key)) + usage_int(usage.get(key))
        summary["provider_usage"] = provider_usage
    tokens = usage_int(getattr(extraction, "usage_tokens", None))
    if tokens:
        summary["tokens"] = int(summary.get("tokens", 0) or 0) + tokens


def finalize_extraction_usage(summary: dict[str, Any]) -> dict[str, Any] | None:
    segment_count = int(summary.get("segment_count", 0) or 0)
    if segment_count <= 0:
        return None
    segments_with_usage = int(summary.get("segments_with_usage", 0) or 0)
    provider_usage = dict(summary.get("provider_usage") or {})
    return {
        "segment_count": segment_count,
        "segments_with_usage": segments_with_usage,
        "usage_unavailable_count": max(segment_count - segments_with_usage, 0),
        "tokens": int(summary.get("tokens", 0) or 0),
        "provider_usage": provider_usage,
    }


_HEADING_RE = HEADING_RE
_WHITESPACE_RE = WHITESPACE_RE
_SUPPORTED_IMPORT_EXTENSIONS = SUPPORTED_IMPORT_EXTENSIONS
_AUDIO_IMPORT_EXTENSIONS = AUDIO_IMPORT_EXTENSIONS
_VIDEO_IMPORT_EXTENSIONS = VIDEO_IMPORT_EXTENSIONS
_IMAGE_IMPORT_EXTENSIONS = IMAGE_IMPORT_EXTENSIONS
_MEDIA_IMPORT_EXTENSIONS = MEDIA_IMPORT_EXTENSIONS
_DEFAULT_EXTRACT_MAX_CONCURRENCY_PER_TENANT = DEFAULT_EXTRACT_MAX_CONCURRENCY_PER_TENANT
_EXTRACTION_USAGE_KEYS = EXTRACTION_USAGE_KEYS
_sha256 = sha256_text
_sha256_bytes = sha256_bytes
_validate_source_sha256 = validate_source_sha256
_normalize_markdown = normalize_markdown
_clean_title = clean_title
_rough_token_count = rough_token_count
_personal_knowledge_root = personal_knowledge_root
_safe_filename = safe_filename
_extension_for_filename = extension_for_filename
_media_kind_for_extension = media_kind_for_extension
_title_from_url = title_from_url
_split_content = split_content
_extract_semaphore_for_tenant = extract_semaphore_for_tenant
_new_extraction_usage_summary = new_extraction_usage_summary
_usage_int = usage_int
_record_extraction_usage = record_extraction_usage
_finalize_extraction_usage = finalize_extraction_usage
