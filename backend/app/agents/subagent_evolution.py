"""Subagent evolution loop (docs/subagent-evolution-loop.md): memory → definition.

P1 nomination + drafting: after a successful distillation write, if an
AGENT-LEVEL definition has accumulated ≥ threshold active memory entries and
has no pending proposal, the platform LLM drafts a revised system-prompt body
that absorbs the mature craft. The draft lands as a proposal file — never
applied directly; P2's approval surface (manual or auto mode) is the only
writer of the definition itself.

Contract-freeze by construction: a proposal stores ONLY the revised body. The
definition's frontmatter (tools/model/rounds/isolation) physically cannot be
changed through this channel — apply re-renders base frontmatter + new body.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid as uuid_mod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.agents.subagent_definition import (
    agent_subagent_root,
    definition_store_for_agent,
    render_subagent_definition,
    validate_subagent_name,
)
from app.agents.subagent_memory import SubagentMemoryStore
from app.services.llm_client import chat_complete

logger = logging.getLogger(__name__)

_PROPOSAL_DELIM = "---"

# Vendor-neutral by product law (L3 model equality) — pinned by test.
EVOLUTION_DRAFT_SYSTEM_PROMPT = """\
You maintain a subagent definition: a reusable specialist whose entire system prompt is the definition body. Your job is to absorb MATURE craft from its accumulated memory into a revised body — so proven working methods become part of the specialist itself instead of riding along as notes.

Hard rules:
- Absorb only craft that is GENERAL and PROVEN — lessons that clearly apply to most future runs. Leave one-off observations in memory (do not list them as absorbed).
- Edit incrementally: keep the existing role skeleton, tone, and structure. You are folding lessons in, not rewriting the specialist.
- The body must remain a complete, self-contained system prompt after your edit.
- HOW, not WHAT: working methods only; never bake domain facts or findings into the body.
- Never include secrets, credentials, or personal data.
- Write in the same language as the existing definition body.
- If nothing is mature enough to absorb, return an empty absorbed list and the body unchanged.

You will receive the current definition body and the active memory entries (each prefixed with its [id=...]).

Return ONLY a JSON object, no other text:
{
  "body": "the complete revised system prompt body",
  "absorbed_entry_ids": ["ids of the memory entries you folded into the body"],
  "rationale": "one short paragraph: what was absorbed and why it is mature"
}
"""


@dataclass(slots=True)
class EvolutionProposal:
    name: str
    status: str  # pending | approved | rejected
    base_definition_sha: str
    absorbed_entry_ids: list[str]
    rationale: str
    created_at: str
    proposal_id: str
    body: str  # the revised system-prompt body ONLY (contract freeze by construction)


class SubagentProposalStore:
    """File-backed proposal state (one pending proposal per definition name).

    Lives at ``<workspace>/subagents/.proposals/<name>.proposal.md`` — the
    dot-dir keeps proposals out of the definition glob (same device as
    ``.memory/``). The frontmatter is the state machine; the markdown body is
    the revised system-prompt body.
    """

    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)

    def _path(self, name: str) -> Path:
        safe_name = validate_subagent_name(name)
        base = self.base_dir.resolve()
        path = (base / f"{safe_name}.proposal.md").resolve()
        if not path.is_relative_to(base):
            raise ValueError("invalid subagent name: path escapes proposal store")
        return path

    def save(self, proposal: EvolutionProposal) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        front = {
            "name": proposal.name,
            "status": proposal.status,
            "base_definition_sha": proposal.base_definition_sha,
            "absorbed_entry_ids": list(proposal.absorbed_entry_ids),
            "rationale": proposal.rationale,
            "created_at": proposal.created_at,
            "proposal_id": proposal.proposal_id,
        }
        yaml_block = yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip()
        path = self._path(proposal.name)
        path.write_text(
            f"{_PROPOSAL_DELIM}\n{yaml_block}\n{_PROPOSAL_DELIM}\n\n{proposal.body}\n",
            encoding="utf-8",
        )
        return path

    def load(self, name: str) -> EvolutionProposal | None:
        path = self._path(name)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        stripped = text.lstrip()
        if not stripped.startswith(_PROPOSAL_DELIM):
            return None
        after = stripped[len(_PROPOSAL_DELIM) :]
        end = after.find("\n" + _PROPOSAL_DELIM)
        if end == -1:
            return None
        front = yaml.safe_load(after[:end]) or {}
        if not isinstance(front, dict):
            return None
        body = after[end + len("\n" + _PROPOSAL_DELIM) :].lstrip("\n").rstrip()
        return EvolutionProposal(
            name=str(front.get("name") or name),
            status=str(front.get("status") or "pending"),
            base_definition_sha=str(front.get("base_definition_sha") or ""),
            absorbed_entry_ids=[str(i) for i in (front.get("absorbed_entry_ids") or [])],
            rationale=str(front.get("rationale") or ""),
            created_at=str(front.get("created_at") or ""),
            proposal_id=str(front.get("proposal_id") or ""),
            body=body,
        )

    def load_pending(self, name: str) -> EvolutionProposal | None:
        proposal = self.load(name)
        return proposal if proposal is not None and proposal.status == "pending" else None

    def set_status(self, name: str, status: str) -> EvolutionProposal | None:
        proposal = self.load(name)
        if proposal is None:
            return None
        proposal.status = status
        self.save(proposal)
        return proposal


def proposal_store_for_agent(agent_id: object, *, agent_data_dir: Path | str | None = None) -> SubagentProposalStore:
    return SubagentProposalStore(agent_subagent_root(agent_id, agent_data_dir=agent_data_dir) / ".proposals")


def definition_sha(definition_text: str) -> str:
    return hashlib.sha256(definition_text.encode("utf-8")).hexdigest()[:16]


def _active_entry_ids(active_memory: str) -> set[str]:
    ids: set[str] = set()
    for line in active_memory.splitlines():
        if "[id=" in line:
            ids.add(line.split("[id=", 1)[1].split("]", 1)[0])
    return ids


async def draft_improvement(
    *,
    definition_body: str,
    active_memory: str,
    model_config: dict,
    agent_id: object | None = None,
    tenant_id: object | None = None,
) -> dict | None:
    """LLM-draft a revised body absorbing mature craft. Fail-soft: None on any
    unusable response (the nomination simply retries on a later distillation)."""

    user_message = f"Current definition body:\n\n{definition_body}\n\nActive memory entries:\n\n{active_memory}"
    try:
        response = await chat_complete(
            provider=model_config["provider"],
            api_key=model_config["api_key"],
            model=model_config["model"],
            base_url=model_config.get("base_url"),
            messages=[
                {"role": "system", "content": EVOLUTION_DRAFT_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.3,
            max_tokens=8192,  # CC-standard auxiliary-call floor
            timeout=90.0,
            usage_source="subagent_evolution",
            usage_agent_id=agent_id,
            usage_tenant_id=tenant_id,
        )
        content = str(response.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("draft response must be a JSON object")
        body = str(parsed.get("body") or "").strip()
        absorbed = [str(i).strip() for i in (parsed.get("absorbed_entry_ids") or []) if str(i).strip()]
        rationale = str(parsed.get("rationale") or "").strip()
        if not body or not absorbed:
            return None  # nothing mature enough — normal outcome, not an error
        return {"body": body, "absorbed_entry_ids": absorbed, "rationale": rationale}
    except Exception as exc:
        logger.warning("[SubagentEvolution] draft skipped (model=%s): %s", model_config.get("model"), exc)
        return None


@dataclass(slots=True)
class ApplyResult:
    applied: bool
    error: str = ""  # no_pending | definition_missing | stale_base | invalid
    proposal_id: str = ""
    absorbed_marked: int = 0


def _agent_workspace(agent_id: object, agent_data_dir: Path | str | None) -> Path:
    from app.config import get_settings

    root = Path(agent_data_dir or get_settings().AGENT_DATA_DIR)
    return root / str(agent_id)


def _memory_store(agent_id: object, agent_data_dir: Path | str | None) -> SubagentMemoryStore:
    from app.agents.subagent_memory import memory_store_for_agent

    return memory_store_for_agent(agent_id, agent_data_dir=agent_data_dir)


def apply_proposal(
    agent_id: object,
    spec_name: str,
    *,
    approved_by: str,
    agent_data_dir: Path | str | None = None,
) -> ApplyResult:
    """Apply a pending proposal — the ONLY definition writer in the loop.

    Atomic order: validate → write definition → mark absorbed → ledger →
    close proposal. Contract freeze is by construction: the revised spec is
    the BASE spec with only system_prompt replaced. A stale base (definition
    edited since drafting) auto-closes the proposal as rejected so nomination
    can run again from current state.
    """

    from dataclasses import replace

    from app.agents.subagent_definition import parse_subagent_definition
    from app.services.evolution_ledger import record_promotion_decision

    proposal_store = proposal_store_for_agent(agent_id, agent_data_dir=agent_data_dir)
    proposal = proposal_store.load_pending(spec_name)
    if proposal is None:
        return ApplyResult(applied=False, error="no_pending")

    definition_store = definition_store_for_agent(agent_id, agent_data_dir=agent_data_dir)
    base_spec = definition_store.load(spec_name)
    if base_spec is None:
        return ApplyResult(applied=False, error="definition_missing", proposal_id=proposal.proposal_id)

    base_text = render_subagent_definition(base_spec)
    if definition_sha(base_text) != proposal.base_definition_sha:
        proposal_store.set_status(spec_name, "rejected")
        record_promotion_decision(
            _agent_workspace(agent_id, agent_data_dir),
            candidate_id=proposal.proposal_id,
            decision="rejected",
            reason="stale_base: definition changed after drafting",
            metadata={"kind": "subagent_definition_promotion", "name": spec_name},
        )
        return ApplyResult(applied=False, error="stale_base", proposal_id=proposal.proposal_id)

    revised_spec = replace(base_spec, system_prompt=proposal.body)
    revised_text = render_subagent_definition(revised_spec)
    parse_subagent_definition(revised_text)  # same chain PUT uses; raises on invalid

    definition_store.save(revised_spec)
    marked = _memory_store(agent_id, agent_data_dir).mark_absorbed(
        spec_name, proposal.absorbed_entry_ids, proposal_id=proposal.proposal_id
    )
    record_promotion_decision(
        _agent_workspace(agent_id, agent_data_dir),
        candidate_id=proposal.proposal_id,
        decision="approved",
        reason=proposal.rationale,
        metadata={
            "kind": "subagent_definition_promotion",
            "name": spec_name,
            "approved_by": approved_by,
            "base_sha": proposal.base_definition_sha,
            "new_sha": definition_sha(revised_text),
            "absorbed_ids": list(proposal.absorbed_entry_ids),
            "absorbed_marked": marked,
        },
    )
    proposal_store.set_status(spec_name, "approved")
    logger.info(
        "[SubagentEvolution] proposal %s applied: agent=%s name=%s by=%s absorbed=%d",
        proposal.proposal_id,
        agent_id,
        spec_name,
        approved_by,
        marked,
    )
    return ApplyResult(applied=True, proposal_id=proposal.proposal_id, absorbed_marked=marked)


def reject_proposal(
    agent_id: object,
    spec_name: str,
    *,
    rejected_by: str,
    agent_data_dir: Path | str | None = None,
) -> bool:
    """Reject a pending proposal (kept on file as a negative sample + ledgered)."""

    from app.services.evolution_ledger import record_promotion_decision

    proposal_store = proposal_store_for_agent(agent_id, agent_data_dir=agent_data_dir)
    proposal = proposal_store.load_pending(spec_name)
    if proposal is None:
        return False
    proposal_store.set_status(spec_name, "rejected")
    record_promotion_decision(
        _agent_workspace(agent_id, agent_data_dir),
        candidate_id=proposal.proposal_id,
        decision="rejected",
        reason=f"rejected_by={rejected_by}",
        metadata={"kind": "subagent_definition_promotion", "name": spec_name, "rejected_by": rejected_by},
    )
    return True


async def _auto_approve_enabled(agent_id: object) -> bool:
    """Read the agent-level approval-mode switch (default manual)."""

    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
        return bool(agent is not None and agent.subagent_evolution_auto_approve)


async def _notify_owner(agent_id: object, spec_name: str, proposal_id: str, *, applied: bool = False) -> None:
    """Notify the agent owner: proposal awaiting review, or auto-applied (with trail)."""

    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.services.notification_service import send_notification
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
        if agent is None:
            return
        recipient = agent.owner_user_id
        if recipient is None:
            return
        title = (
            f"Subagent '{spec_name}' definition improvement auto-applied"
            if applied
            else f"Subagent '{spec_name}' has a definition improvement awaiting review"
        )
        await send_notification(
            db,
            recipient,
            type="subagent_evolution_proposal",
            title=title,
            body=f"proposal {proposal_id}",
            link=f"/agents/{agent_id}#subagents",
        )
        await db.commit()


async def maybe_nominate(
    *,
    agent_id: object,
    spec_name: str,
    memory_store: SubagentMemoryStore,
    model_config: dict,
    threshold: int | None = None,
    agent_data_dir: Path | str | None = None,
) -> EvolutionProposal | None:
    """Evolution-loop nomination (P1): threshold-gated, agent-level-only, fail-soft.

    Returns the created proposal, or None when no nomination happened (the
    overwhelmingly normal case). Never raises — this rides the spawn path.
    """

    try:
        if threshold is None:
            from app.config import get_settings

            threshold = get_settings().SUBAGENT_EVOLUTION_THRESHOLD

        # Agent-level definitions only: tenant/builtin/inline have no file here.
        definition_store = definition_store_for_agent(agent_id, agent_data_dir=agent_data_dir)
        spec = definition_store.load(spec_name)
        if spec is None:
            return None

        if memory_store.count_active_entries(spec_name) < threshold:
            return None

        proposal_store = proposal_store_for_agent(agent_id, agent_data_dir=agent_data_dir)
        if proposal_store.load_pending(spec_name) is not None:
            return None

        # Draft-budget coherence guard: the drafter must return the REWRITTEN
        # body in full within its 8192-token output budget. A body past ~24K
        # chars (~6K tokens) cannot round-trip — skip loudly instead of
        # burning an LLM call into a guaranteed parse failure on every
        # distillation tick. Owner remedy: prune the definition manually.
        if len(spec.system_prompt) > 24_000:
            logger.warning(
                "[SubagentEvolution] %s body too large for the draft budget (%d chars) — nomination skipped",
                spec_name,
                len(spec.system_prompt),
            )
            return None

        active_memory = memory_store.load(spec_name, active_only=True)
        try:
            from app.services.tenant_resolver import resolve_tenant_for_agent

            tenant_id = await resolve_tenant_for_agent(agent_id)
        except Exception:  # noqa: BLE001 — metering context must not block nomination
            tenant_id = None
        draft = await draft_improvement(
            definition_body=spec.system_prompt,
            active_memory=active_memory,
            model_config=model_config,
            agent_id=agent_id,
            tenant_id=tenant_id,
        )
        if draft is None:
            return None

        # Only ids that actually exist in active memory count as absorbed.
        valid_ids = _active_entry_ids(active_memory)
        absorbed = [i for i in draft["absorbed_entry_ids"] if i in valid_ids]
        if not absorbed:
            return None

        # The revised definition must round-trip the SAME validation chain PUT
        # uses — base frontmatter + new body (contract freeze by construction).
        from dataclasses import replace

        from app.agents.subagent_definition import parse_subagent_definition

        revised_spec = replace(spec, system_prompt=draft["body"])
        parse_subagent_definition(render_subagent_definition(revised_spec))

        base_text = render_subagent_definition(spec)
        proposal = EvolutionProposal(
            name=spec_name,
            status="pending",
            base_definition_sha=definition_sha(base_text),
            absorbed_entry_ids=absorbed,
            rationale=draft["rationale"],
            created_at=datetime.now(timezone.utc).isoformat(),
            proposal_id=uuid_mod.uuid4().hex[:12],
            body=draft["body"],
        )
        proposal_store.save(proposal)
        logger.info(
            "[SubagentEvolution] proposal %s nominated: agent=%s name=%s absorbed=%d",
            proposal.proposal_id,
            agent_id,
            spec_name,
            len(absorbed),
        )

        # Auto mode (§4.3): apply immediately through the SAME path a human
        # approval takes — only the click is skipped. Any failure leaves the
        # proposal pending for manual review.
        applied = False
        try:
            if await _auto_approve_enabled(agent_id):
                result = apply_proposal(agent_id, spec_name, approved_by="auto", agent_data_dir=agent_data_dir)
                applied = result.applied
                if not applied:
                    logger.warning(
                        "[SubagentEvolution] auto-approve failed (%s) — proposal %s stays pending",
                        result.error,
                        proposal.proposal_id,
                    )
        except Exception as exc:
            logger.warning("[SubagentEvolution] auto-approve check failed (non-fatal): %s", exc)

        try:
            await _notify_owner(agent_id, spec_name, proposal.proposal_id, applied=applied)
        except Exception as exc:
            logger.warning("[SubagentEvolution] owner notification failed (non-fatal): %s", exc)
        return proposal
    except Exception as exc:
        logger.warning("[SubagentEvolution] nomination skipped (non-fatal): name=%s err=%s", spec_name, exc)
        return None
