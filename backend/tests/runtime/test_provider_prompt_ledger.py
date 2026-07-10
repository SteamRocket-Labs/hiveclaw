from __future__ import annotations

from app.services.llm_client import LLMMessage


def test_provider_prompt_ledger_accounts_tool_schema_tokens() -> None:
    from app.runtime.provider_prompt_ledger import build_provider_prompt_ledger

    messages = [
        LLMMessage(role="system", content="You are an analyst."),
        LLMMessage(role="user", content="Summarize the latest events."),
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": "Fetch and parse a web page. " * 80,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to fetch."},
                    },
                    "required": ["url"],
                },
            },
        }
    ]

    ledger = build_provider_prompt_ledger(
        messages=messages,
        tools=tools,
        provider="minimax",
        model="m3",
        round_index=3,
        model_window_tokens=1_000_000,
        provider_call_id="call-test",
        cache_hints_applied=True,
    )

    categories = {item["name"]: item for item in ledger["categories"]}

    assert ledger["schema"] == "hive.ccplus.provider_prompt_ledger.v1"
    assert ledger["provider_call_id"] == "call-test"
    assert ledger["round"] == 3
    assert categories["messages"]["tokens"] > 0
    assert categories["tool_schemas"]["tokens"] > categories["messages"]["tokens"]
    assert ledger["tool_schema_tokens"] == categories["tool_schemas"]["tokens"]
    assert ledger["projected_input_tokens"] >= (categories["messages"]["tokens"] + categories["tool_schemas"]["tokens"])
    assert ledger["projected_uncached_input_tokens"] >= ledger["tool_schema_tokens"]
    assert ledger["cache_hints_applied"] is True


def test_provider_prompt_ledger_marks_volatile_extra_surfaces_uncached() -> None:
    from app.runtime.provider_prompt_ledger import build_provider_prompt_ledger

    ledger = build_provider_prompt_ledger(
        messages=[LLMMessage(role="user", content="Continue.")],
        tools=[],
        extra_surfaces={
            "dynamic_notice": "## Dynamic Memory\nFresh recall.",
            "runtime_reminders": ["Remember the pending approval.", "Check the work ledger."],
        },
        provider="openai",
        model="gpt",
        round_index=1,
        model_window_tokens=128_000,
    )

    categories = {item["name"]: item for item in ledger["categories"]}

    assert categories["dynamic_notice"]["cacheability"] == "volatile"
    assert categories["runtime_reminders"]["cacheability"] == "volatile"
    assert categories["dynamic_notice"]["tokens"] > 0
    assert categories["runtime_reminders"]["tokens"] > 0
    assert ledger["projected_uncached_input_tokens"] >= (
        categories["dynamic_notice"]["tokens"] + categories["runtime_reminders"]["tokens"]
    )
