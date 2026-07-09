"""Marketplace impersonation / homograph guard.

Pure, vendor-neutral detection of marketplace sources whose name spoofs a
trusted (official) marketplace identity. The trusted registry is injected data
(analogous to the remote-host allowlist) so the detection logic never hardcodes
a vendor/brand identity — the same code protects any deployment's own trusted
marketplace list.

FreeCode baseline (utils/plugins/schemas.ts:6-157): Claude Code DOES guard
official-marketplace impersonation — a reserved-name set
(ALLOWED_OFFICIAL_MARKETPLACE_NAMES), a hard non-ASCII reject
(NON_ASCII_PATTERN, its homograph defense), a blocked-pattern regex
(BLOCKED_OFFICIAL_NAME_PATTERN), and source-org verification
(validateOfficialNameSource: reserved names must come from the official git
org). CC *hard-blocks* at schema-validation time and hardcodes vendor names.

Hive deltas, deliberate: (1) vendor-neutral, injected trusted registry instead
of hardcoded brand names (AI-Native model-equality law); (2) warn-not-block —
warnings surface into the Trust Gate review so an admin decides, rather than a
schema hard-reject that can false-positive and cannot be overridden; (3)
confusable-skeleton matching (case / cross-script homograph / affix) that names
*which* trusted identity is being spoofed, versus CC's blunt non-ASCII reject.

Consumers: ``marketplace_sources.create_marketplace_source`` calls
``evaluate_marketplace_impersonation`` and persists/surfaces the warnings; it
never blocks on them (the Trust Gate review is the human decision point).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import os
from typing import Any
import unicodedata
from urllib.parse import urlparse

# Env-configured trusted marketplace registry. JSON array of {"name","host"}.
# Empty by default so no vendor identity is baked into runtime code; deployments
# opt in by configuring their own official marketplace list.
_TRUSTED_MARKETPLACES_ENV = "HIVE_TRUSTED_MARKETPLACES"

# Curated confusables map (simplified Unicode TR39 skeleton reduction). Keys are
# lowercase non-ASCII code points that render visually as the mapped ASCII
# letter. NFKC already folds fullwidth/compatibility forms, so only true
# cross-script look-alikes (Cyrillic, Greek, and a few symbol forms) are listed.
_CONFUSABLES: dict[str, str] = {
    # Cyrillic → Latin
    "а": "a",
    "ᴀ": "a",
    "ь": "b",
    "в": "b",
    "с": "c",
    "ԁ": "d",
    "е": "e",
    "ё": "e",
    "ԍ": "g",
    "һ": "h",
    "і": "i",
    "ї": "i",
    "ј": "j",
    "к": "k",
    "ӏ": "l",
    "м": "m",
    "н": "h",
    "о": "o",
    "р": "p",
    "ԛ": "q",
    "г": "r",
    "ѕ": "s",
    "т": "t",
    "и": "u",
    "ѵ": "v",
    "ԝ": "w",
    "х": "x",
    "у": "y",
    "з": "z",
    # Greek → Latin
    "α": "a",
    "β": "b",
    "ϲ": "c",
    "ԑ": "e",
    "ε": "e",
    "η": "n",
    "ι": "i",
    "κ": "k",
    "ν": "v",
    "ο": "o",
    "ρ": "p",
    "τ": "t",
    "υ": "u",
    "χ": "x",
    "γ": "y",
    "ω": "w",
    "ϳ": "j",
    # Misc look-alikes
    "ı": "i",
    "ł": "l",
    "ø": "o",
    "０": "0",
}


@dataclass(frozen=True)
class TrustedMarketplace:
    """A known-official marketplace identity to protect against impersonation."""

    name: str
    host: str


@dataclass(frozen=True)
class ImpersonationWarning:
    code: str  # case_variant | homograph_confusable | affix_impersonation
    trusted_name: str
    observed: str
    host_match: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "trusted_name": self.trusted_name,
            "observed": self.observed,
            "host_match": self.host_match,
            "detail": self.detail,
        }


def load_trusted_marketplaces(raw: str | None = None) -> tuple[TrustedMarketplace, ...]:
    """Load the trusted marketplace registry from ``HIVE_TRUSTED_MARKETPLACES``.

    The env value is a JSON array of ``{"name": ..., "host": ...}`` objects.
    Malformed entries are skipped rather than raised so a bad config never breaks
    marketplace registration — impersonation detection simply degrades to empty.
    """

    payload = raw if raw is not None else os.getenv(_TRUSTED_MARKETPLACES_ENV)
    if not payload:
        return ()
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    entries: list[TrustedMarketplace] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        host = str(item.get("host") or "").strip()
        if name:
            entries.append(TrustedMarketplace(name=name, host=host))
    return tuple(entries)


def marketplace_name_skeleton(value: str) -> str:
    """Reduce a name to a confusable-folded skeleton for look-alike comparison.

    NFKC folds fullwidth/compatibility forms, casefold folds case, the
    confusables map collapses cross-script look-alikes, and NFKD + combining-mark
    stripping removes diacritics. Two names with the same skeleton render (near)
    identically to a human even if their code points differ.
    """

    normalized = unicodedata.normalize("NFKC", value).casefold()
    mapped = "".join(_CONFUSABLES.get(ch, ch) for ch in normalized)
    decomposed = unicodedata.normalize("NFKD", mapped)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).strip()


def evaluate_marketplace_impersonation(
    *,
    name: str,
    source_uri: str,
    trusted: Sequence[TrustedMarketplace],
) -> list[ImpersonationWarning]:
    """Flag a marketplace ``name`` that spoofs any trusted marketplace identity.

    Emits at most one (highest-severity) warning per trusted entry. Exact name
    matches are treated as the legitimate marketplace and never flagged — only
    *similar* names are impersonation signals. Never raises; returns [] when the
    trusted registry is empty.
    """

    observed_name = (name or "").strip()
    if not observed_name or not trusted:
        return []

    observed_host = (urlparse(source_uri).netloc or "").lower()
    observed_casefold = observed_name.casefold()
    observed_skeleton = marketplace_name_skeleton(observed_name)

    warnings: list[ImpersonationWarning] = []
    for entry in trusted:
        trusted_name = (entry.name or "").strip()
        if not trusted_name or observed_name == trusted_name:
            # Byte-identical name is the trusted marketplace itself, not a spoof.
            continue
        host_match = observed_host == (entry.host or "").lower()
        code = _impersonation_code(
            observed_casefold=observed_casefold,
            observed_skeleton=observed_skeleton,
            trusted_name=trusted_name,
        )
        if code is None:
            continue
        warnings.append(
            ImpersonationWarning(
                code=code,
                trusted_name=trusted_name,
                observed=observed_name,
                host_match=host_match,
                detail=_impersonation_detail(code, host_match),
            )
        )
    return warnings


def _impersonation_code(
    *,
    observed_casefold: str,
    observed_skeleton: str,
    trusted_name: str,
) -> str | None:
    if observed_casefold == trusted_name.casefold():
        return "case_variant"
    trusted_skeleton = marketplace_name_skeleton(trusted_name)
    if not trusted_skeleton:
        return None
    if observed_skeleton == trusted_skeleton:
        return "homograph_confusable"
    if _is_affix(observed_skeleton, trusted_skeleton):
        return "affix_impersonation"
    return None


def _is_affix(observed_skeleton: str, trusted_skeleton: str) -> bool:
    """True when the trusted skeleton is embedded at a token boundary of observed."""

    if len(observed_skeleton) <= len(trusted_skeleton):
        return False
    if trusted_skeleton not in observed_skeleton:
        return False
    prefix = observed_skeleton.startswith(trusted_skeleton) and _is_boundary(observed_skeleton[len(trusted_skeleton)])
    suffix = observed_skeleton.endswith(trusted_skeleton) and _is_boundary(
        observed_skeleton[-len(trusted_skeleton) - 1]
    )
    return prefix or suffix


def _is_boundary(char: str) -> bool:
    return not char.isalnum()


def _impersonation_detail(code: str, host_match: bool) -> str:
    host_note = "same host as trusted" if host_match else "different host from trusted"
    return f"{code} of a trusted marketplace name ({host_note})"
