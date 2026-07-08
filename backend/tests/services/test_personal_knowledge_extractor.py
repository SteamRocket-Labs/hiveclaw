from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services.personal_knowledge_extractor import (
    PersonalKnowledgeExtractionError,
    PersonalKnowledgeExtractionUnavailable,
    PersonalKnowledgeLLMExtractor,
    parse_extraction_payload,
)


class _FakeLLMClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.complete_calls: list[dict] = []
        self.closed = False

    async def complete(self, **kwargs):
        self.complete_calls.append(kwargs)
        return SimpleNamespace(content=self.content)

    async def close(self) -> None:
        self.closed = True


def test_parse_extraction_payload_cleans_schema_and_drops_invalid_items() -> None:
    result = parse_extraction_payload(
        {
            "entities": [
                {
                    "name": "  Open Notebook  ",
                    "entity_type": "project",
                    "aliases": ["OpenNotebook", "open notebook", "", "Open Notebook"],
                    "description": " Source-grounded notebook workflow. ",
                    "confidence": 1.7,
                },
                {"canonical_name": ""},
                "bad",
            ],
            "assertions": [
                {"subject_text": "Open Notebook", "predicate": "supports", "object_text": "source refs", "confidence": -1},
                {"subject": "", "predicate": "missing", "object": "drop"},
            ],
            "links": [
                {
                    "from_name": "Open Notebook",
                    "from_type": "project",
                    "to_name": "Personal KB",
                    "to_type": "system",
                    "relation": "informs",
                    "confidence": "0.61",
                },
                {"from": "Open Notebook", "to": "", "relation": "drop"},
            ],
            "warnings": ["  keep source refs  ", ""],
        }
    )

    assert len(result.entities) == 1
    assert result.entities[0].canonical_name == "Open Notebook"
    assert result.entities[0].entity_type == "project"
    assert result.entities[0].aliases == ("OpenNotebook",)
    assert result.entities[0].confidence == 1.0
    assert len(result.assertions) == 1
    assert result.assertions[0].confidence == 0.0
    assert len(result.links) == 1
    assert result.links[0].from_type == "project"
    assert result.links[0].to_type == "system"
    assert result.links[0].confidence == pytest.approx(0.61)
    assert result.warnings == ("keep source refs",)


@pytest.mark.asyncio
async def test_llm_extractor_parses_fenced_json_and_closes_client(monkeypatch) -> None:
    import app.services.llm_client as llm_client_module

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    segment = SimpleNamespace(
        id=uuid.uuid4(),
        heading_path_json=["Knowledge", "Imports"],
        content="Open Notebook imports should preserve source references.",
    )
    document = SimpleNamespace(id=uuid.uuid4(), title="Notebook memo", source_kind="paste")
    response_payload = {
        "entities": [{"canonical_name": "Open Notebook", "type": "project", "aliases": ["OpenNotebook"]}],
        "assertions": [{"subject": "Open Notebook", "predicate": "preserves", "object": "source references"}],
        "links": [{"from": "Open Notebook", "to": "Personal KB", "relation": "informs"}],
        "warnings": [],
    }
    fake_client = _FakeLLMClient(f"```json\n{json.dumps(response_payload)}\n```")
    created_configs: list[dict] = []

    async def resolve_model_config(resolved_tenant_id):
        assert resolved_tenant_id == tenant_id
        return {"provider": "fake", "model": "extractor-test"}

    def fake_with_usage(config, **kwargs):
        created_configs.append({"config": config, "usage": kwargs})
        return {"wrapped": config, "usage": kwargs}

    monkeypatch.setattr(llm_client_module, "with_llm_usage_context", fake_with_usage)
    monkeypatch.setattr(llm_client_module, "create_llm_client_from_config", lambda _config: fake_client)

    result = await PersonalKnowledgeLLMExtractor(model_config_resolver=resolve_model_config).extract_segment(
        segment=segment,
        document=document,
        source_ref={"segment_id": str(segment.id), "document_id": str(document.id)},
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        sensitivity="internal",
    )

    assert fake_client.closed is True
    assert fake_client.complete_calls[0]["temperature"] == 0.0
    assert fake_client.complete_calls[0]["max_tokens"] == 4096
    user_payload = json.loads(fake_client.complete_calls[0]["messages"][1].content)
    assert user_payload["heading_path"] == ["Knowledge", "Imports"]
    assert user_payload["content"] == "Open Notebook imports should preserve source references."
    assert created_configs[0]["usage"]["source"] == "personal_knowledge_extractor"
    assert created_configs[0]["usage"]["tenant_id"] == tenant_id
    assert created_configs[0]["usage"]["user_id"] == owner_id
    assert result.entities[0].canonical_name == "Open Notebook"
    assert result.assertions[0].predicate == "preserves"
    assert result.links[0].relation == "informs"


@pytest.mark.asyncio
async def test_llm_extractor_skips_sensitive_without_resolving_model() -> None:
    async def unexpected_resolver(_tenant_id):  # pragma: no cover - must not run
        raise AssertionError("sensitive extraction must not resolve a model")

    with pytest.raises(PersonalKnowledgeExtractionUnavailable, match="knowledge_extraction_skipped_sensitive"):
        await PersonalKnowledgeLLMExtractor(model_config_resolver=unexpected_resolver).extract_segment(
            segment=SimpleNamespace(id=uuid.uuid4(), heading_path_json=[], content="secret"),
            document=SimpleNamespace(id=uuid.uuid4(), title="Secret", source_kind="paste"),
            source_ref={},
            tenant_id=uuid.uuid4(),
            owner_user_id=uuid.uuid4(),
            sensitivity="PL4",
        )


@pytest.mark.asyncio
async def test_llm_extractor_raises_for_empty_and_invalid_json(monkeypatch) -> None:
    import app.services.llm_client as llm_client_module

    async def resolve_model_config(_tenant_id):
        return {"provider": "fake", "model": "extractor-test"}

    monkeypatch.setattr(llm_client_module, "with_llm_usage_context", lambda config, **_kwargs: config)

    async def run_with_response(content: str) -> None:
        fake_client = _FakeLLMClient(content)
        monkeypatch.setattr(llm_client_module, "create_llm_client_from_config", lambda _config: fake_client)
        await PersonalKnowledgeLLMExtractor(model_config_resolver=resolve_model_config).extract_segment(
            segment=SimpleNamespace(id=uuid.uuid4(), heading_path_json=[], content="source text"),
            document=SimpleNamespace(id=uuid.uuid4(), title="Doc", source_kind="paste"),
            source_ref={},
            tenant_id=uuid.uuid4(),
            owner_user_id=uuid.uuid4(),
            sensitivity="internal",
        )

    with pytest.raises(PersonalKnowledgeExtractionError, match="empty extraction"):
        await run_with_response("")
    with pytest.raises(PersonalKnowledgeExtractionError, match="not valid JSON|no JSON object"):
        await run_with_response("not-json")
