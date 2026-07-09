"""Runtime tenant admission contracts shared by services and DB session guards."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal


RuntimeTenantAdmissionStatus = Literal["allowed", "blocked_precondition"]


@dataclass(frozen=True)
class RuntimeTenantAdmission:
    ok: bool
    tenant_id: uuid.UUID | None
    status: RuntimeTenantAdmissionStatus
    reason_code: str
    message: str
    agent_id: uuid.UUID | None
    source: str

    def metadata(self) -> dict[str, str | bool | None]:
        return {
            "tenant_admission_status": self.status,
            "precondition_status": self.status if not self.ok else None,
            "reason_code": self.reason_code,
            "source": self.source,
            "agent_id": str(self.agent_id) if self.agent_id else None,
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "ok": self.ok,
        }


class RuntimeTenantPreconditionError(RuntimeError):
    """Raised when a mutating runtime path lacks the tenant precondition."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        source: str,
        agent_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> None:
        super().__init__(message)
        self.status = "blocked_precondition"
        self.reason_code = reason_code
        self.source = source
        self.agent_id = agent_id
        self.tenant_id = tenant_id

    def metadata(self) -> dict[str, str | None]:
        return {
            "tenant_admission_status": self.status,
            "precondition_status": self.status,
            "reason_code": self.reason_code,
            "source": self.source,
            "agent_id": str(self.agent_id) if self.agent_id else None,
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
        }


def blocked_runtime_tenant_admission(
    *,
    reason_code: str,
    message: str,
    source: str,
    agent_id: uuid.UUID | None = None,
) -> RuntimeTenantAdmission:
    return RuntimeTenantAdmission(
        ok=False,
        tenant_id=None,
        status="blocked_precondition",
        reason_code=reason_code,
        message=message,
        agent_id=agent_id,
        source=source,
    )


def raise_runtime_tenant_precondition(admission: RuntimeTenantAdmission) -> None:
    raise RuntimeTenantPreconditionError(
        reason_code=admission.reason_code,
        message=admission.message,
        source=admission.source,
        agent_id=admission.agent_id,
        tenant_id=admission.tenant_id,
    )
