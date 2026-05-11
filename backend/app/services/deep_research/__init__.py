"""First-class Deep Research engine.

The pack-level skill is only the router. This package owns the executable
research workflow: planning, source discovery, fetched-source ledgering,
claim evaluation, and artifact writing.
"""

from app.services.deep_research.orchestrator import DeepResearchOrchestrator, run_deep_research
from app.services.deep_research.schemas import ResearchRequest, ResearchRun

__all__ = ["DeepResearchOrchestrator", "ResearchRequest", "ResearchRun", "run_deep_research"]
