"""Tests for the empty optional memory enhancement hook."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.memory.retriever import MemoryRetriever


TENANT = uuid.UUID("aaaaaaaa-1111-2222-3333-444444444444")
AGENT = uuid.UUID("bbbbbbbb-5555-6666-7777-888888888888")


@pytest.mark.asyncio
async def test_returns_empty_without_query(tmp_path: Path) -> None:
    retriever = MemoryRetriever(data_root=tmp_path)
    assert await retriever._retrieve_semantic_backend(AGENT, "", str(TENANT)) == []


@pytest.mark.asyncio
async def test_returns_empty_without_tenant(tmp_path: Path) -> None:
    retriever = MemoryRetriever(data_root=tmp_path)
    assert await retriever._retrieve_semantic_backend(AGENT, "alice", None) == []


@pytest.mark.asyncio
async def test_returns_empty_with_valid_query_and_tenant(tmp_path: Path) -> None:
    retriever = MemoryRetriever(data_root=tmp_path)
    items = await retriever._retrieve_semantic_backend(AGENT, "what does alice do?", str(TENANT))
    assert items == []
