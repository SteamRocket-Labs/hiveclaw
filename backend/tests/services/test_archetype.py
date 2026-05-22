"""Phase 12: HR archetype inference + defaults."""

from __future__ import annotations

from app.services.archetype import (
    Archetype,
    apply_archetype_defaults,
    default_company_charter,
    default_owner_charter,
    infer_archetype,
)


class TestInferArchetype:
    def test_research_analyst(self) -> None:
        assert (
            infer_archetype(
                role_description="research analyst tracking sector trends",
                primary_users=["partner"],
                core_outputs=["weekly research memos", "competitor landscape"],
            )
            == Archetype.RESEARCH_ANALYST
        )

    def test_chief_of_staff(self) -> None:
        assert (
            infer_archetype(
                role_description="chief of staff coordinating leadership cadence",
                primary_users=["ceo"],
                core_outputs=["weekly leadership digest", "OKR tracking"],
            )
            == Archetype.CHIEF_OF_STAFF
        )

    def test_customer_success(self) -> None:
        assert (
            infer_archetype(
                role_description="customer success replying to ticket escalations",
                primary_users=["enterprise customers"],
                core_outputs=["ticket triage", "renewal risk notes"],
            )
            == Archetype.CUSTOMER_SUCCESS
        )

    def test_engineering_assistant(self) -> None:
        assert (
            infer_archetype(
                role_description="engineering assistant reviewing pull requests",
                primary_users=["staff engineer"],
                core_outputs=["PR review notes", "deploy checklists"],
            )
            == Archetype.ENGINEERING_ASSISTANT
        )

    def test_falls_back_to_generalist(self) -> None:
        assert (
            infer_archetype(role_description="something niche", primary_users=[], core_outputs=[])
            == Archetype.GENERALIST
        )


class TestDefaultOwnerCharter:
    def test_research_analyst_charter_keeps_publishing_confirm_first(self) -> None:
        charter = default_owner_charter(Archetype.RESEARCH_ANALYST)
        assert "full_authority" in charter
        assert "confirm_first" in charter
        assert "never_do" in charter
        confirm_text = " ".join(charter["confirm_first"]).lower()
        assert "external" in confirm_text or "publish" in confirm_text

    def test_customer_success_never_committing_refunds_alone(self) -> None:
        charter = default_owner_charter(Archetype.CUSTOMER_SUCCESS)
        never_text = " ".join(charter["never_do"]).lower()
        assert "refund" in never_text or "commit" in never_text


class TestDefaultCompanyCharter:
    def test_company_charter_carries_goals_and_boundaries(self) -> None:
        charter = default_company_charter(Archetype.CHIEF_OF_STAFF)
        assert charter["goals"]
        assert charter["boundaries"]
        assert charter["escalation"]


class TestApplyArchetypeDefaults:
    def test_fills_missing_charter_from_archetype(self) -> None:
        blueprint = {
            "role_description": "research analyst tracking emerging markets",
            "primary_users": ["partner"],
            "core_outputs": ["weekly research memos"],
            "owner_agency_charter": {},
            "company_charter": {},
        }
        applied = apply_archetype_defaults(blueprint)
        assert applied["archetype"] == Archetype.RESEARCH_ANALYST.value
        assert applied["owner_agency_charter"]["full_authority"]
        assert applied["owner_agency_charter"]["confirm_first"]
        assert applied["company_charter"]["goals"]

    def test_explicit_charter_overrides_archetype(self) -> None:
        blueprint = {
            "role_description": "research analyst tracking emerging markets",
            "primary_users": ["partner"],
            "core_outputs": ["weekly research memos"],
            "owner_agency_charter": {
                "full_authority": ["draft sector summaries"],
                "confirm_first": ["share with LPs"],
                "never_do": ["sign NDAs"],
            },
            "company_charter": {},
        }
        applied = apply_archetype_defaults(blueprint)
        assert applied["owner_agency_charter"]["full_authority"] == ["draft sector summaries"]
        assert applied["owner_agency_charter"]["confirm_first"] == ["share with LPs"]
        assert applied["company_charter"]["goals"]
