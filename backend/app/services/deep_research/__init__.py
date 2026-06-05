"""First-class Deep Research engine — workflow-native (DR-6b, single path).

The pack-level skill is only the router. This package owns the executable
research capability as workflow leaf presets: planning brief, governed
explorer fan-out with source ledgering, per-claim adversarial critique, and
gated synthesis with artifact landing. The legacy linear/controller/worker
orchestrator was retired in DR-6b (docs/deep-research-workflow-unification.md).
"""

from app.services.deep_research.schemas import ResearchRequest, ResearchRun
from app.services.deep_research.workflow_definition import (
    DEEP_RESEARCH_WORKFLOW_NAME,
    build_deep_research_workflow_definition,
    start_deep_research_workflow_run,
)

__all__ = [
    "DEEP_RESEARCH_WORKFLOW_NAME",
    "ResearchRequest",
    "ResearchRun",
    "build_deep_research_workflow_definition",
    "start_deep_research_workflow_run",
]
