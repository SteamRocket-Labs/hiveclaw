"""Prompt section modules — structured system prompt components.

Sections rendered into the runtime prompt (via `build_frozen_prompt_prefix`
and `build_dynamic_prompt_suffix` in `prompt_builder.py`):

FROZEN PREFIX (session-stable):
  § Identity — agent name, role, personality (identity.py)
  § System — kernel execution model, governance (system.py)
  § Doing Tasks — code style, security guidance (tasks.py)
  § Using Your Tools — tool preferences (tools.py)
  § Tone and Style — output format, language (tone_style.py, includes output efficiency)
  § Skills Catalog — progressive disclosure index (skills_catalog.py)
  § Relationships — colleagues, org structure (relationships.py)

DYNAMIC SUFFIX (per-round):
  § Memory — 4-layer pyramid + current T3 snapshot (memory.py)
  § Task Playbook — task-aware scenario addenda (scenario.py)
  § Active Runtime Tool Groups — tool groups in session (active_tool_groups.py)
  § Knowledge — external knowledge retrieval (knowledge.py)
  § Environment — user, channel, timestamp (environment.py)
  § Triggers — active triggers (triggers.py)
"""

from .active_tool_groups import build_active_tool_groups_section
from .environment import build_environment_section
from .executing_actions import build_executing_actions_section
from .identity import build_identity_section
from .knowledge import build_knowledge_section
from .memory import build_memory_section
from .memory_navigation import build_memory_navigation_section
from .scenario import build_scenario_section
from .relationships import build_relationships_section
from .skills_catalog import build_skills_catalog_section
from .system import build_system_section
from .tasks import build_tasks_section
from .tone_style import build_tone_style_section
from .tools import build_tools_section
from .triggers import build_triggers_section

__all__ = [
    # Frozen prefix
    "build_identity_section",
    "build_system_section",
    "build_tasks_section",
    "build_executing_actions_section",
    "build_tools_section",
    "build_tone_style_section",
    "build_skills_catalog_section",
    "build_relationships_section",
    # Dynamic suffix
    "build_memory_section",
    "build_memory_navigation_section",
    "build_scenario_section",
    "build_active_tool_groups_section",
    "build_knowledge_section",
    "build_environment_section",
    "build_triggers_section",
]
