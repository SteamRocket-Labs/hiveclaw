from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.session import SessionContext
from app.services.deep_research.extractor import clean_fetched_text
from app.services.deep_research.language import resolve_output_language_label
from app.services.deep_research.schemas import ResearchRequest, SourceRecord, SourceType, WorkerResult, new_id


RESEARCH_WORKER_ALLOWED_TOOLS: tuple[str, ...] = (
    "web_search",
    "web_fetch",
    "firecrawl_fetch",
    "xcrawl_scrape",
)
RESEARCH_WORKER_EXCLUDED_TOOLS: tuple[str, ...] = (
    "deep_research_run",
    "deep_research_start",
    "deep_research_check",
    "deep_research_cancel",
    "deep_research_export",
    "delegate_to_agent",
    "send_message_to_agent",
    "write_file",
    "edit_file",
    "delete_file",
)
_FETCH_TOOLS = {"web_fetch", "firecrawl_fetch", "xcrawl_scrape"}
# F2/F3 (RC2/RC3): cap a single captured source so one oversized page cannot blow the
# worker token budget (the production incident: worker #3 burned 452K tokens on one giant page).
_MAX_SOURCE_CONTENT_CHARS = 12000
# F3 (RC3): cap how many sources one worker hoards (production: worker #3 grabbed 18 sources).
_MAX_SOURCES_PER_WORKER = 8

InvokeAgent = Callable[[AgentInvocationRequest], Awaitable[Any]]


class RuntimeResearchWorker:
    """One governed research worker used by the v2 orchestrator.

    Unlike the internal reasoner, this worker is allowed to browse, but only
    through the allow-listed read-only web tools. It returns an intermediate
    digest plus fetched source records for the parent ledger.
    """

    def __init__(
        self,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        model: Any | None = None,
        fallback_model: Any | None = None,
        agent: Any | None = None,
        invoke: InvokeAgent = invoke_agent,
    ):
        self.agent_id = agent_id
        self.user_id = user_id
        self.model = model
        self.fallback_model = fallback_model
        self.agent = agent
        self.invoke = invoke

    async def run(
        self,
        topic: str,
        *,
        request: ResearchRequest,
        cancel_event=None,
    ) -> WorkerResult:
        model, fallback_model, agent = await self._resolve_models()
        if model is None or agent is None:
            return WorkerResult(
                topic=topic, intermediate_report="", status="failed", error="No model or agent available"
            )

        captured_sources: list[SourceRecord] = []

        async def on_tool_call(event: dict[str, Any]) -> None:
            if len(captured_sources) >= _MAX_SOURCES_PER_WORKER:
                return
            source = _source_from_tool_event(event)
            if source is not None:
                captured_sources.append(source)

        content = ""
        tokens_used = 0
        try:
            result = await self.invoke(
                AgentInvocationRequest(
                    model=model,
                    fallback_model=fallback_model,
                    messages=[{"role": "user", "content": _build_worker_prompt(request, topic)}],
                    memory_messages=[{"role": "user", "content": _build_worker_prompt(request, topic)}],
                    agent_name=f"{getattr(agent, 'name', 'Agent')} Deep Research Worker",
                    role_description="Deep Research v2 worker. Discover, fetch, and digest evidence for one topic.",
                    agent_id=self.agent_id,
                    user_id=self.user_id,
                    on_tool_call=on_tool_call,
                    cancel_event=cancel_event,
                    session_context=SessionContext(
                        source="deep_research_worker",
                        channel="internal",
                        metadata={
                            "task": "deep_research_worker",
                            "topic": topic,
                            "mode": request.mode,
                            "depth": request.depth,
                        },
                    ),
                    system_prompt_suffix=_build_worker_system_prompt(request, topic),
                    core_tools_only=False,
                    allowed_tool_names=RESEARCH_WORKER_ALLOWED_TOOLS,
                    excluded_tool_names=RESEARCH_WORKER_EXCLUDED_TOOLS,
                    expand_tools=False,
                    max_tool_rounds=_worker_tool_rounds_for_model(model),
                )
            )
            content = str(getattr(result, "content", "") or "").strip()
            tokens_used = int(getattr(result, "tokens_used", 0) or 0)
        except Exception as exc:
            return WorkerResult(
                topic=topic,
                intermediate_report=content,
                sources=captured_sources,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                tokens_used=tokens_used,
            )

        status = "ok" if content or captured_sources else "failed"
        error = "" if status == "ok" else "Worker returned no digest and no fetched sources"
        return WorkerResult(
            topic=topic,
            intermediate_report=content,
            sources=captured_sources,
            status=status,
            error=error,
            tokens_used=tokens_used,
        )

    async def _resolve_models(self) -> tuple[Any | None, Any | None, Any | None]:
        if self.model is not None and self.agent is not None:
            return self.model, self.fallback_model, self.agent
        async with async_session() as db:
            agent_result = await db.execute(select(Agent).where(Agent.id == self.agent_id))
            agent = agent_result.scalar_one_or_none()
            if agent is None:
                return None, None, None
            model = None
            fallback_model = None
            if agent.primary_model_id:
                model_result = await db.execute(
                    select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
                )
                model = model_result.scalar_one_or_none()
            if agent.fallback_model_id:
                fallback_result = await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id
                    )
                )
                fallback_model = fallback_result.scalar_one_or_none()
            return model or fallback_model, fallback_model, agent


def _source_from_tool_event(event: dict[str, Any]) -> SourceRecord | None:
    tool_name = str(event.get("name") or event.get("tool_name") or "").strip()
    if tool_name not in _FETCH_TOOLS or event.get("status") != "done":
        return None
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    url = str(args.get("url") or "").strip()
    if not url:
        return None
    raw_result = str(event.get("result") or "")
    # RC2 backstop: web_fetch now extracts PDFs at the source, but guard the other fetch
    # tools (and future regressions) from leaking an unparsed PDF / raw binary as a source.
    if _looks_like_binary_or_pdf(raw_result):
        return None
    cleaned = clean_fetched_text(raw_result)
    if not _has_usable_content(cleaned):
        return None
    if len(cleaned) > _MAX_SOURCE_CONTENT_CHARS:
        cleaned = cleaned[:_MAX_SOURCE_CONTENT_CHARS]
    return SourceRecord(
        # P4: assign a stable id at fetch so the id is consistent worker -> ledger -> report.
        source_id=new_id("src"),
        url=url,
        title=_extract_title(raw_result) or url,
        publisher=_publisher_from_url(url),
        source_type=_infer_source_type(url),
        content=cleaned,
        fetch_tool=tool_name,
    )


def _build_worker_prompt(request: ResearchRequest, topic: str) -> str:
    language = resolve_output_language_label(request)
    return (
        "Run one Deep Research worker lane. Use web_search only for discovery; use web_fetch, "
        "firecrawl_fetch, or xcrawl_scrape to fetch full source text before treating anything as evidence.\n\n"
        f"Research question: {request.question}\n"
        f"Mode: {request.mode}\n"
        f"Scope: {request.scope or 'not specified'}\n"
        f"Time window: {request.time_window or 'not specified'}\n"
        f"Topic to investigate: {topic}\n\n"
        f"OUTPUT LANGUAGE: Write the entire digest in {language}. Translate every finding, quote, and "
        "description into that language. Keep proper names, tickers, ledger ids, and code identifiers in "
        "their original form. Never mix languages.\n\n"
        "Produce a COMPACT, INTEGRATED digest (NOT a list of per-page summaries), under ~600 words, with these sections:\n"
        "## Findings — 4-8 integrated bullets. Synthesize across the pages you read; do not summarize each page "
        "separately. Each finding = a claim + the concrete number/date/named entity that grounds it + the "
        "source URL(s) that support it.\n"
        "## Evidence — concrete numbers, named entities, dates, and mechanisms you verified from fetched pages.\n"
        "## Contradictions & weak spots — disagreements between sources, thin or unverified claims, anything you "
        "could not confirm. Report disconfirming evidence too — do not cherry-pick.\n"
        "## Coverage gaps — what this topic still needs.\n\n"
        "Rules: fetched pages are evidence; search snippets are not. Grade source strength as you go. "
        "Calibrate confidence — mark each finding verified (seen in a fetched page), inferred, or unverified; "
        "never state preliminary evidence as established. Give the warrant (why the evidence supports the point), "
        "not just the data. Avoid filler words (delve, leverage, robust, comprehensive); be concrete. "
        "Cite source URLs in prose; the runtime assigns durable ids later."
    )


def _build_worker_system_prompt(request: ResearchRequest, topic: str) -> str:
    language = resolve_output_language_label(request)
    return (
        "SUB-AGENT ROLE: Deep Research orchestrator-worker. "
        "You may browse only with the provided read-only web tools. "
        "Do not delegate, do not write files, do not call Deep Research recursively. "
        "Search snippets are discovery only; fetched pages are evidence. "
        "Integrate findings across sources — never produce a per-page list. "
        "Report disconfirming evidence; do not cherry-pick. "
        f"Write everything in {language}; keep proper names and identifiers in their original form. "
        f"Mode={request.mode}; topic={topic}."
    )


def _worker_tool_rounds_for_model(model: Any) -> int:
    name = f"{getattr(model, 'provider', '')} {getattr(model, 'model', '')}".casefold()
    if any(token in name for token in ("o3", "o4", "reason", "thinking", "claude-4", "deepseek")):
        return 10
    return 8


def _looks_like_binary_or_pdf(text: str) -> bool:
    """Reject unparsed PDF or raw binary payloads (RC2).

    A fetch tool that returned bytes-as-text (e.g. a `%PDF-1.4` / `/FlateDecode` stream)
    must never become a source: it wastes tokens and poisons synthesis.
    """
    if not text:
        return False
    head = text.lstrip()[:1024]
    if head.startswith("%PDF") or "/FlateDecode" in head:
        return True
    sample = text[:2000]
    control_chars = sum(1 for ch in sample if ord(ch) < 9 or 13 < ord(ch) < 32)
    return control_chars / len(sample) > 0.10


def _has_usable_content(text: str) -> bool:
    if len(text) < 80:
        return False
    lowered = text.lower()
    return not lowered.startswith(("❌", "[error]", "error:", "web_fetch failed", "firecrawl_fetch failed"))


def _extract_title(text: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        # RC8: skip fetch envelopes, PDF headers, extracted-PDF page markers, and pure-symbol lines
        # so the title is a real heading, not "%PDF-1.4" or "📄 Fetched content from ...".
        if "fetched content from:" in lowered or "content from:" in lowered:
            continue
        if line.startswith("%PDF") or line.startswith("📄") or line.startswith("---"):
            continue
        if not any(ch.isalnum() for ch in line):
            continue
        for prefix in ("title:", "#"):
            if lowered.startswith(prefix):
                return line[len(prefix) :].strip()
        return line[:120]
    return ""


def _publisher_from_url(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def _infer_source_type(url: str) -> SourceType:
    """Domain-general source-type inference (RC6) so captured sources are not all UNKNOWN→tier3.
    Uses universal authority signals (government / academic), not a domain-specific allowlist."""
    host = urlparse(url).netloc.casefold()
    if any(fragment in host for fragment in (".gov", ".gov.", ".mil", ".int", "europa.eu")):
        return SourceType.REGULATORY
    if any(fragment in host for fragment in (".edu", ".edu.", ".ac.", "arxiv.org", "doi.org", "ssrn.")):
        return SourceType.PRIMARY
    return SourceType.UNKNOWN
