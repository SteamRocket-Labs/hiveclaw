from __future__ import annotations

from fastapi.testclient import TestClient

from tests.journeys.fake_external_provider import _response_payload, _tool_for_journey, app


def _payload(messages: list[dict]) -> dict:
    return {
        "messages": messages,
        "tools": [
            {"type": "function", "function": {"name": "write_file"}},
            {"type": "function", "function": {"name": "exit_plan_mode"}},
        ],
    }


def test_controlled_tool_calls_match_production_schemas() -> None:
    assert _tool_for_journey("J-08", {"load_skill"}) == ("load_skill", {"name": "Advanced Web Research"})
    assert _tool_for_journey("J-09", {"spawn_subagent"}) == (
        "spawn_subagent",
        {
            "task": "J-09 return the atomic subagent receipt",
            "type": "explorer",
            "run_in_background": True,
        },
    )
    # J-13's unbound external session runs tool-disabled by production
    # authority — the provider must never propose send_channel_message for it.
    assert _tool_for_journey("J-13", {"send_channel_message"}) is None
    assert _tool_for_journey("J-12", {"preview_agent_blueprint"}) == (
        "preview_agent_blueprint",
        {
            "name": "Atomic Journey Employee",
            "role_description": "J-12 validates durable governed employee provisioning.",
            "primary_users": ["Atomic journey owner"],
            "core_outputs": ["A durable provisioning receipt"],
            "permission_scope": "self",
            "boundaries": "Never bypass confirmation or tenant authority.",
            "focus_content": "Validate the first governed HR task.",
        },
    )


def test_controlled_slack_provider_records_delivery_evidence() -> None:
    client = TestClient(app)
    profile = client.get("/slack/api/users.info", params={"user": "U-ATOMIC"})
    delivery = client.post(
        "/slack/api/chat.postMessage",
        json={"channel": "C-ATOMIC", "text": "J-13 terminal receipt"},
    )

    assert profile.json()["user"]["profile"]["display_name"] == "Atomic Slack User"
    assert delivery.json()["ok"] is True
    evidence = client.get("/evidence").json()
    assert evidence["calls"]["slack:C-ATOMIC"] >= 1


def test_plan_journey_writes_runtime_plan_file_then_submits_then_finishes() -> None:
    user = {
        "role": "user",
        "content": (
            "J-03 plan this request. Use and update this exact file as the working plan, "
            "the only path writable in Plan Mode: workspace/plans/session.plan.md."
        ),
    }
    content, call = _response_payload(_payload([user]))
    assert content is None
    assert call == (
        "write_file",
        {
            "path": "workspace/plans/session.plan.md",
            "content": "# Atomic journey plan\n\nVerify the governed plan confirmation and continuation contract.",
        },
    )

    write_assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "write_file", "arguments": "{}"}}],
    }
    write_result = {"role": "tool", "tool_call_id": "write-1", "content": "ok"}
    content, call = _response_payload(_payload([user, write_assistant, write_result]))
    assert content is None
    assert call is not None and call[0] == "exit_plan_mode"

    exit_assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "exit_plan_mode", "arguments": "{}"}}],
    }
    exit_result = {"role": "tool", "tool_call_id": "exit-1", "content": "ok"}
    content, call = _response_payload(_payload([user, write_assistant, write_result, exit_assistant, exit_result]))
    assert call is None
    assert content == "J-03 terminal receipt from the controlled provider."


def test_receipt_only_probe_does_not_invoke_domain_tool() -> None:
    content, call = _response_payload(_payload([{"role": "user", "content": "J-03 receipt-only acceptance probe"}]))

    assert call is None
    assert content == "J-03 terminal receipt from the controlled provider."


def test_current_journey_marker_wins_over_historical_context() -> None:
    content, call = _response_payload(
        _payload(
            [
                {
                    "role": "user",
                    "content": "J-01 exercise the production journey contract.",
                },
                {
                    "role": "user",
                    "content": "Injected historical context mentions J-03 after the actual request.",
                },
            ]
        )
    )

    assert call is None
    assert content == "J-01 terminal receipt from the controlled provider."


def test_journeys_with_out_of_contract_tool_effects_have_no_candidate() -> None:
    # J-02's product path is the chat upload plus the workspace uploads
    # listing (exercised by the journey itself); a write_file model call only
    # produces an out-of-workspace failure. J-05's path is the disabled
    # schedule API plus listing; a set_trigger call would need enabled side
    # effects that violate the disabled-schedule journey. Both must return
    # the controlled terminal receipt with no model tool effect.
    assert _tool_for_journey("J-02", {"write_file"}) is None
    assert _tool_for_journey("J-05", {"set_trigger"}) is None


def test_evidence_slack_history_grows_by_exact_new_row() -> None:
    from fastapi.testclient import TestClient

    from tests.journeys import fake_external_provider as provider

    with TestClient(provider.app) as client:
        # Baseline count first: state growth must be proven by before/after
        # count plus the newly appended exact row, so an identical message
        # posted twice (or a retried test run) never cross-satisfies.
        before = client.get("/evidence").json()["slack_messages"]
        baseline = len(before)
        posted = client.post(
            "/slack/api/chat.postMessage",
            json={"channel": "C-HIST", "text": "J-13 terminal receipt from the controlled provider."},
        )
        assert posted.status_code == 200
        after = client.get("/evidence").json()["slack_messages"]
        assert len(after) == baseline + 1
        assert after[baseline:] == [
            {"channel": "C-HIST", "text": "J-13 terminal receipt from the controlled provider."}
        ]


def test_j09_spawn_task_carries_explicit_journey_marker() -> None:
    from tests.journeys.fake_external_provider import _tool_for_journey

    # The spawn task must carry the J-09 marker so the child's controlled
    # response is the J-09 receipt, never an earlier journey's (full5 J-01
    # false green came from a marker-less task falling back through history).
    tool, args = _tool_for_journey("J-09", {"spawn_subagent"})
    assert tool == "spawn_subagent"
    assert "J-09" in args["task"]


def test_extractor_protocol_request_returns_strict_extraction_json() -> None:
    import json as jsonlib

    from tests.journeys.fake_external_provider import _response_payload

    system_prompt = (
        "You extract a compact, source-grounded personal knowledge graph from one Markdown segment.\n"
        "Return only strict JSON with keys: entities, assertions, links, warnings.\n"
    )
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": jsonlib.dumps(
                    {"document": {"id": "d", "title": "t", "source_kind": "paste"}, "content": "J-07 marker bytes"}
                ),
            },
        ],
        "tools": [],
    }
    content, tool_call = _response_payload(payload)
    assert tool_call is None
    parsed = jsonlib.loads(str(content))
    assert set(parsed) == {"entities", "assertions", "links", "warnings"}
    # Source-safe: no invented semantic facts — strict empty arrays still
    # prove the protocol boundary and let the pipeline reach ready.
    assert parsed["entities"] == []
    assert parsed["assertions"] == []
    assert parsed["links"] == []
    assert parsed["warnings"] == []


def test_j09_integration_continuation_returns_receipt_without_new_subagent() -> None:
    """Regression: the runtime result integration continuation must not re-spawn.

    The continuation turn is a fresh provider payload whose messages carry the
    integration page runtime context and the completed child's J-09 receipt,
    but NOT the original spawn tool_call — so tool-name bookkeeping cannot
    suppress a second spawn. The provider must integrate and answer the
    terminal receipt with no tool effect (fresh_1420 duplicate-child defect).
    """
    messages = [
        {
            "role": "user",
            "content": "J-09 exercise the production journey contract.",
        },
        {
            "role": "user",
            "content": (
                "Runtime result integration page.\n"
                "This is internal runtime context, not a user message or a platform-authored conclusion.\n"
                "Integration epoch: 1\n"
                "Root coverage: terminal=2/expected=2\n"
                "Result references in mailbox order:\n"
                "- seq=1 child=Subagent status=completed bytes=1276 artifacts=0 "
                "ref=runtime-result://eda3bdac-6118-5ffa-b652-80999a9cae83/b7d5db85\n"
                "Integrate only after reading the result bodies needed for the task."
            ),
        },
    ]
    content, tool_call = _response_payload(
        {
            "messages": messages,
            "tools": [{"type": "function", "function": {"name": "spawn_subagent"}}],
        }
    )
    assert tool_call is None
    assert content == "J-09 terminal receipt from the controlled provider."


def test_j07_progressive_disclosure_sequence() -> None:
    import json as jsonlib

    from tests.journeys.fake_external_provider import _next_tool_for_journey

    def messages_with(called_tools: list[tuple[str, str]], search_result: str | None = None) -> list[dict]:
        built: list[dict] = [
            {
                "role": "user",
                "content": "J-07 exercise the production journey contract with unique marker j07-ab12cd34.",
            }
        ]
        for name, args_json in called_tools:
            built.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": f"call-{name}", "type": "function", "function": {"name": name, "arguments": args_json}}
                    ],
                }
            )
            if name == "search_personal_kb" and search_result is not None:
                built.append({"role": "tool", "tool_call_id": f"call-{name}", "content": search_result})
            else:
                built.append({"role": "tool", "tool_call_id": f"call-{name}", "content": "{}"})
        return built

    # Step 1: only tool_search is available first — discover the deferred tools.
    first = _next_tool_for_journey("J-07", available={"tool_search"}, messages=messages_with([]))
    assert first == ("tool_search", {"query": "personal knowledge"})

    # Step 2: search the exact unique per-run marker carried by the prompt —
    # repeat-safe; a static query could match an earlier document.
    second = _next_tool_for_journey(
        "J-07",
        available={"tool_search", "search_personal_kb", "read_personal_kb"},
        messages=messages_with([("tool_search", '{"query": "personal knowledge"}')]),
    )
    assert second == ("search_personal_kb", {"query": "j07-ab12cd34"})

    # Step 3: read the exact document/segment ids parsed from the matched
    # search tool result — never fabricated, never a second search.
    search_json = jsonlib.dumps(
        {
            "status": "ok",
            "results": [
                {
                    "document_id": "11111111-1111-4111-8111-111111111111",
                    "segment_id": "22222222-2222-4222-8222-222222222222",
                    "title": "J-07 owner knowledge",
                    "snippet": "marker",
                    "source_ref": "kb://person/owner/documents/doc#segment=seg",
                }
            ],
            "authority": {"allowed": True},
        }
    )
    third = _next_tool_for_journey(
        "J-07",
        available={"tool_search", "search_personal_kb", "read_personal_kb"},
        messages=messages_with(
            [
                ("tool_search", '{"query": "personal knowledge"}'),
                ("search_personal_kb", '{"query": "j07-ab12cd34"}'),
            ],
            search_result=search_json,
        ),
    )
    assert third == (
        "read_personal_kb",
        {
            "document_id": "11111111-1111-4111-8111-111111111111",
            "segment_ids": ["22222222-2222-4222-8222-222222222222"],
        },
    )

    # Step 4: after the read, the sequence ends with the terminal receipt.
    done = _next_tool_for_journey(
        "J-07",
        available={"tool_search", "search_personal_kb", "read_personal_kb"},
        messages=messages_with(
            [
                ("tool_search", '{"query": "personal knowledge"}'),
                ("search_personal_kb", '{"query": "j07-ab12cd34"}'),
                ("read_personal_kb", '{"document_id": "11111111-1111-4111-8111-111111111111"}'),
            ],
            search_result=search_json,
        ),
    )
    assert done is None

    # A search result with no parseable ids must not fabricate a read.
    empty = _next_tool_for_journey(
        "J-07",
        available={"tool_search", "search_personal_kb", "read_personal_kb"},
        messages=messages_with(
            [
                ("tool_search", '{"query": "personal knowledge"}'),
                ("search_personal_kb", '{"query": "j07-ab12cd34"}'),
            ],
            search_result=jsonlib.dumps({"status": "ok", "results": [], "authority": {"allowed": True}}),
        ),
    )
    assert empty is None

    # No unique marker in the prompt: no search may be proposed.
    markerless = _next_tool_for_journey(
        "J-07",
        available={"tool_search", "search_personal_kb", "read_personal_kb"},
        messages=[{"role": "user", "content": "J-07 exercise the production journey contract."}],
    )
    assert markerless == ("tool_search", {"query": "personal knowledge"})


def test_j07_marker_survives_trailing_system_notice_user_message() -> None:
    """Regression: a trailing user-role System Notice must not hide the marker.

    Round 2 of the live turn appends a role=user System Notice after the
    original marker prompt; the marker resolver must scan user messages in
    reverse so search_personal_kb still queries the exact per-run marker.
    """
    from tests.journeys.fake_external_provider import _latest_user_marker, _next_tool_for_journey

    messages = [
        {
            "role": "user",
            "content": "J-07 exercise the production journey contract with unique marker j07-ab12cd34.",
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-tool_search",
                    "type": "function",
                    "function": {"name": "tool_search", "arguments": '{"query": "personal knowledge"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-tool_search", "content": "{}"},
        {
            "role": "user",
            "content": "System Notice: the runtime granted the deferred tools for this round.",
        },
    ]
    assert _latest_user_marker(messages) == "j07-ab12cd34"
    proposal = _next_tool_for_journey(
        "J-07",
        available={"tool_search", "search_personal_kb", "read_personal_kb"},
        messages=messages,
    )
    assert proposal == ("search_personal_kb", {"query": "j07-ab12cd34"})
