"""Shared memory read-visibility helpers.

The MD files remain the source of truth, but every runtime/read-model/tool
consumer must apply the same principal-aware sensitivity boundary before
showing durable memory content.
"""

from __future__ import annotations

from app.services.principal_context import PrincipalStack
from app.services.privacy_layer import PrivacyLayer


def default_principal_stack(principal_stack: PrincipalStack | None = None) -> PrincipalStack:
    """No resolved principal defaults to anonymous-safe visibility."""
    return principal_stack or PrincipalStack()


def can_access_sensitivity(sensitivity: str | None, principal_stack: PrincipalStack | None = None) -> bool:
    return default_principal_stack(principal_stack).can_access_sensitivity(sensitivity or "PL1_public")


def can_access_metadata(metadata: dict | None, principal_stack: PrincipalStack | None = None) -> bool:
    metadata = metadata or {}
    return can_access_sensitivity(str(metadata.get("sensitivity") or "PL1_public"), principal_stack)


def redact_text_for_principal(
    text: str,
    sensitivity: str | None,
    principal_stack: PrincipalStack | None = None,
) -> str:
    return PrivacyLayer.redact_for_principal(
        text, sensitivity or "PL1_public", default_principal_stack(principal_stack)
    )


def classify_text_sensitivity(text: str) -> str:
    return PrivacyLayer().classify_and_mask(text).sensitivity.value


def classify_and_redact_text(
    text: str,
    principal_stack: PrincipalStack | None = None,
    *,
    sensitivity: str | None = None,
) -> tuple[str, str]:
    decision = PrivacyLayer().classify_and_mask(text)
    resolved_sensitivity = sensitivity or decision.sensitivity.value
    return (
        redact_text_for_principal(decision.sanitized_text, resolved_sensitivity, principal_stack),
        resolved_sensitivity,
    )
