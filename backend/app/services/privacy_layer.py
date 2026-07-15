"""Privacy and sensitivity gate for memory write/read paths."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from app.services.principal_context import PrincipalStack


class SensitivityLevel(StrEnum):
    PL1_PUBLIC = "PL1_public"
    PL2_PII = "PL2_pii"
    PL3_SENSITIVE = "PL3_sensitive"
    PL4_CREDENTIAL = "PL4_credential"


_SENSITIVITY_ALIASES: dict[str, SensitivityLevel] = {
    "public": SensitivityLevel.PL1_PUBLIC,
    "internal": SensitivityLevel.PL1_PUBLIC,
    "pl1": SensitivityLevel.PL1_PUBLIC,
    "pl1_public": SensitivityLevel.PL1_PUBLIC,
    "pii": SensitivityLevel.PL2_PII,
    "pl2": SensitivityLevel.PL2_PII,
    "pl2_pii": SensitivityLevel.PL2_PII,
    "private": SensitivityLevel.PL3_SENSITIVE,
    "confidential": SensitivityLevel.PL3_SENSITIVE,
    "secret": SensitivityLevel.PL3_SENSITIVE,
    "restricted": SensitivityLevel.PL3_SENSITIVE,
    "sensitive": SensitivityLevel.PL3_SENSITIVE,
    "pl3": SensitivityLevel.PL3_SENSITIVE,
    "pl3_sensitive": SensitivityLevel.PL3_SENSITIVE,
    "credential": SensitivityLevel.PL4_CREDENTIAL,
    "credentials": SensitivityLevel.PL4_CREDENTIAL,
    "pl4": SensitivityLevel.PL4_CREDENTIAL,
    "pl4_credential": SensitivityLevel.PL4_CREDENTIAL,
}
_SENSITIVITY_RANK = {
    SensitivityLevel.PL1_PUBLIC: 1,
    SensitivityLevel.PL2_PII: 2,
    SensitivityLevel.PL3_SENSITIVE: 3,
    SensitivityLevel.PL4_CREDENTIAL: 4,
}


def canonicalize_sensitivity(value: SensitivityLevel | str | None) -> SensitivityLevel:
    """Return the one persisted sensitivity enum or reject an unknown write label."""

    if isinstance(value, SensitivityLevel):
        return value
    normalized = str(value or "").strip().lower()
    try:
        return _SENSITIVITY_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported sensitivity: {value!r}") from exc


def sensitivity_rank(value: SensitivityLevel | str | None) -> int:
    return _SENSITIVITY_RANK[canonicalize_sensitivity(value)]


def max_sensitivity(*values: SensitivityLevel | str | None) -> SensitivityLevel:
    if not values:
        raise ValueError("at least one sensitivity is required")
    return max((canonicalize_sensitivity(value) for value in values), key=_SENSITIVITY_RANK.__getitem__)


def is_sensitive_extraction_blocked(value: SensitivityLevel | str | None) -> bool:
    """Fail closed for legacy/unknown values on the durable extraction path."""

    try:
        return sensitivity_rank(value) >= _SENSITIVITY_RANK[SensitivityLevel.PL3_SENSITIVE]
    except ValueError:
        return True


@dataclass(slots=True)
class PrivacyDecision:
    original_text: str
    sanitized_text: str
    sensitivity: SensitivityLevel
    rejected: bool = False
    placeholders: dict[str, str] = field(default_factory=dict)
    reason: str = ""


class PrivacyStore:
    """Small typed-placeholder reverse map.

    This is intentionally storage-agnostic. Production can back it with encrypted,
    tenant-scoped persistence; tests and local write paths use the in-memory map.
    """

    def __init__(self) -> None:
        self._reverse: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def placeholder_for(self, placeholder_type: str, original: str) -> str:
        self._counters[placeholder_type] = self._counters.get(placeholder_type, 0) + 1
        placeholder = f"<{placeholder_type}_{self._counters[placeholder_type]}>"
        self._reverse[placeholder] = original
        return placeholder

    def unmask(self, text: str) -> str:
        restored = text
        for placeholder, original in self._reverse.items():
            restored = restored.replace(placeholder, original)
        return restored


class PrivacyLayer:
    _EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
    # A phone candidate must be a standalone token. In particular, it must not start after the
    # hyphen of a UUID or end before the separator of a clock/identifier. Digit-count validation
    # below applies the E.164 15-digit ceiling after formatting characters are removed.
    _PHONE_RE = re.compile(r"(?<![\w-])(?:\+?\d[\d\s().-]{7,}\d)(?![\w:-])")
    _CREDENTIAL_PATTERNS = (
        re.compile(r"\b(?:api[_-]?key|secret|token|password|credential)\s*[:=]\s*[A-Za-z0-9_\-./+=]{12,}\b", re.I),
        re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    )

    def __init__(self, store: PrivacyStore | None = None) -> None:
        self.store = store or PrivacyStore()

    def classify_and_mask(self, text: str) -> PrivacyDecision:
        sanitized = text
        placeholders: dict[str, str] = {}
        credential_found = False

        for pattern in self._CREDENTIAL_PATTERNS:
            sanitized, found = self._replace_matches(sanitized, pattern, "Credential", placeholders)
            credential_found = credential_found or found

        if credential_found:
            return PrivacyDecision(
                original_text=text,
                sanitized_text=sanitized,
                sensitivity=SensitivityLevel.PL4_CREDENTIAL,
                rejected=True,
                placeholders=placeholders,
                reason="PL4 credentials have zero retention.",
            )

        sanitized, email_found = self._replace_matches(sanitized, self._EMAIL_RE, "Email", placeholders)
        sanitized, phone_found = self._replace_matches(
            sanitized,
            self._PHONE_RE,
            "Phone",
            placeholders,
            min_digits=10,
            max_digits=15,
        )
        if email_found or phone_found:
            return PrivacyDecision(text, sanitized, SensitivityLevel.PL2_PII, placeholders=placeholders)

        return PrivacyDecision(text, sanitized, SensitivityLevel.PL1_PUBLIC, placeholders=placeholders)

    def _replace_matches(
        self,
        text: str,
        pattern: re.Pattern[str],
        placeholder_type: str,
        placeholders: dict[str, str],
        min_digits: int = 0,
        max_digits: int = 0,
    ) -> tuple[str, bool]:
        found = False

        def repl(match: re.Match[str]) -> str:
            nonlocal found
            original = match.group(0)
            digit_count = sum(ch.isdigit() for ch in original)
            if min_digits and digit_count < min_digits:
                return original
            if max_digits and digit_count > max_digits:
                return original
            found = True
            placeholder = self.store.placeholder_for(placeholder_type, original)
            placeholders[placeholder] = original
            return placeholder

        replaced = pattern.sub(repl, text)
        return replaced, found

    @staticmethod
    def redact_for_principal(text: str, sensitivity: SensitivityLevel | str, principal_stack: PrincipalStack) -> str:
        value = sensitivity.value if isinstance(sensitivity, SensitivityLevel) else str(sensitivity)
        if principal_stack.can_access_sensitivity(value):
            return text
        normalized = value.lower()
        if normalized in {"pl4_credential", "pl4", "credential"}:
            return "[REDACTED_PL4]"
        if normalized in {"pl3_sensitive", "pl3", "sensitive"}:
            return "[REDACTED_PL3]"
        return text
