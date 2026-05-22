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
) -> OutboundRedactDecision:
    """Apply PL1-PL4 strip before content leaves Hive.

    PL4 credentials are rejected. PL3 is allowed only when the channel is
    owner-private (e.g. internal `web` chat) and the current user is the
    direct owner or a company admin; otherwise it is replaced with
    `[REDACTED_PL3]`. PL2 PII is replaced with typed placeholders unless the
    principal stack explicitly authorizes the original value (which today is
    never — outbound always masks PII).
    """

    privacy = layer or PrivacyLayer(PrivacyStore())
    classified = privacy.classify_and_mask(text)

    if classified.sensitivity == SensitivityLevel.PL4_CREDENTIAL:
        return OutboundRedactDecision(
            text=classified.sanitized_text,
            sensitivity=SensitivityLevel.PL4_CREDENTIAL,
            rejected=True,
            reason="PL4 credential outbound blocked",
            placeholders=classified.placeholders,
        )

    if classified.sensitivity == SensitivityLevel.PL3_SENSITIVE:
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

    if classified.sensitivity == SensitivityLevel.PL2_PII:
        return OutboundRedactDecision(
            text=classified.sanitized_text,
            sensitivity=SensitivityLevel.PL2_PII,
            placeholders=classified.placeholders,
        )

    return OutboundRedactDecision(
        text=text,
        sensitivity=SensitivityLevel.PL1_PUBLIC,
    )
