from __future__ import annotations

import pytest

from app.memory.form_lint import enforce_memory_form, lint_memory_form


def test_form_lint_accepts_self_contained_memory() -> None:
    result = lint_memory_form("2026-05-22 owner Alice rejected Agent Kairos external vendor reply draft.")

    assert result.ok
    assert result.violations == []


@pytest.mark.parametrize(
    "content",
    [
        "He will ship it tomorrow.",
        "用户说不要这样做。",
    ],
)
def test_form_lint_rejects_pronouns_and_relative_time(content: str) -> None:
    result = lint_memory_form(content)

    assert not result.ok
    assert result.violations


def test_enforce_memory_form_raises_with_actionable_message() -> None:
    with pytest.raises(ValueError, match="Form Contract violation"):
        enforce_memory_form("They should handle this next week.")

