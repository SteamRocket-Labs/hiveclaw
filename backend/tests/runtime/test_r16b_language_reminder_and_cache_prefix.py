"""R16b: a one-line language reminder rides the per-request tail, not the cache prefix.

The member writes Chinese; the own assistant answered with English progress notes
because MEMBER_LANGUAGE_RULE sits in the frozen prefix thousands of tokens above
the request tail. The reminder is added to § Environment (dynamic suffix, the
last system-guidance block the model sees) so the cache-stable prefix bytes are
untouched.
"""

from __future__ import annotations

from app.runtime.prompt_builder import (
    PROMPT_CACHE_BOUNDARY,
    assemble_runtime_prompt,
    build_dynamic_prompt_suffix,
    build_frozen_prompt_prefix,
)
from app.runtime.prompt_sections import build_tone_style_section
from app.runtime.prompt_sections.environment import build_environment_section
from app.runtime.prompt_sections.tone_style import MEMBER_LANGUAGE_RULE

# R16b round 2 (N6): § Environment restates MEMBER_LANGUAGE_RULE verbatim instead
# of a paraphrase, so the marker is the whole § Environment line.
LANGUAGE_REMINDER = f"- Language: {MEMBER_LANGUAGE_RULE}"


def _frozen_prefix() -> str:
    # Representative frozen prefix: agent context (with the tone/style rule) plus
    # the static § sections, exactly the layers build_frozen_prompt_prefix joins.
    return build_frozen_prompt_prefix(agent_context="### Identity\n我的助理\n\n" + build_tone_style_section())


def _dynamic_suffix(*, query: str) -> str:
    return build_dynamic_prompt_suffix(
        retrieval_context="",
        latest_user_query=query,
        user_name="test123",
        channel="web",
        agent_name="我的助理",
        system_prompt_suffix="",
    )


class TestReminderPlacement:
    def test_environment_section_carries_the_one_line_reminder(self):
        section = build_environment_section(user_name="test123", channel="web", agent_name="我的助理")
        assert LANGUAGE_REMINDER in section
        assert "progress notes between tool calls" in section

    def test_reminder_is_present_on_every_turn_of_the_dynamic_suffix(self):
        first = _dynamic_suffix(query="帮我把这份销售数据写成三条周报要点")
        second = _dynamic_suffix(query="现在把它们翻译成英文")
        assert LANGUAGE_REMINDER in first
        assert LANGUAGE_REMINDER in second


class TestCacheStablePrefixUnchanged:
    def test_frozen_prefix_is_byte_identical_across_turns_and_excludes_the_reminder(self):
        # The rule itself still ships in the frozen prefix …
        frozen_one = _frozen_prefix()
        frozen_two = _frozen_prefix()
        assert frozen_one == frozen_two
        assert "same language the user uses" in frozen_one
        # … but the new reminder never enters it.
        assert LANGUAGE_REMINDER not in frozen_one

    def test_assembled_request_keeps_the_reminder_below_the_cache_boundary(self):
        combined = assemble_runtime_prompt(_frozen_prefix(), _dynamic_suffix(query="写三条周报要点"))
        frozen, dynamic = combined.split(PROMPT_CACHE_BOUNDARY, 1)
        assert LANGUAGE_REMINDER not in frozen
        assert LANGUAGE_REMINDER in dynamic
        # The reminder rides § Environment, the tail block of the request.
        assert dynamic.index("## Environment") < dynamic.index(LANGUAGE_REMINDER)
