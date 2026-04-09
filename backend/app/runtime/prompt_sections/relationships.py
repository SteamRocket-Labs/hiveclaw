"""§ Relationships section — colleague agents, organization structure."""

from __future__ import annotations


def build_relationships_section(
    relationships_text: str = "",
    org_structure_text: str = "",
    company_info_text: str = "",
) -> str:
    """Build the relationships and organizational context section.

    Args:
        relationships_text: Content from relationships.md (already stripped of heading).
        org_structure_text: Organization structure text.
        company_info_text: Company information text.
    """
    parts: list[str] = []

    if company_info_text:
        parts.append(f"### Company Information\n{company_info_text}")

    if org_structure_text:
        parts.append(f"### Organization Structure\n{org_structure_text}")

    if relationships_text and "暂无" not in relationships_text and "None yet" not in relationships_text:
        parts.append(f"### Relationships\n{relationships_text}")

    if not parts:
        return ""
    # NOTE: No "## Context Material" wrapper — agent_context.py provides that.
    return "\n\n".join(parts)
