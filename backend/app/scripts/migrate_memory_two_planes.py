"""C7 migration: legacy four-file T3 + mixed soul → two-plane layout (spec §6.3).

Usage:
    python -m app.scripts.migrate_memory_two_planes                 # dry-run all agents
    python -m app.scripts.migrate_memory_two_planes --agent-id ID   # dry-run one agent
    python -m app.scripts.migrate_memory_two_planes --apply --confirm  # irreversible apply

Content reorganization is an LLM judgment over the FULL legacy corpus (L1):
soul purity split (应然 stays, 实然 moves to self), worker constraints split
two ways (owner-given hard constraints → soul candidates; self-observed soft
boundaries + blocked patterns → self), capabilities split three ways
(strategy/reference → knowledge pages; self-assessment → self), episodes →
milestone selection (most stay archived in T2). The platform owns atomicity,
archival (legacy files MOVE to memory/.archive/legacy_t3/<ts>/ — never
deleted), idempotency marker, and the dry-run/confirm safety gate. No model
config → held, data untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MIGRATION_MARKER_RELATIVE = Path("control") / "two_plane_migration.json"
LEGACY_T3_FILES = ("episodes.md", "user.md", "worker.md", "capabilities.md")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,78}$")
_EMPTY_SELF_MD = "# Self\n\n_No accepted self observations migrated._"
_EMPTY_SOUL_MD = "# Soul\n\n_No soul content available before migration._"

PlanBuilder = Callable[..., Awaitable[dict]]

MIGRATION_PLAN_PROMPT = """\
<role>
You are the memory migration architect for one agent. You receive the agent's
ENTIRE legacy memory corpus — soul.md plus the four flat T3 files — and you
reorganize it into the two-plane layout in ONE complete pass.
</role>

<two_plane_law>
- soul.md = 应然 (what the agent SHOULD be): identity, who it serves,
  owner-given hard boundaries, quality standards. Owner-authored voice.
  REMOVE everything 实然 (self-observed abilities, habits, failure modes,
  "how I learn") — that moves to self.
- self_md = 实然 (what the agent actually IS): capabilities with proficiency
  tier (熟练/一般/弱), methods, failure modes with lifecycle state
  (active/规避中/已根除), style. `###` entries, each with `<!-- id: ... -->`
  and evidence refs.
- profiles/owner|collaborators|domain = converged pictures of stable parties;
  user.md content usually becomes profiles/owner entries. domain holds
  domain-level taste, NOT concept facts.
- knowledge_pages = atomic concept pages (Current Claim / Scope / Evidence /
  Contradictions / Relations, >=1 relation edge — forward references count).
  capabilities.md strategy/reference/general knowledge lands here.
- milestone_pages = FEW narrative anchors (owner feedback, major
  failure/rework, first success). Most episodes do NOT become milestones —
  their evidence stays reachable through T2 archives.
- worker.md constraints split two ways: owner-GIVEN hard constraints belong
  to soul (应然); self-OBSERVED soft boundaries and blocked patterns belong
  to self (实然). The test: "someone required this of me" → soul;
  "I noticed this about myself" → self.
</two_plane_law>

<output_contract>
Return ONE raw JSON object, no fences, no prose:
{
  "soul_md": "...purified soul, whole file...",
  "self_md": "...whole file, ### entries with <!-- id: --> anchors...",
  "profiles": {"owner": "...", "collaborators": "...", "domain": "..."},
  "knowledge_pages": [{"slug": "kebab-case", "content": "...whole page..."}],
  "milestone_pages": [{"slug": "ms-kebab-case", "content": "...whole page..."}],
  "notes": "what went where and why, one paragraph"
}
Preserve every evidence ref you can see (t2://... URIs may be cited as t2-
short ids). Never invent claims that are not in the corpus. Empty string for
a profiles file with nothing to hold.
</output_contract>
"""


@dataclass(frozen=True, slots=True)
class MigrationReport:
    agent_id: str
    status: str  # planned | applied | held | already_migrated | no_legacy_data
    plan_path: Path | None = None
    archived_to: str = ""
    issues: tuple[str, ...] = ()


async def migrate_agent_memory(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    apply: bool = False,
    plan_builder: PlanBuilder | None = None,
) -> MigrationReport:
    root = Path(data_root)
    agent_root = root / str(agent_id)
    mem_dir = agent_root / "memory"
    marker_path = mem_dir / MIGRATION_MARKER_RELATIVE

    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            marker = {}
        if marker.get("status") == "applied":
            return MigrationReport(agent_id=str(agent_id), status="already_migrated")

    corpus = _load_legacy_corpus(agent_root)
    if not corpus["t3_files"]:
        return MigrationReport(agent_id=str(agent_id), status="no_legacy_data")

    if plan_builder is None:
        return MigrationReport(
            agent_id=str(agent_id),
            status="held",
            issues=("no plan builder / model config available — migration is an LLM judgment, no mechanical fallback",),
        )

    plan = _fill_no_claim_scaffolds(await plan_builder(corpus), corpus)
    issues = _validate_plan(plan)
    staging_dir = mem_dir / ".staging" / "migration" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    staging_dir.mkdir(parents=True, exist_ok=True)
    plan_path = staging_dir / "migration_plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if issues:
        (staging_dir / "issues.json").write_text(
            json.dumps({"issues": issues}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return MigrationReport(agent_id=str(agent_id), status="held", plan_path=plan_path, issues=tuple(issues))

    if not apply:
        return MigrationReport(agent_id=str(agent_id), status="planned", plan_path=plan_path)

    archive_dir = mem_dir / ".archive" / "legacy_t3" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_dir.mkdir(parents=True, exist_ok=True)

    _write_file(mem_dir / "self" / "self.md", str(plan["self_md"]))
    for name in ("owner", "collaborators", "domain"):
        content = str((plan.get("profiles") or {}).get(name) or "").strip()
        if content:
            _write_file(mem_dir / "profiles" / f"{name}.md", content)
    for page in plan.get("knowledge_pages") or []:
        _write_file(mem_dir / "knowledge" / f"{page['slug']}.md", str(page["content"]))
    for page in plan.get("milestone_pages") or []:
        _write_file(mem_dir / "milestones" / f"{page['slug']}.md", str(page["content"]))

    soul_path = agent_root / "soul.md"
    if soul_path.exists():
        _archive_file(soul_path, archive_dir / "soul.md", keep_original=True)
    _write_file(soul_path, str(plan["soul_md"]))
    for filename in LEGACY_T3_FILES:
        legacy_path = mem_dir / "t3" / filename
        if legacy_path.exists():
            _archive_file(legacy_path, archive_dir / filename, keep_original=False)

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {
                "schema": "two_plane_migration.v1",
                "agent_id": str(agent_id),
                "status": "applied",
                "applied_at": datetime.now(UTC).isoformat(),
                "plan_path": str(plan_path),
                "archived_to": str(archive_dir),
                "notes": str(plan.get("notes") or ""),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        from app.memory.reference_index import rebuild_reference_index

        rebuild_reference_index(agent_id=agent_id, data_root=root)
    except Exception as exc:  # noqa: BLE001 - index is derived; a rebuild failure must not fail the migration
        logger.warning("Post-migration index rebuild failed for %s: %s", agent_id, exc)
    return MigrationReport(agent_id=str(agent_id), status="applied", plan_path=plan_path, archived_to=str(archive_dir))


async def build_migration_plan_with_llm(
    corpus: dict, *, model_config: dict, agent_id: str, tenant_id: str | None
) -> dict:
    """One LLM call over the whole legacy corpus (L1: full visibility, ample output)."""
    from app.services.llm_client import LLMMessage, create_llm_client_from_config, with_llm_usage_context

    client = create_llm_client_from_config(
        with_llm_usage_context(
            model_config,
            source="two_plane_migration",
            agent_id=agent_id,
            tenant_id=tenant_id,
        )
    )
    try:
        response = await client.stream(
            messages=[
                LLMMessage(role="system", content=MIGRATION_PLAN_PROMPT),
                LLMMessage(role="user", content=json.dumps(corpus, ensure_ascii=False, indent=2)),
            ],
            max_tokens=16_384,
            temperature=0.2,
        )
        content = response.content or ""
        start = content.find("{")
        end = content.rfind("}") + 1
        if start < 0 or end <= start:
            raise ValueError("migration plan response contained no JSON object")
        return json.loads(content[start:end])
    finally:
        await client.close()


def _load_legacy_corpus(agent_root: Path) -> dict:
    mem_dir = agent_root / "memory"
    t3_files: dict[str, str] = {}
    for filename in LEGACY_T3_FILES:
        path = mem_dir / "t3" / filename
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                t3_files[filename] = content
    soul_path = agent_root / "soul.md"
    soul = soul_path.read_text(encoding="utf-8", errors="replace") if soul_path.exists() else ""
    return {"schema": "two_plane_migration_corpus.v1", "soul_md": soul, "t3_files": t3_files}


def _validate_plan(plan: Any) -> list[str]:
    issues: list[str] = []
    if not isinstance(plan, dict):
        return ["migration plan must be a JSON object"]
    if not str(plan.get("soul_md") or "").strip():
        issues.append("plan missing soul_md")
    if not str(plan.get("self_md") or "").strip():
        issues.append("plan missing self_md")
    profiles = plan.get("profiles")
    if not isinstance(profiles, dict):
        issues.append("plan missing profiles object")
    for key, pages in (
        ("knowledge_pages", plan.get("knowledge_pages")),
        ("milestone_pages", plan.get("milestone_pages")),
    ):
        if pages is None:
            continue
        if not isinstance(pages, list):
            issues.append(f"{key} must be a list")
            continue
        for page in pages:
            slug = str((page or {}).get("slug") or "")
            if not _SLUG_RE.match(slug):
                issues.append(f"{key} slug invalid: {slug or '<missing>'}")
            if not str((page or {}).get("content") or "").strip():
                issues.append(f"{key} page content empty: {slug}")
    return issues


def _fill_no_claim_scaffolds(plan: Any, corpus: dict) -> Any:
    """Fill required files only when doing so adds no semantic claim."""
    if not isinstance(plan, dict):
        return plan
    normalized = dict(plan)
    if not str(normalized.get("self_md") or "").strip():
        normalized["self_md"] = _EMPTY_SELF_MD
    if not str(normalized.get("soul_md") or "").strip() and not str(corpus.get("soul_md") or "").strip():
        normalized["soul_md"] = _EMPTY_SOUL_MD
    return normalized


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_text(content.rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _archive_file(source: Path, target: Path, *, keep_original: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if keep_original:
        target.write_text(source.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    else:
        os.replace(source, target)


def _iter_agent_ids(data_root: Path | str) -> list[str]:
    """Return only UUID-named agent directories from the data root."""
    root = Path(data_root)
    agent_ids: list[str] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        try:
            uuid.UUID(path.name)
        except ValueError:
            logger.warning("Skipping non-agent data directory during two-plane migration: %s", path.name)
            continue
        agent_ids.append(path.name)
    return agent_ids


def _init_script_secrets_provider(settings: Any) -> None:
    """Mirror app startup secrets initialization for standalone script runs."""
    from app.services.secrets_provider import init_secrets_provider, validate_secrets_provider_config

    validate_secrets_provider_config(settings.SECRETS_MASTER_KEY or None, debug=settings.DEBUG)
    init_secrets_provider(settings.SECRETS_MASTER_KEY or None)


async def _amain() -> int:
    parser = argparse.ArgumentParser(description="Migrate agent memory to the two-plane layout (C7).")
    parser.add_argument("--agent-id", help="Migrate one agent (default: every agent under AGENT_DATA_DIR)")
    parser.add_argument("--apply", action="store_true", help="Apply the migration (default: dry-run)")
    parser.add_argument("--confirm", action="store_true", help="Required together with --apply")
    args = parser.parse_args()

    if args.apply and not args.confirm:
        print("--apply is irreversible for the live layout; add --confirm to proceed.")
        return 2

    from app.config import get_settings

    settings = get_settings()
    _init_script_secrets_provider(settings)
    data_root = Path(settings.AGENT_DATA_DIR)
    agent_ids = [args.agent_id] if args.agent_id else _iter_agent_ids(data_root)

    async def plan_builder_for(agent_id: str) -> PlanBuilder | None:
        try:
            from app.services.memory_service import _get_summary_model_config
            from app.services.tenant_resolver import resolve_tenant_for_agent

            tenant_id = await resolve_tenant_for_agent(uuid.UUID(agent_id))
            model_config = await _get_summary_model_config(tenant_id) if tenant_id else None
        except Exception as exc:  # noqa: BLE001 - a broken tenant lookup holds this agent, not the whole run
            logger.warning("Model config resolution failed for %s: %s", agent_id, exc)
            return None
        if not model_config:
            return None

        async def builder(corpus: dict) -> dict:
            return await build_migration_plan_with_llm(
                corpus, model_config=model_config, agent_id=agent_id, tenant_id=str(tenant_id)
            )

        return builder

    for agent_id in agent_ids:
        builder = await plan_builder_for(agent_id)
        report = await migrate_agent_memory(
            agent_id=agent_id, data_root=data_root, apply=args.apply, plan_builder=builder
        )
        print(f"{agent_id}: {report.status}" + (f" issues={list(report.issues)}" if report.issues else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
