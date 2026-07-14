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
    def __init__(self, content: str, usage: dict | None = None) -> None:
        self.content = content
        self.usage = usage
        self.complete_calls: list[dict] = []
        self.closed = False

    async def complete(self, **kwargs):
        self.complete_calls.append(kwargs)
        return SimpleNamespace(content=self.content, usage=self.usage, model="fake-extractor-model")

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
                {
                    "subject_text": "Open Notebook",
                    "predicate": "supports",
                    "object_text": "source refs",
                    "confidence": -1,
                },
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


def test_parse_extraction_payload_preserves_every_model_authored_fact() -> None:
    payload = {
        "entities": [{"canonical_name": f"Entity {index}", "type": "topic"} for index in range(45)],
        "assertions": [
            {"subject": f"Entity {index}", "predicate": "relates_to", "object": f"Fact {index}"} for index in range(45)
        ],
        "links": [
            {"from": f"Entity {index % 45}", "to": f"Entity {(index + 1) % 45}", "relation": "links"}
            for index in range(65)
        ],
        "warnings": [],
    }

    result = parse_extraction_payload(payload)

    assert len(result.entities) == 45
    assert len(result.assertions) == 45
    assert len(result.links) == 65
    assert result.entities[-1].canonical_name == "Entity 44"
    assert result.assertions[-1].object_text == "Fact 44"
    assert result.links[-1].from_name == "Entity 19"


def test_parse_extraction_payload_preserves_unbounded_semantic_text_fields() -> None:
    long_description = "description-tail-" + ("d" * 2_000)
    long_object = "object-tail-" + ("o" * 4_000)
    long_warning = "warning-tail-" + ("w" * 900)

    result = parse_extraction_payload(
        {
            "entities": [
                {
                    "canonical_name": "Complete Entity",
                    "type": "topic",
                    "description": long_description,
                }
            ],
            "assertions": [
                {
                    "subject": "Complete Entity",
                    "predicate": "records",
                    "object": long_object,
                }
            ],
            "links": [],
            "warnings": [long_warning],
        }
    )

    assert result.entities[0].description == long_description
    assert result.assertions[0].object_text == long_object
    assert result.warnings == (long_warning,)


@pytest.mark.parametrize(
    ("payload", "field_name"),
    [
        ({"entities": [{"canonical_name": "n" * 301}]}, "canonical_name"),
        (
            {"assertions": [{"subject": "subject", "predicate": "p" * 121, "object": "object"}]},
            "predicate",
        ),
    ],
)
def test_parse_extraction_payload_rejects_structural_overflow_instead_of_truncating(
    payload: dict,
    field_name: str,
) -> None:
    with pytest.raises(PersonalKnowledgeExtractionError, match=field_name):
        parse_extraction_payload(payload)


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
        return {"provider": "fake", "model": "extractor-test", "max_output_tokens": 32768}

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
    assert fake_client.complete_calls[0]["max_tokens"] == 32768
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
async def test_llm_extractor_returns_usage_tokens_for_job_accounting(monkeypatch) -> None:
    import app.services.llm_client as llm_client_module

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    response_payload = {
        "entities": [{"canonical_name": "Open Notebook", "type": "project"}],
        "assertions": [],
        "links": [],
        "warnings": [],
    }
    fake_client = _FakeLLMClient(
        json.dumps(response_payload),
        usage={"input_tokens": 125, "output_tokens": 37, "cache_read_input_tokens": 50},
    )

    async def resolve_model_config(_tenant_id):
        return {"provider": "fake", "model": "extractor-test"}

    monkeypatch.setattr(llm_client_module, "with_llm_usage_context", lambda config, **_kwargs: config)
    monkeypatch.setattr(llm_client_module, "create_llm_client_from_config", lambda _config: fake_client)

    result = await PersonalKnowledgeLLMExtractor(model_config_resolver=resolve_model_config).extract_segment(
        segment=SimpleNamespace(id=uuid.uuid4(), heading_path_json=[], content="Open Notebook source text."),
        document=SimpleNamespace(id=uuid.uuid4(), title="Doc", source_kind="paste"),
        source_ref={},
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        sensitivity="internal",
    )

    assert result.usage == {"input_tokens": 125, "output_tokens": 37, "cache_read_input_tokens": 50}
    assert result.usage_tokens == 162


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
