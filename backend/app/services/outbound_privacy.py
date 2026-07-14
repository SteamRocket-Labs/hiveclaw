"""Channel outbound redact (§16.2, §18 acceptance criterion #11).

Final-mile gate that runs against every text leaving Hive over an external
channel. PL3/PL4 content must never be sent over Feishu/Slack/Telegram/etc.
without an explicit principal that can see it; PL4 is rejected unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.principal_context import PrincipalStack
from app.services.privacy_layer import PrivacyLayer, PrivacyStore, SensitivityLevel


_OWNER_PRIVATE_CHANNELS = frozenset({"web"})
_SENSITIVITY_RANK = {
    SensitivityLevel.PL1_PUBLIC: 1,
    SensitivityLevel.PL2_PII: 2,
    SensitivityLevel.PL3_SENSITIVE: 3,
    SensitivityLevel.PL4_CREDENTIAL: 4,
}


@dataclass(slots=True)
class OutboundRedactDecision:
    text: str
    sensitivity: SensitivityLevel
    rejected: bool = False
    reason: str = ""
    placeholders: dict[str, str] = field(default_factory=dict)


def redact_outbound(
    text: str,
    *,
    channel: str,
    principal_stack: PrincipalStack | None = None,
    layer: PrivacyLayer | None = None,
    declared_sensitivity: SensitivityLevel | str | None = None,
) -> OutboundRedactDecision:
    """Apply PL1-PL4 strip before content leaves Hive.

    Exact credentials and PII are detected mechanically. Semantic sensitivity
    such as PL3 must be supplied as typed provenance by the caller; prose
    keywords never become policy decisions. PL4 is rejected. PL3 is allowed
    only when the channel is
    owner-private (e.g. internal `web` chat) and the current user is the
    direct owner or a company admin; otherwise it is replaced with
    `[REDACTED_PL3]`. PL2 PII is replaced with typed placeholders unless the
    principal stack explicitly authorizes the original value (which today is
    never — outbound always masks PII).
    """

    privacy = layer or PrivacyLayer(PrivacyStore())
    classified = privacy.classify_and_mask(text)
    sensitivity = _effective_sensitivity(classified.sensitivity, declared_sensitivity)

    if sensitivity == SensitivityLevel.PL4_CREDENTIAL:
        return OutboundRedactDecision(
            text=(
                classified.sanitized_text
                if classified.sensitivity == SensitivityLevel.PL4_CREDENTIAL
                else "[REDACTED_PL4]"
            ),
            sensitivity=SensitivityLevel.PL4_CREDENTIAL,
            rejected=True,
            reason="PL4 credential outbound blocked",
            placeholders=classified.placeholders,
        )

    if sensitivity == SensitivityLevel.PL3_SENSITIVE:
        if (
            channel in _OWNER_PRIVATE_CHANNELS
            and principal_stack is not None
            and (principal_stack.current_user_is_direct_owner or principal_stack.current_user_is_company_admin)
        ):
            return OutboundRedactDecision(
                text=text,
                sensitivity=SensitivityLevel.PL3_SENSITIVE,
                placeholders=classified.placeholders,
            )
        return OutboundRedactDecision(
            text="[REDACTED_PL3]",
            sensitivity=SensitivityLevel.PL3_SENSITIVE,
            reason="PL3 content stripped for external channel",
            placeholders=classified.placeholders,
        )

    if sensitivity == SensitivityLevel.PL2_PII:
        return OutboundRedactDecision(
            text=classified.sanitized_text,
            sensitivity=SensitivityLevel.PL2_PII,
            placeholders=classified.placeholders,
        )

    return OutboundRedactDecision(
        text=text,
        sensitivity=SensitivityLevel.PL1_PUBLIC,
    )


def _effective_sensitivity(
    detected: SensitivityLevel,
    declared: SensitivityLevel | str | None,
) -> SensitivityLevel:
    if declared is None:
        return detected
    try:
        declared_level = declared if isinstance(declared, SensitivityLevel) else SensitivityLevel(str(declared))
    except ValueError as exc:
        raise ValueError(f"invalid declared_sensitivity: {declared!r}") from exc
    return declared_level if _SENSITIVITY_RANK[declared_level] > _SENSITIVITY_RANK[detected] else detected
