"""Prompt text for subagent, peer delegation, and team handoff briefs."""

from __future__ import annotations


DELEGATION_BRIEF_CONTRACT = (
    "Goal / Context / Known facts / Constraints / Evidence needed / Output / Stop condition. "
    "Put the facts, figures, and short excerpts the worker needs into the message itself; the worker cannot read "
    "your workspace or session files, so do not point at your own paths as its sources. "
    "Write the brief and its headings in the member's language, using localized public employee names (the Chinese UI calls the built-in My Assistant 我的助理); keep internal identifiers out of public prose. "
    "Start with the task, without a sender self-introduction. The recipient keeps its own display name and identity. "
    "Use this structure for delegated work so the worker can run independently, return a useful "
    "digest, and stop at the right boundary. Do not ask the worker to infer missing scope silently; "
    "name assumptions or ask the parent/user before delegating if the ambiguity changes the outcome. "
    "If the request names existing workspace artifacts, use the cross-workspace artifact contract: "
    "explicitly name every target path and ask the worker to update the same path. This applies to "
    "presentations, spreadsheets, code files, and documents. Do not create replacement artifacts unless "
    "the user asked for separate deliverables."
)


def build_delegation_brief_contract() -> str:
    return DELEGATION_BRIEF_CONTRACT
