"""Principal stack for owner/company-aware memory and action decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PrincipalRole(StrEnum):
    PLATFORM = "platform"
    COMPANY = "company"
    COMPANY_ADMIN = "company_admin"
    OWNER = "owner"
    CREATOR = "creator"
    CURRENT_USER = "current_user"
    DELEGATING_AGENT = "delegating_agent"


@dataclass(frozen=True, slots=True)
class Principal:
    role: PrincipalRole
    id: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class PrincipalStack:
    """Resolved accountability frame for memory visibility and action posture."""

    platform: Principal | None = None
    company: Principal | None = None
    direct_owner: Principal | None = None
    creator: Principal | None = None
    current_user: Principal | None = None
    delegating_agent: Principal | None = None

    def ordered(self) -> list[Principal]:
        return [
            principal
            for principal in (
                self.platform,
                self.company,
                self.direct_owner,
                self.creator,
                self.delegating_agent,
                self.current_user,
            )
            if principal is not None
        ]

    @property
    def direct_owner_accountability_id(self) -> str | None:
        return self.direct_owner.id if self.direct_owner else None

    @property
    def current_user_is_direct_owner(self) -> bool:
        return bool(self.current_user and self.direct_owner and self.current_user.id == self.direct_owner.id)

    @property
    def current_user_is_company_admin(self) -> bool:
        return bool(self.current_user and self.current_user.role == PrincipalRole.COMPANY_ADMIN)

    def can_access_sensitivity(self, sensitivity: str) -> bool:
        normalized = (sensitivity or "PL1_public").strip().lower()
        if normalized in {"pl1", "pl1_public", "public"}:
            return True
        if normalized in {"pl2", "pl2_pii", "pii"}:
            return True
        if normalized in {"pl3", "pl3_sensitive", "sensitive"}:
            return self.current_user_is_direct_owner or self.current_user_is_company_admin
        if normalized in {"pl4", "pl4_credential", "credential"}:
            return False
        return False
