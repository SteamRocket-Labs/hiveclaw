"""Self-contained memory form contract linting."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FormViolation:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class FormLintResult:
    ok: bool
    violations: list[FormViolation]


_EN_PRONOUN_RE = re.compile(r"\b(he|she|it|they|this|that|these|those)\b", re.I)
_CJK_PRONOUN_RE = re.compile(r"(他|她|它|他们|她们|这|那|这样|那个|这个)")
_RELATIVE_TIME_RE = re.compile(
    r"\b(yesterday|today|tomorrow|last week|next week|recently|soon|later)\b|"
    r"(昨天|今天|明天|上周|下周|最近|稍后|之后|以后)",
    re.I,
)
def lint_memory_form(content: str) -> FormLintResult:
    text = (content or "").strip()
    violations: list[FormViolation] = []
    if not text:
        violations.append(FormViolation("empty", "Memory content is empty."))
    if _EN_PRONOUN_RE.search(text) or _CJK_PRONOUN_RE.search(text):
        violations.append(FormViolation("ambiguous_pronoun", "Replace pronouns with explicit actors or objects."))
    if _RELATIVE_TIME_RE.search(text):
        violations.append(FormViolation("relative_time", "Replace relative time with an absolute date or timestamp."))
    return FormLintResult(ok=not violations, violations=violations)


def enforce_memory_form(content: str) -> None:
    result = lint_memory_form(content)
    if result.ok:
        return
    details = "; ".join(f"{violation.code}: {violation.message}" for violation in result.violations)
    raise ValueError(f"Form Contract violation: {details}")
