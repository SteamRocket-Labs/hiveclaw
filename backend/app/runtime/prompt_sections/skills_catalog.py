"""§ Skills Catalog section — progressive disclosure skill index."""

from __future__ import annotations


def build_skills_catalog_section(skills_text: str = "", budget_chars: int = 4000) -> str:
    """Build the skill catalog section.

    Args:
        skills_text: Pre-loaded skills index (name + summary lines).
        budget_chars: Maximum character budget for the catalog.
    """
    if not skills_text:
        return ""

    truncated = skills_text
    if len(truncated) > budget_chars:
        truncated = truncated[:budget_chars] + "\n\n...(skill catalog truncated — use `load_skill` to see full details)"

    return f"""\
## Skills

{truncated}

Use `load_skill(name)` to load a skill's full instructions before using it. Do NOT guess what a skill contains."""
