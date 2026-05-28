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
from app.services.deep_research.schemas import ResearchRequest, SourceRecord, SourceType, WorkerResult


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
            return WorkerResult(topic=topic, intermediate_report="", status="failed", error="No model or agent available")

        captured_sources: list[SourceRecord] = []

        async def on_tool_call(event: dict[str, Any]) -> None:
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
                    select(LLMModel).where(LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id)
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
    cleaned = clean_fetched_text(raw_result)
    if not _has_usable_content(cleaned):
        return None
    return SourceRecord(
        source_id="",
        url=url,
        title=_extract_title(raw_result) or url,
        publisher=_publisher_from_url(url),
        source_type=SourceType.UNKNOWN,
        content=cleaned,
        fetch_tool=tool_name,
    )


def _build_worker_prompt(request: ResearchRequest, topic: str) -> str:
    return (
        "Run one Deep Research worker lane. Use web_search only for discovery; use web_fetch, "
        "firecrawl_fetch, or xcrawl_scrape to fetch source text before treating anything as evidence.\n\n"
        f"User question: {request.question}\n"
        f"Mode: {request.mode}\n"
        f"Scope: {request.scope or 'not specified'}\n"
        f"Time window: {request.time_window or 'not specified'}\n"
        f"Topic to investigate: {topic}\n\n"
        "Return a compact intermediate report with:\n"
        "- topic answer in 5-10 bullets\n"
        "- source-grounded facts with concrete numbers, dates, named entities\n"
        "- contradictions, weak evidence, and missing checks\n"
        "- use source ids only when the runtime provides them; otherwise cite URLs in prose."
    )


def _build_worker_system_prompt(request: ResearchRequest, topic: str) -> str:
    return (
        "SUB-AGENT ROLE: Deep Research orchestrator-worker. "
        "You may browse only with the provided read-only web tools. "
        "Do not delegate, do not write files, do not call Deep Research recursively. "
        "Search snippets are discovery only; fetched pages are evidence. "
        "Produce dense intermediate findings, not final report prose. "
        f"Mode={request.mode}; topic={topic}."
    )


def _worker_tool_rounds_for_model(model: Any) -> int:
    name = f"{getattr(model, 'provider', '')} {getattr(model, 'model', '')}".casefold()
    if any(token in name for token in ("o3", "o4", "reason", "thinking", "claude-4", "deepseek")):
        return 10
    return 8


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
        if "fetched content from:" in lowered:
            continue
        for prefix in ("title:", "#"):
            if lowered.startswith(prefix):
                return line[len(prefix):].strip()
        return line[:120]
    return ""


def _publisher_from_url(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")
