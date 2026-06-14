"""CI-only behavior eval entrypoints.

This module runs inside the deployed backend, so it exercises the real
tenant-scoped runtime without requiring GitHub Actions to SSH into Railway.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.evals.hive_live_runner import (
    DETERMINISTIC_BEHAVIOR_SCENARIOS,
    _model_runtime_metadata,
    build_invoke_agent_runner,
    resolve_production_eval_runtime,
    run_hive_behavior_eval,
)


async def run_production_behavior_eval_for_ci(*, scenarios: Sequence[str] | None = None) -> dict[str, Any]:
    """Run the live behavior eval against the production runtime configured for
    the eval tenant.

    The eval tenant/model/user/agent are resolved from normal backend config.
    No provider key or model override is accepted from CI.
    """

    tenant_id = os.environ.get("HIVE_EVAL_TENANT_ID", "").strip()
    if not tenant_id:
        raise RuntimeError("HIVE_EVAL_TENANT_ID is required")

    runtime = await resolve_production_eval_runtime(
        agent_id=os.environ.get("HIVE_EVAL_AGENT_ID") or None,
        user_id=os.environ.get("HIVE_EVAL_USER_ID") or None,
        expected_tenant_id=tenant_id,
    )
    runner = build_invoke_agent_runner(
        model=runtime.model,
        fallback_model=runtime.fallback_model,
        agent_name=runtime.agent_name,
        role_description=runtime.role_description,
        agent_id=runtime.agent_id,
        user_id=runtime.user_id,
        tenant_id=runtime.tenant_id,
    )
    selected_scenarios = tuple(scenarios) if scenarios else DETERMINISTIC_BEHAVIOR_SCENARIOS
    report = await run_hive_behavior_eval(
        agent_runner=runner,
        output_dir=Path(tempfile.mkdtemp(prefix="hive-behavior-eval-ci-")),
        scenarios=selected_scenarios,
    )
    report.setdefault("runtime", {}).update(
        {
            **_model_runtime_metadata(runtime.model, source=runtime.model_source),
            "tenant_id": str(runtime.tenant_id),
            "agent_id": str(runtime.agent_id),
            "user_id": str(runtime.user_id),
        }
    )
    if runtime.fallback_model is not None:
        report["runtime"]["fallback_model"] = _model_runtime_metadata(runtime.fallback_model)
    return report
