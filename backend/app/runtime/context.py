"""Explicit runtime context types shared by agent entrypoints.

P1-W3-5: this module is the convergence point for what used to be four
independent helpers (context.py / context_engine.py / context_budget.py
/ coordinator.py). Each piece keeps its own focused module — the engine
encapsulates context-block fencing, the budget profile encapsulates
char/token allocation, the coordinator picks the per-turn dispatch shape
— but `RuntimeContext` now exposes them as composed members and offers a
single `for_invocation` factory so downstream callers stop having to
import all four modules to build one consistent context.

Adoption is incremental: existing callers that touch only `session` /
`execution_identity` / `tenant_id` / `metadata` keep working unchanged
because the new fields default to None.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.execution_context import ExecutionIdentity
from app.runtime.session import SessionContext

if TYPE_CHECKING:  # avoid circular imports at runtime
    from app.runtime.context_budget import ContextBudget
    from app.runtime.context_engine import ContextEngine


_ASSEMBLY_STATE_KEY = "runtime_assembly_state"


def _dict_payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_payload(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _dict_list_payload(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in _list_payload(value) if isinstance(item, dict)]


def _string_list_payload(value: Any) -> list[str]:
    return [text for item in _list_payload(value) if (text := str(item or "").strip())]


def _manifest_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_manifest"):
        value = value.to_manifest()
    return _dict_payload(value)


@dataclass(slots=True)
class RuntimeAssemblyState:
    """Serializable runtime assembly ledger for one invocation/session.

    This is the convergence point for context/tool/skill/retrieval assembly
    artifacts. Top-level session metadata keys remain as compatibility mirrors,
    but this object owns the canonical runtime read model.
    """

    session: SessionContext | None = field(default=None, repr=False, compare=False)
    prompt_assembly_manifest: dict[str, Any] = field(default_factory=dict)
    context_usage_ledger: dict[str, Any] = field(default_factory=dict)
    dynamic_context_section_ledger: dict[str, Any] = field(default_factory=dict)
    tool_result_ledger: list[dict[str, Any]] = field(default_factory=list)
    cache_decision_ledger: list[dict[str, Any]] = field(default_factory=list)
    runtime_decision_ledger: list[dict[str, Any]] = field(default_factory=list)
    agent_cycle_decision_ledger: list[dict[str, Any]] = field(default_factory=list)
    runtime_reminder_candidates: list[dict[str, Any]] = field(default_factory=list)
    codex_optimization_ledger: dict[str, Any] = field(default_factory=dict)
    activation_query: dict[str, Any] = field(default_factory=dict)
    activation_lanes: list[str] = field(default_factory=list)
    activation_parse_trace: list[dict[str, Any]] = field(default_factory=list)
    activation_candidates: list[dict[str, Any]] = field(default_factory=list)
    activation_router_output: dict[str, Any] = field(default_factory=dict)
    top_activation_candidates: list[dict[str, Any]] = field(default_factory=list)
    suppressed_activation_candidates: list[dict[str, Any]] = field(default_factory=list)
    available_deferred_tool_candidates: list[Any] = field(default_factory=list)
    available_deferred_tools: list[str] = field(default_factory=list)
    skill_catalog_ranking: list[dict[str, Any]] = field(default_factory=list)
    skill_catalog_ranking_inputs: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_metadata(
        cls,
        data: Any,
        *,
        session: SessionContext | None = None,
    ) -> "RuntimeAssemblyState":
        payload = _dict_payload(data)
        return cls(
            session=session,
            prompt_assembly_manifest=_dict_payload(payload.get("prompt_assembly_manifest")),
            context_usage_ledger=_dict_payload(payload.get("context_usage_ledger")),
            dynamic_context_section_ledger=_dict_payload(payload.get("dynamic_context_section_ledger")),
            tool_result_ledger=[dict(item) for item in _list_payload(payload.get("tool_result_ledger"))],
            cache_decision_ledger=[dict(item) for item in _list_payload(payload.get("cache_decision_ledger"))],
            runtime_decision_ledger=[dict(item) for item in _list_payload(payload.get("runtime_decision_ledger"))],
            agent_cycle_decision_ledger=[
                dict(item) for item in _list_payload(payload.get("agent_cycle_decision_ledger"))
            ],
            runtime_reminder_candidates=[
                dict(item) for item in _list_payload(payload.get("runtime_reminder_candidates"))
            ],
            codex_optimization_ledger=_dict_payload(payload.get("codex_optimization_ledger")),
            activation_query=_dict_payload(payload.get("activation_query")),
            activation_lanes=_string_list_payload(payload.get("activation_lanes")),
            activation_parse_trace=_dict_list_payload(payload.get("activation_parse_trace")),
            activation_candidates=_dict_list_payload(payload.get("activation_candidates")),
            activation_router_output=_dict_payload(payload.get("activation_router_output")),
            top_activation_candidates=_dict_list_payload(payload.get("top_activation_candidates")),
            suppressed_activation_candidates=_dict_list_payload(payload.get("suppressed_activation_candidates")),
            available_deferred_tool_candidates=_list_payload(payload.get("available_deferred_tool_candidates")),
            available_deferred_tools=[
                str(item) for item in _list_payload(payload.get("available_deferred_tools")) if str(item).strip()
            ],
            skill_catalog_ranking=[dict(item) for item in _list_payload(payload.get("skill_catalog_ranking"))],
            skill_catalog_ranking_inputs=_dict_payload(payload.get("skill_catalog_ranking_inputs")),
        )

    def to_metadata(self) -> dict[str, Any]:
        return {
            "schema": "hive.ccplus.runtime_assembly_state.v1",
            "prompt_assembly_manifest": dict(self.prompt_assembly_manifest),
            "context_usage_ledger": dict(self.context_usage_ledger),
            "dynamic_context_section_ledger": dict(self.dynamic_context_section_ledger),
            "tool_result_ledger": [dict(item) for item in self.tool_result_ledger],
            "cache_decision_ledger": [dict(item) for item in self.cache_decision_ledger],
            "runtime_decision_ledger": [dict(item) for item in self.runtime_decision_ledger],
            "agent_cycle_decision_ledger": [dict(item) for item in self.agent_cycle_decision_ledger],
            "runtime_reminder_candidates": [dict(item) for item in self.runtime_reminder_candidates],
            "codex_optimization_ledger": dict(self.codex_optimization_ledger),
            "activation_query": dict(self.activation_query),
            "activation_lanes": list(self.activation_lanes),
            "activation_parse_trace": [dict(item) for item in self.activation_parse_trace],
            "activation_candidates": [dict(item) for item in self.activation_candidates],
            "activation_router_output": dict(self.activation_router_output),
            "top_activation_candidates": [dict(item) for item in self.top_activation_candidates],
            "suppressed_activation_candidates": [dict(item) for item in self.suppressed_activation_candidates],
            "available_deferred_tool_candidates": list(self.available_deferred_tool_candidates),
            "available_deferred_tools": list(self.available_deferred_tools),
            "skill_catalog_ranking": [dict(item) for item in self.skill_catalog_ranking],
            "skill_catalog_ranking_inputs": dict(self.skill_catalog_ranking_inputs),
        }

    def persist(self) -> None:
        if self.session is None:
            return
        metadata = self.session.metadata
        metadata[_ASSEMBLY_STATE_KEY] = self.to_metadata()
        if self.prompt_assembly_manifest:
            metadata["prompt_assembly_manifest"] = dict(self.prompt_assembly_manifest)
        if self.context_usage_ledger:
            metadata["context_usage_ledger"] = dict(self.context_usage_ledger)
        if self.dynamic_context_section_ledger:
            metadata["dynamic_context_section_ledger"] = dict(self.dynamic_context_section_ledger)
        if self.tool_result_ledger:
            metadata["tool_result_ledger"] = [dict(item) for item in self.tool_result_ledger]
        if self.cache_decision_ledger:
            metadata["cache_decision_ledger"] = [dict(item) for item in self.cache_decision_ledger]
        if self.runtime_decision_ledger:
            metadata["runtime_decision_ledger"] = [dict(item) for item in self.runtime_decision_ledger]
        if self.agent_cycle_decision_ledger:
            metadata["agent_cycle_decision_ledger"] = [dict(item) for item in self.agent_cycle_decision_ledger]
        if self.runtime_reminder_candidates:
            metadata["runtime_reminder_candidates"] = [dict(item) for item in self.runtime_reminder_candidates]
        if self.codex_optimization_ledger:
            metadata["codex_optimization_ledger"] = dict(self.codex_optimization_ledger)
        if self.activation_query:
            metadata["activation_query"] = dict(self.activation_query)
        if self.activation_lanes:
            metadata["activation_lanes"] = list(self.activation_lanes)
        if self.activation_parse_trace:
            metadata["activation_parse_trace"] = [dict(item) for item in self.activation_parse_trace]
        if self.activation_candidates:
            metadata["activation_candidates"] = [dict(item) for item in self.activation_candidates]
        if self.activation_router_output:
            metadata["activation_router_output"] = dict(self.activation_router_output)
        if self.top_activation_candidates:
            metadata["top_activation_candidates"] = [dict(item) for item in self.top_activation_candidates]
        if self.suppressed_activation_candidates:
            metadata["suppressed_activation_candidates"] = [
                dict(item) for item in self.suppressed_activation_candidates
            ]
        if self.available_deferred_tool_candidates:
            metadata["available_deferred_tool_candidates"] = list(self.available_deferred_tool_candidates)
        if self.available_deferred_tools:
            metadata["available_deferred_tools"] = list(self.available_deferred_tools)
        if self.skill_catalog_ranking:
            metadata["skill_catalog_ranking"] = [dict(item) for item in self.skill_catalog_ranking]
        if self.skill_catalog_ranking_inputs:
            metadata["skill_catalog_ranking_inputs"] = dict(self.skill_catalog_ranking_inputs)

    def record_prompt_manifest(self, manifest: dict[str, Any]) -> None:
        self.prompt_assembly_manifest = dict(manifest)
        self.context_usage_ledger = _dict_payload(manifest.get("context_usage_ledger"))
        self.dynamic_context_section_ledger = _dict_payload(manifest.get("dynamic_context_section_ledger"))
        self.persist()

    def record_context_usage_ledger(self, ledger: dict[str, Any]) -> None:
        self.context_usage_ledger = dict(ledger)
        self.persist()

    def record_dynamic_context_section_ledger(self, ledger: dict[str, Any]) -> None:
        self.dynamic_context_section_ledger = dict(ledger)
        self.persist()

    def record_tool_result(self, entry: dict[str, Any], *, limit: int = 50) -> None:
        self.tool_result_ledger.append(dict(entry))
        if len(self.tool_result_ledger) > limit:
            del self.tool_result_ledger[: len(self.tool_result_ledger) - limit]
        self.persist()

    def record_cache_decision(self, entry: dict[str, Any], *, limit: int = 50) -> None:
        self.cache_decision_ledger.append(dict(entry))
        if len(self.cache_decision_ledger) > limit:
            del self.cache_decision_ledger[: len(self.cache_decision_ledger) - limit]
        self.persist()

    def record_runtime_decision(self, entry: dict[str, Any], *, limit: int = 100) -> None:
        self.runtime_decision_ledger.append(dict(entry))
        if len(self.runtime_decision_ledger) > limit:
            del self.runtime_decision_ledger[: len(self.runtime_decision_ledger) - limit]
        self.persist()

    def record_agent_cycle_decision(self, entry: dict[str, Any], *, limit: int = 100) -> None:
        self.agent_cycle_decision_ledger.append(dict(entry))
        if len(self.agent_cycle_decision_ledger) > limit:
            del self.agent_cycle_decision_ledger[: len(self.agent_cycle_decision_ledger) - limit]
        self.persist()

    def record_runtime_reminder_candidate(self, candidate: dict[str, Any], *, limit: int = 50) -> None:
        self.runtime_reminder_candidates.append(dict(candidate))
        if len(self.runtime_reminder_candidates) > limit:
            del self.runtime_reminder_candidates[: len(self.runtime_reminder_candidates) - limit]
        self.persist()

    def record_codex_optimization_ledger(self, ledger: dict[str, Any]) -> None:
        self.codex_optimization_ledger = dict(ledger)
        self.persist()

    def record_activation_query(self, query: Any) -> None:
        manifest = _manifest_payload(query)
        self.activation_query = manifest
        self.activation_lanes = _string_list_payload(manifest.get("candidate_lanes"))
        self.activation_parse_trace = _dict_list_payload(manifest.get("parse_trace"))
        self.persist()

    def record_activation_candidates(self, candidates: list[Any] | tuple[Any, ...], *, limit: int = 200) -> None:
        entries = [_manifest_payload(candidate) for candidate in candidates]
        self.activation_candidates = entries[-limit:]
        self.persist()

    def record_activation_router_output(self, output: dict[str, Any]) -> None:
        self.activation_router_output = dict(output)
        self.top_activation_candidates = _dict_list_payload(output.get("top_activation_candidates"))
        self.suppressed_activation_candidates = _dict_list_payload(output.get("suppressed_activation_candidates"))
        self.persist()

    def record_deferred_tools(self, candidates: list[Any] | tuple[Any, ...]) -> None:
        candidate_list = list(candidates or [])
        names: list[str] = []
        for candidate in candidate_list:
            if isinstance(candidate, dict):
                name = str(candidate.get("name") or "").strip()
            else:
                name = str(candidate or "").strip()
            if name:
                names.append(name)
        self.available_deferred_tool_candidates = candidate_list
        self.available_deferred_tools = names
        self.persist()

    def record_skill_catalog_ranking(
        self,
        *,
        ranking: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        inputs: dict[str, Any],
    ) -> None:
        self.skill_catalog_ranking = [dict(item) for item in ranking]
        self.skill_catalog_ranking_inputs = dict(inputs)
        self.persist()


def ensure_runtime_assembly_state(session: SessionContext | None) -> RuntimeAssemblyState:
    if session is None:
        return RuntimeAssemblyState()
    existing = getattr(session, "runtime_assembly_state", None)
    if isinstance(existing, RuntimeAssemblyState):
        return existing
    state = RuntimeAssemblyState.from_metadata(session.metadata.get(_ASSEMBLY_STATE_KEY), session=session)
    if not state.prompt_assembly_manifest:
        state.prompt_assembly_manifest = _dict_payload(session.metadata.get("prompt_assembly_manifest"))
    if not state.context_usage_ledger:
        state.context_usage_ledger = _dict_payload(session.metadata.get("context_usage_ledger"))
    if not state.dynamic_context_section_ledger:
        state.dynamic_context_section_ledger = _dict_payload(session.metadata.get("dynamic_context_section_ledger"))
    if not state.tool_result_ledger:
        state.tool_result_ledger = [dict(item) for item in _list_payload(session.metadata.get("tool_result_ledger"))]
    if not state.cache_decision_ledger:
        state.cache_decision_ledger = [
            dict(item) for item in _list_payload(session.metadata.get("cache_decision_ledger"))
        ]
    if not state.runtime_decision_ledger:
        state.runtime_decision_ledger = [
            dict(item) for item in _list_payload(session.metadata.get("runtime_decision_ledger"))
        ]
    if not state.agent_cycle_decision_ledger:
        state.agent_cycle_decision_ledger = [
            dict(item) for item in _list_payload(session.metadata.get("agent_cycle_decision_ledger"))
        ]
    if not state.runtime_reminder_candidates:
        state.runtime_reminder_candidates = [
            dict(item) for item in _list_payload(session.metadata.get("runtime_reminder_candidates"))
        ]
    if not state.codex_optimization_ledger:
        state.codex_optimization_ledger = _dict_payload(session.metadata.get("codex_optimization_ledger"))
    if not state.activation_query:
        state.activation_query = _dict_payload(session.metadata.get("activation_query"))
    if not state.activation_lanes:
        state.activation_lanes = _string_list_payload(session.metadata.get("activation_lanes"))
    if not state.activation_parse_trace:
        state.activation_parse_trace = _dict_list_payload(session.metadata.get("activation_parse_trace"))
    if not state.activation_candidates:
        state.activation_candidates = _dict_list_payload(session.metadata.get("activation_candidates"))
    if not state.activation_router_output:
        state.activation_router_output = _dict_payload(session.metadata.get("activation_router_output"))
    if not state.top_activation_candidates:
        state.top_activation_candidates = _dict_list_payload(session.metadata.get("top_activation_candidates"))
    if not state.suppressed_activation_candidates:
        state.suppressed_activation_candidates = _dict_list_payload(
            session.metadata.get("suppressed_activation_candidates")
        )
    if not state.available_deferred_tool_candidates:
        state.available_deferred_tool_candidates = _list_payload(
            session.metadata.get("available_deferred_tool_candidates")
        )
    if not state.available_deferred_tools:
        state.available_deferred_tools = [
            str(item) for item in _list_payload(session.metadata.get("available_deferred_tools")) if str(item).strip()
        ]
    if not state.skill_catalog_ranking:
        state.skill_catalog_ranking = [dict(item) for item in _list_payload(session.metadata.get("skill_catalog_ranking"))]
    if not state.skill_catalog_ranking_inputs:
        state.skill_catalog_ranking_inputs = _dict_payload(session.metadata.get("skill_catalog_ranking_inputs"))
    session.runtime_assembly_state = state
    state.persist()
    return state


@dataclass(slots=True)
class RuntimeContext:
    """Normalized runtime context for a single agent invocation.

    Composes the four W3-5 primitives in one place so callers don't have
    to import context_budget + context_engine + coordinator separately
    when wiring an invocation.
    """

    session: SessionContext = field(default_factory=SessionContext)
    execution_identity: ExecutionIdentity | None = None
    tenant_id: uuid.UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # P1-W3-5 composition members. Optional so the dataclass keeps its
    # existing default-construction surface; callers that need them set
    # explicit values via `for_invocation` (or the field directly).
    engine: "ContextEngine | None" = None
    budget: "ContextBudget | None" = None
    assembly_state: RuntimeAssemblyState = field(default_factory=RuntimeAssemblyState)

    @classmethod
    def for_invocation(
        cls,
        *,
        session: SessionContext | None = None,
        execution_identity: ExecutionIdentity | None = None,
        tenant_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
        engine: "ContextEngine | None" = None,
        budget: "ContextBudget | None" = None,
    ) -> "RuntimeContext":
        """Build a RuntimeContext with all primitives wired in one call.

        Lazily defaults `engine` to `DefaultContextEngine()` so callers
        that don't specify one still get the standard fencing behaviour.
        Budget stays None unless caller passes one (compute_context_budget
        needs request-shaped inputs that the factory can't synthesize).
        """
        if engine is None:
            from app.runtime.context_engine import DefaultContextEngine

            engine = DefaultContextEngine()
        resolved_session = session if session is not None else SessionContext()
        return cls(
            session=resolved_session,
            execution_identity=execution_identity,
            tenant_id=tenant_id,
            metadata=metadata if metadata is not None else {},
            engine=engine,
            budget=budget,
            assembly_state=ensure_runtime_assembly_state(resolved_session),
        )
