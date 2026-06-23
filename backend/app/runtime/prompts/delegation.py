"""Prompt text for subagent, peer delegation, and team handoff briefs."""

from __future__ import annotations


DELEGATION_BRIEF_CONTRACT = (
    "Goal / Context / Known facts / Constraints / Evidence needed / Output / Stop condition. "
    "Use this structure for delegated work so the worker can run independently, return a useful "
    "digest, and stop at the right boundary. Do not ask the worker to infer missing scope silently; "
    "name assumptions or ask the parent/user before delegating if the ambiguity changes the outcome."
)


def build_delegation_brief_contract() -> str:
    return DELEGATION_BRIEF_CONTRACT
