"""Owner/company charter contracts for agent accountability."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.action_preflight import CharterZone
from app.services.principal_context import Principal, PrincipalRole, PrincipalStack


@dataclass(frozen=True, slots=True)
class CompanyCharter:
    company_id: str
    company_name: str
    goals: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()
    escalation_targets: tuple[str, ...] = ()

    @classmethod
    def default(cls, *, company_id: str, company_name: str) -> "CompanyCharter":
        return cls(
            company_id=company_id,
            company_name=company_name,
            goals=(
                "Protect company data boundaries, compliance posture, and reputation.",
                "Support cross-team work without bypassing platform governance.",
            ),
            boundaries=(
                "share credentials",
                "bypass company policy",
                "expose PL3 or PL4 sensitive data outside authorized channels",
            ),
            escalation_targets=("company_admin",),
        )

    def conflicts_with(self, action: str) -> bool:
        return any(_matches_clause(action, boundary) for boundary in self.boundaries)

    @property
    def primary_escalation_target(self) -> str | None:
        return self.escalation_targets[0] if self.escalation_targets else None


@dataclass(frozen=True, slots=True)
class OwnerAgencyCharter:
    owner_id: str
    owner_name: str
    full_authority: tuple[str, ...] = ()
    confirm_first: tuple[str, ...] = ()
    never_do: tuple[str, ...] = ()
    default_style: str = "Prepare evidence, state risks, and ask before external or irreversible action."

    @classmethod
    def default(cls, *, owner_id: str, owner_name: str) -> "OwnerAgencyCharter":
        return cls(
            owner_id=owner_id,
            owner_name=owner_name,
            full_authority=(
                "prepare local draft",
                "summarize internal context",
                "run read-only check",
            ),
            confirm_first=(
                "send external message",
                "make production change",
                "commit or deploy change",
                "spend money",
            ),
            never_do=(
                "share credentials",
                "bypass company policy",
            ),
        )

    def zone_for(self, action: str) -> CharterZone:
        if any(_matches_clause(action, clause) for clause in self.never_do):
            return CharterZone.NEVER_DO
        if any(_matches_clause(action, clause) for clause in self.confirm_first):
            return CharterZone.CONFIRM_FIRST
        if any(_matches_clause(action, clause) for clause in self.full_authority):
            return CharterZone.FULL_AUTHORITY
        return CharterZone.CONFIRM_FIRST


@dataclass(frozen=True, slots=True)
class ActionPosture:
    charter_zone: CharterZone
    company_boundary_conflict: bool = False
    escalation_target: str | None = None
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AgentAccountabilityContext:
    principal_stack: PrincipalStack
    company_charter: CompanyCharter
    owner_charter: OwnerAgencyCharter

    def identity_statement(self) -> str:
        return (
            f"I directly support {self.owner_charter.owner_name} "
            f"within {self.company_charter.company_name}'s company charter."
        )

    def action_posture(self, action: str) -> ActionPosture:
        zone = self.owner_charter.zone_for(action)
        company_conflict = self.company_charter.conflicts_with(action)
        reasons: list[str] = [f"owner_charter:{zone.value}"]
        escalation_target = None
        if company_conflict:
            reasons.append("company_boundary_conflict")
            escalation_target = self.company_charter.primary_escalation_target
        return ActionPosture(
            charter_zone=zone,
            company_boundary_conflict=company_conflict,
            escalation_target=escalation_target,
            reasons=reasons,
        )


def build_default_accountability_context(
    *,
    company_id: str,
    company_name: str,
    owner_id: str,
    owner_name: str,
    current_user_id: str | None = None,
    current_user_name: str | None = None,
    creator_id: str | None = None,
    creator_name: str | None = None,
    delegating_agent_id: str | None = None,
    delegating_agent_name: str | None = None,
) -> AgentAccountabilityContext:
    stack = PrincipalStack(
        platform=Principal(PrincipalRole.PLATFORM, "hive", "Hive"),
        company=Principal(PrincipalRole.COMPANY, company_id, company_name),
        direct_owner=Principal(PrincipalRole.OWNER, owner_id, owner_name),
        creator=Principal(PrincipalRole.CREATOR, creator_id, creator_name or creator_id or "") if creator_id else None,
        current_user=Principal(
            PrincipalRole.CURRENT_USER,
            current_user_id,
            current_user_name or current_user_id or "",
        )
        if current_user_id
        else None,
        delegating_agent=Principal(
            PrincipalRole.DELEGATING_AGENT,
            delegating_agent_id,
            delegating_agent_name or delegating_agent_id or "",
        )
        if delegating_agent_id
        else None,
    )
    return AgentAccountabilityContext(
        principal_stack=stack,
        company_charter=CompanyCharter.default(company_id=company_id, company_name=company_name),
        owner_charter=OwnerAgencyCharter.default(owner_id=owner_id, owner_name=owner_name),
    )


def _matches_clause(action: str, clause: str) -> bool:
    """Match typed charter action identifiers, never natural-language similarity."""
    normalized_action = _normalize(action)
    normalized_clause = _normalize(clause)
    return bool(normalized_action and normalized_clause and normalized_action == normalized_clause)


def _normalize(text: str) -> str:
    return " ".join(str(text).lower().replace("_", " ").replace("-", " ").split())
