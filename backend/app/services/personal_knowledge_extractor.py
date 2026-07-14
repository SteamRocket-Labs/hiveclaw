"""LLM-backed extraction for Personal Knowledge Base graph projections."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


class PersonalKnowledgeExtractionError(RuntimeError):
    """Raised when a knowledge extraction attempt is unusable."""


class PersonalKnowledgeExtractionUnavailable(PersonalKnowledgeExtractionError):
    """Raised when extraction cannot run without leaking or without a model."""


@dataclass(frozen=True)
class KnowledgeExtractionEntity:
    canonical_name: str
    entity_type: str = "freeform"
    aliases: Sequence[str] = field(default_factory=tuple)
    description: str | None = None
    confidence: float = 1.0


@dataclass(frozen=True)
class KnowledgeExtractionAssertion:
    subject_text: str
    predicate: str
    object_text: str
    confidence: float = 1.0


@dataclass(frozen=True)
class KnowledgeExtractionLink:
    from_name: str
    to_name: str
    relation: str
    from_type: str = "freeform"
    to_type: str = "freeform"
    confidence: float = 1.0


@dataclass(frozen=True)
class KnowledgeExtractionResult:
    entities: Sequence[KnowledgeExtractionEntity] = field(default_factory=tuple)
    assertions: Sequence[KnowledgeExtractionAssertion] = field(default_factory=tuple)
    links: Sequence[KnowledgeExtractionLink] = field(default_factory=tuple)
    warnings: Sequence[str] = field(default_factory=tuple)
    usage: dict[str, Any] | None = None
    usage_tokens: int | None = None


def _strip_fences(content: str) -> str:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    return text


def _loads_json_object(content: str) -> dict[str, Any]:
    text = _strip_fences(content)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise PersonalKnowledgeExtractionError("model response contained no JSON object") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise PersonalKnowledgeExtractionError(f"model response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PersonalKnowledgeExtractionError("model response JSON must be an object")
    return parsed


def _clean_text(
    value: Any,
    *,
    max_len: int | None = 300,
    field_name: str = "text",
) -> str:
    clean = re.sub(r"\s+", " ", str(value or "").strip())
    if max_len is not None and len(clean) > max_len:
        raise PersonalKnowledgeExtractionError(f"{field_name} exceeds the structural limit of {max_len} characters")
    return clean


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, number))


def parse_extraction_payload(payload: dict[str, Any]) -> KnowledgeExtractionResult:
    """Parse the strict JSON object returned by the extractor LLM."""

    entities: list[KnowledgeExtractionEntity] = []
    for item in payload.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = _clean_text(
            item.get("canonical_name") or item.get("name"),
            field_name="canonical_name",
        )
        if not name:
            continue
        aliases = [
            alias
            for alias in (_clean_text(alias, field_name="alias") for alias in (item.get("aliases") or []))
            if alias and alias.lower() != name.lower()
        ]
        entities.append(
            KnowledgeExtractionEntity(
                canonical_name=name,
                entity_type=_clean_text(
                    item.get("type") or item.get("entity_type") or "freeform",
                    max_len=80,
                    field_name="entity_type",
                )
                or "freeform",
                aliases=tuple(dict.fromkeys(aliases)),
                description=_clean_text(
                    item.get("description"),
                    max_len=None,
                    field_name="description",
                )
                or None,
                confidence=_confidence(item.get("confidence")),
            )
        )

    assertions: list[KnowledgeExtractionAssertion] = []
    for item in payload.get("assertions") or []:
        if not isinstance(item, dict):
            continue
        subject = _clean_text(
            item.get("subject") or item.get("subject_text"),
            field_name="subject",
        )
        predicate = _clean_text(item.get("predicate"), max_len=120, field_name="predicate")
        obj = _clean_text(
            item.get("object") or item.get("object_text"),
            max_len=None,
            field_name="object",
        )
        if not subject or not predicate or not obj:
            continue
        assertions.append(
            KnowledgeExtractionAssertion(
                subject_text=subject,
                predicate=predicate,
                object_text=obj,
                confidence=_confidence(item.get("confidence")),
            )
        )

    links: list[KnowledgeExtractionLink] = []
    for item in payload.get("links") or []:
        if not isinstance(item, dict):
            continue
        from_name = _clean_text(
            item.get("from") or item.get("from_name"),
            field_name="from_name",
        )
        to_name = _clean_text(
            item.get("to") or item.get("to_name"),
            field_name="to_name",
        )
        relation = _clean_text(item.get("relation"), max_len=80, field_name="relation")
        if not from_name or not to_name or not relation:
            continue
        links.append(
            KnowledgeExtractionLink(
                from_name=from_name,
                to_name=to_name,
                relation=relation,
                from_type=_clean_text(
                    item.get("from_type") or "freeform",
                    max_len=80,
                    field_name="from_type",
                )
                or "freeform",
                to_type=_clean_text(
                    item.get("to_type") or "freeform",
                    max_len=80,
                    field_name="to_type",
                )
                or "freeform",
                confidence=_confidence(item.get("confidence")),
            )
        )

    warnings = [_clean_text(warning, max_len=None, field_name="warning") for warning in (payload.get("warnings") or [])]
    return KnowledgeExtractionResult(
        entities=tuple(entities),
        assertions=tuple(assertions),
        links=tuple(links),
        warnings=tuple(warning for warning in warnings if warning),
    )


class PersonalKnowledgeLLMExtractor:
    """Extract entities, assertions, and links from one canonical KB segment."""

    def __init__(self, *, model_config_resolver: Any | None = None) -> None:
        self._model_config_resolver = model_config_resolver

    async def _resolve_model_config(self, tenant_id: uuid.UUID) -> dict[str, Any] | None:
        if self._model_config_resolver is not None:
            return await self._model_config_resolver(tenant_id)
        from app.services.memory_service import _get_summary_model_config

        return await _get_summary_model_config(tenant_id)

    async def extract_segment(
        self,
        *,
        segment: Any,
        document: Any,
        source_ref: dict[str, Any],
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        sensitivity: str,
    ) -> KnowledgeExtractionResult:
        if str(sensitivity or "").lower() in {"private", "secret", "restricted", "pl3", "pl4", "credential"}:
            raise PersonalKnowledgeExtractionUnavailable("knowledge_extraction_skipped_sensitive")

        model_config = await self._resolve_model_config(tenant_id)
        if not model_config:
            raise PersonalKnowledgeExtractionUnavailable("knowledge_extractor_model_unavailable")

        from app.services.llm_client import (
            LLMMessage,
            create_llm_client_from_config,
            get_max_tokens,
            with_llm_usage_context,
        )

        client = create_llm_client_from_config(
            with_llm_usage_context(
                model_config,
                source="personal_knowledge_extractor",
                tenant_id=tenant_id,
                user_id=owner_user_id,
                metadata={"document_id": str(document.id), "segment_id": str(segment.id)},
            )
        )
        prompt = (
            "You extract a compact, source-grounded personal knowledge graph from one Markdown segment.\n"
            "Return only strict JSON with keys: entities, assertions, links, warnings.\n"
            "Rules:\n"
            "- Use only facts present in the segment. Do not infer beyond the text.\n"
            "- entities[]: {canonical_name, type, aliases[], description, confidence}.\n"
            "- assertions[]: {subject, predicate, object, confidence}.\n"
            "- links[]: {from, from_type, to, to_type, relation, confidence}.\n"
            "- Structural limits: names/aliases/subjects <=300 chars; types/relations <=80; predicates <=120.\n"
            "- Description, assertion object, and warning text must preserve every source-grounded detail needed for meaning.\n"
            "- Keep identifiers short and stable. Prefer user-facing concepts, people, orgs, projects, documents.\n"
            "- If the segment has no durable knowledge, return empty arrays.\n"
        )
        payload = {
            "document": {
                "id": str(document.id),
                "title": str(getattr(document, "title", "") or ""),
                "source_kind": str(getattr(document, "source_kind", "") or ""),
            },
            "source_ref": source_ref,
            "heading_path": list(getattr(segment, "heading_path_json", None) or []),
            "content": str(getattr(segment, "content", "") or ""),
        }
        try:
            response = await client.complete(
                messages=[
                    LLMMessage(role="system", content=prompt),
                    LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False, indent=2)),
                ],
                temperature=0.0,
                max_tokens=get_max_tokens(
                    str(model_config.get("provider") or ""),
                    str(model_config.get("model") or ""),
                    model_config.get("max_output_tokens"),
                ),
            )
        finally:
            await client.close()

        content = str(getattr(response, "content", "") or "")
        if not content.strip():
            raise PersonalKnowledgeExtractionError("model returned empty extraction")
        result = parse_extraction_payload(_loads_json_object(content))
        usage = getattr(response, "usage", None)
        usage_dict = dict(usage) if isinstance(usage, dict) else None
        usage_tokens: int | None = None
        if usage_dict:
            from app.services.token_tracker import extract_usage_tokens

            usage_tokens = extract_usage_tokens(usage_dict)
        return KnowledgeExtractionResult(
            entities=result.entities,
            assertions=result.assertions,
            links=result.links,
            warnings=result.warnings,
            usage=usage_dict,
            usage_tokens=usage_tokens,
        )
