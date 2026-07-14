"""§ Skills Catalog section — progressive disclosure skill index."""

from __future__ import annotations


def build_skills_catalog_section(skills_text: str = "", budget_chars: int = 4000) -> str:
    """Build the complete skill catalog section.

    Args:
        skills_text: Pre-loaded skills index (name + summary lines).
        budget_chars: Advisory compatibility argument; never used to prune
            model-visible discovery metadata.
    """
    if not skills_text:
        return ""

    del budget_chars

    return f"""\
## Skills

{skills_text}

Skills are progressive-disclosure capability capsules. A folder-based skill can package references, \
templates, scripts, workflow definitions, and subagent definitions; loading it adds context and guidance only. \
Executable components still run through their governed runtime: workflows via `preview_workflow`/`start_workflow`, \
subagents via `spawn_subagent`/`delegate_to_agent`, and scripts through approved sandbox/code execution.

Use `load_skill(name)` only when you need the skill's method/component guide, decision rules, or examples. \
Do NOT load a skill before simple direct tool calls, and do NOT guess what a skill contains."""
