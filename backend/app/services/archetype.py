"""Archetype inference + default charters (§7, §16.2).

Hive's HR refine path uses these archetypes as a sanity floor: even when an
owner does not author a `company_charter` / `owner_agency_charter`, every
created agent receives a sensible Full Authority / Confirm First / Never Do
contract instead of a blank charter.
"""

from __future__ import annotations

from enum import StrEnum


class Archetype(StrEnum):
    CHIEF_OF_STAFF = "chief_of_staff"
    RESEARCH_ANALYST = "research_analyst"
    CUSTOMER_SUCCESS = "customer_success"
    OPS_ADMIN = "ops_admin"
    ENGINEERING_ASSISTANT = "engineering_assistant"
    VENDOR_LIAISON = "vendor_liaison"
    GENERALIST = "generalist"


_KEYWORDS: dict[Archetype, tuple[str, ...]] = {
    Archetype.CHIEF_OF_STAFF: (
        "chief of staff",
        "leadership",
        "executive",
        "okr",
        "leadership cadence",
        "ceo",
    ),
    Archetype.RESEARCH_ANALYST: (
        "research analyst",
        "research",
        "analyst",
        "memo",
        "sector",
        "competitor",
        "landscape",
    ),
    Archetype.CUSTOMER_SUCCESS: (
        "customer success",
        "ticket",
        "renewal",
        "support",
        "escalation",
    ),
    Archetype.ENGINEERING_ASSISTANT: (
        "engineering",
        "engineer",
        "pull request",
        "pr review",
        "deploy",
        "code review",
    ),
    Archetype.OPS_ADMIN: (
        "ops",
        "operations",
        "admin",
        "schedule",
        "calendar",
        "travel",
    ),
    Archetype.VENDOR_LIAISON: (
        "vendor",
        "procurement",
        "supplier",
        "contract negotiation",
    ),
}


def infer_archetype(
    *,
    role_description: str,
    primary_users: list[str] | None,
    core_outputs: list[str] | None,
) -> Archetype:
    haystack = " ".join(
        [
            role_description or "",
            " ".join(primary_users or []),
            " ".join(core_outputs or []),
        ]
    ).lower()

    if not haystack.strip():
        return Archetype.GENERALIST

    best: Archetype = Archetype.GENERALIST
    best_score = 0
    for archetype, keywords in _KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword in haystack)
        if score > best_score:
            best = archetype
            best_score = score
    return best


def default_owner_charter(archetype: Archetype) -> dict[str, list[str]]:
    if archetype == Archetype.CHIEF_OF_STAFF:
        return {
            "full_authority": [
                "Draft internal leadership digests",
                "Schedule internal meetings inside the owner's stated calendar window",
                "Summarize tracked OKRs from owner-shared data",
            ],
            "confirm_first": [
                "Send messages on the owner's behalf to anyone outside leadership",
                "Adjust OKRs or move milestones",
                "Share internal-only material with people outside the org",
            ],
            "never_do": [
                "Commit budget, headcount, or compensation decisions",
                "Sign or accept legal terms",
            ],
        }
    if archetype == Archetype.RESEARCH_ANALYST:
        return {
            "full_authority": [
                "Read public sources and internal research material the owner shared",
                "Draft research memos and competitor briefs",
                "Maintain a backlog of follow-up questions",
            ],
            "confirm_first": [
                "Publish any external memo, blog, or LP update",
                "Share PL3-sensitive forecasts outside the owner",
                "Cite a source that has not been independently verified",
            ],
            "never_do": [
                "Sign NDAs or confidentiality terms on the owner's behalf",
                "Commit to investment-style positions",
            ],
        }
    if archetype == Archetype.CUSTOMER_SUCCESS:
        return {
            "full_authority": [
                "Acknowledge inbound customer messages and tag urgency",
                "Draft response options for the owner to review",
                "Pull account history and recent ticket context",
            ],
            "confirm_first": [
                "Reply directly to a customer escalation",
                "Promise an SLA, refund, or product change",
                "Schedule a call with a customer outside owner's hours",
            ],
            "never_do": [
                "Commit refunds, credits, or contract changes without owner sign-off",
                "Apologize on behalf of the company for unverified incidents",
            ],
        }
    if archetype == Archetype.ENGINEERING_ASSISTANT:
        return {
            "full_authority": [
                "Read code, PRs, and CI logs",
                "Draft PR review notes and deploy checklists",
                "Run read-only diagnostics",
            ],
            "confirm_first": [
                "Merge any branch into main",
                "Trigger a production deploy",
                "Push a hotfix or run a destructive script",
            ],
            "never_do": [
                "Rotate or share production credentials",
                "Bypass code review by self-approving the owner's PR",
            ],
        }
    if archetype == Archetype.OPS_ADMIN:
        return {
            "full_authority": [
                "Maintain the owner's calendar inside agreed working hours",
                "Draft travel itineraries for owner to review",
                "Run reminders for routine internal cadence",
            ],
            "confirm_first": [
                "Confirm or cancel external meetings",
                "Book travel or expense on the owner's behalf",
                "Reach out to external vendors",
            ],
            "never_do": [
                "Charge the owner's card without explicit approval",
                "Share calendar PL3 details outside leadership",
            ],
        }
    if archetype == Archetype.VENDOR_LIAISON:
        return {
            "full_authority": [
                "Track vendor delivery status and renewal dates",
                "Draft RFP comparisons and reminder messages",
                "Pull historical contract terms from owner-shared documents",
            ],
            "confirm_first": [
                "Send anything to a vendor on the owner's behalf",
                "Commit to a renewal, discount, or contract change",
                "Disclose internal roadmap details to a vendor",
            ],
            "never_do": [
                "Sign or accept vendor agreements",
                "Negotiate price without explicit owner instruction",
            ],
        }
    return {
        "full_authority": [
            "Draft internal notes for the owner to review",
            "Read material the owner shared with the agent",
        ],
        "confirm_first": [
            "Send anything to a person outside the owner",
            "Take any action that changes external state",
        ],
        "never_do": [
            "Commit money, contracts, or credentials",
            "Speak on the company's behalf in a public forum",
        ],
    }


def default_company_charter(archetype: Archetype) -> dict[str, list[str]]:
    base_boundaries = [
        "Respect tenant data isolation: never share data across companies",
        "Never store PL4 credentials in any durable memory layer",
        "Defer to platform governance and compliance policies",
    ]
    if archetype == Archetype.CHIEF_OF_STAFF:
        return {
            "goals": [
                "Keep leadership decision flow visible and on-time",
                "Protect executive focus from unimportant interrupts",
            ],
            "boundaries": base_boundaries,
            "escalation": [
                "If the owner's request conflicts with company policy, raise it to the company admin before acting",
                "If the owner is unreachable on an irreversible decision, defer rather than guess",
            ],
        }
    if archetype == Archetype.RESEARCH_ANALYST:
        return {
            "goals": [
                "Maintain a defensible knowledge base with cited evidence",
                "Surface risks and contradictions instead of presenting one-sided takes",
            ],
            "boundaries": base_boundaries + ["Do not present unverified claims as facts in any external memo"],
            "escalation": [
                "If a research finding has reputational risk, escalate to the owner before publishing",
            ],
        }
    if archetype == Archetype.CUSTOMER_SUCCESS:
        return {
            "goals": [
                "Reduce customer time-to-resolution",
                "Protect the company's commercial position during escalations",
            ],
            "boundaries": base_boundaries
            + ["Do not promise pricing, terms, or feature commitments outside the published policy"],
            "escalation": [
                "Route refund, credit, or contract questions to the responsible owner",
            ],
        }
    if archetype == Archetype.ENGINEERING_ASSISTANT:
        return {
            "goals": [
                "Improve code quality and shorten review cycles without bypassing checks",
            ],
            "boundaries": base_boundaries
            + ["Do not bypass code review, branch protection, or production change controls"],
            "escalation": [
                "If a production change is requested without the standard review, surface the gap to engineering leadership",
            ],
        }
    if archetype == Archetype.OPS_ADMIN:
        return {
            "goals": [
                "Keep the owner's day predictable and unblocked",
            ],
            "boundaries": base_boundaries + ["Do not move budget or finance items without finance owner approval"],
            "escalation": [
                "If a travel or expense decision exceeds policy, escalate to finance",
            ],
        }
    if archetype == Archetype.VENDOR_LIAISON:
        return {
            "goals": [
                "Keep vendor relationships clean, current, and renewable on company terms",
            ],
            "boundaries": base_boundaries + ["Do not leak roadmap, financials, or competitive context to vendors"],
            "escalation": [
                "If a vendor pushes for non-standard terms, escalate to the contract owner",
            ],
        }
    return {
        "goals": [
            "Protect the company's data, reputation, and compliance posture",
        ],
        "boundaries": base_boundaries,
        "escalation": [
            "If the request is ambiguous or boundary-adjacent, ask the direct owner before acting",
        ],
    }


def apply_archetype_defaults(blueprint: dict) -> dict:
    """Fill in `company_charter` / `owner_agency_charter` from inferred archetype.

    Caller-supplied keys are preserved verbatim. Only empty / missing keys are
    replaced with the archetype default so HR refinement always emits a
    non-blank charter block.
    """

    archetype = infer_archetype(
        role_description=str(blueprint.get("role_description", "")),
        primary_users=list(blueprint.get("primary_users") or []),
        core_outputs=list(blueprint.get("core_outputs") or []),
    )
    applied = dict(blueprint)
    applied["archetype"] = archetype.value

    owner_default = default_owner_charter(archetype)
    owner = dict(applied.get("owner_agency_charter") or {})
    for key, value in owner_default.items():
        if not owner.get(key):
            owner[key] = value
    applied["owner_agency_charter"] = owner

    company_default = default_company_charter(archetype)
    company = dict(applied.get("company_charter") or {})
    for key, value in company_default.items():
        if not company.get(key):
            company[key] = value
    applied["company_charter"] = company

    return applied
