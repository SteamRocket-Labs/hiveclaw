"""Semantic selection keeps a reusable index prefix and its output contract."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.memory.retriever import MemoryRetriever
from app.memory.types import MemoryItem, MemoryKind


async def test_selector_prefix_is_identical_across_queries_and_activation_order(tmp_path, monkeypatch):
    calls = []
    items = [
        MemoryItem(
            kind=MemoryKind.SEMANTIC,
            source=f"memory/knowledge/{name}.md",
            content=f"Complete {name} body",
            score=score,
            metadata={
                "entry_id": f"knowledge/{name}",
                "title": name,
                "description": f"Bounded {name} description",
                "activation_reasons": ["query:first"],
                "activation_raw_score": score,
            },
        )
        for name, score in [("beta", 3.0), ("alpha", 1.2)]
    ]
    items.extend(
        MemoryItem(
            kind=MemoryKind.SEMANTIC,
            source=f"memory/knowledge/reference-{index}.md",
            content=f"Full reference body {index}",
            score=0.4,
            metadata={
                "entry_id": f"knowledge/reference-{index}",
                "title": f"Reference {index}",
                "description": "Traceable owner delivery evidence and readable release reports. " * 3,
                "activation_reasons": ["query:first"],
            },
        )
        for index in range(58)
    )
    by_content = {
        item.content: item.metadata["selection_candidate_id"]
        for item in MemoryRetriever._attach_selection_candidate_ids(items)
    }
    response_ids = [by_content[item.content] for item in items[:2]]

    class Client:
        async def complete(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=json.dumps({"selected_ids": response_ids, "reason": "Useful facts."}))

        async def close(self):
            pass

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda _: Client())
    retriever = MemoryRetriever(data_root=tmp_path)
    agent = uuid4()
    results = []
    for query, candidates in [
        ("First query", items),
        (
            "Second query",
            [
                replace(item, score=1 - item.score, metadata={**item.metadata, "activation_reasons": ["query:second"]})
                for item in reversed(items)
            ],
        ),
    ]:
        results.append(
            await retriever._select_candidates(
                candidates,
                agent_id=agent,
                tenant_id=None,
                query=query,
                model_config={"provider": "fake", "model": "fake"},
            )
        )

    prompts = [call["messages"][-1].content for call in calls]
    prefixes = [prompt.split('<semantic_source ref="activation_observations"', 1)[0].encode() for prompt in prompts]
    assert prefixes[0] == prefixes[1]
    assert all(f"Bounded {name} description".encode() in prefixes[0] for name in ("alpha", "beta"))
    assert "First query" not in prefixes[0].decode()
    assert "coverage_manifest_ref" not in prefixes[0].decode()
    assert all(
        prompt.endswith(f"<current_query>\n{query}\n</current_query>")
        for prompt, query in zip(prompts, ("First query", "Second query"))
    )
    assert "query:first" in prompts[0] and "query:second" in prompts[1]
    assert calls[0]["messages"][0].content == calls[1]["messages"][0].content
    assert "brief audit note" in calls[0]["messages"][0].content
    observations = [
        json.loads(
            prompt.split('<semantic_source ref="activation_observations"', 1)[1]
            .split("\n", 1)[1]
            .split("\n</semantic_source>", 1)[0]
        )
        for prompt in prompts
    ]
    strongest, weaker = response_ids
    assert observations[0][strongest]["score"] == observations[0][weaker]["score"] == 1.0
    assert observations[0][strongest]["activation_rank"] == 1
    assert observations[0][weaker]["activation_rank"] == 2
    assert observations[1][strongest]["activation_rank"] > observations[1][weaker]["activation_rank"]
    assert "Index order is stable and carries no ranking" in calls[0]["messages"][0].content
    assert [[item.content for item in result] for result in results] == [
        [item.content for item in items[:2]],
        [item.content for item in items[:2]],
    ]
    assert all(item.metadata["semantic_selection_reason"] == "Useful facts." for result in results for item in result)
    receipts = [
        json.loads(path.read_text())
        for path in (tmp_path / str(agent) / "memory/control/retrieval_reviews").glob("*.decision.json")
    ]
    assert len(receipts) == 2
    assert all(row["selected_ids"] == response_ids and row["reason"] == "Useful facts." for row in receipts)
    assert len({row["coverage_path"] for row in receipts}) == 2


async def test_identical_fake_selection_response_preserves_order_deduplication_and_audit(tmp_path, monkeypatch):
    items = [
        MemoryItem(kind=MemoryKind.SEMANTIC, source=name, content=name, metadata={"entry_id": name})
        for name in ("alpha", "beta")
    ]
    ids = [item.metadata["selection_candidate_id"] for item in MemoryRetriever._attach_selection_candidate_ids(items)]

    class Client:
        async def complete(self, **_kwargs):
            return SimpleNamespace(
                content=json.dumps(
                    {"selected_ids": [ids[1], ids[0], ids[1]], "reason": "Exact fake rationale is preserved."}
                )
            )

        async def close(self):
            pass

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda _: Client())
    retriever = MemoryRetriever(data_root=tmp_path)
    results = [
        await retriever._select_candidates(
            items, agent_id=uuid4(), tenant_id=None, query=query, model_config={"provider": "fake", "model": "fake"}
        )
        for query in ("one", "two")
    ]
    assert all([item.content for item in result] == ["beta", "alpha"] for result in results)
    assert all(
        item.metadata["semantic_selection_reason"] == "Exact fake rationale is preserved."
        for result in results
        for item in result
    )


@pytest.mark.parametrize("provider", ["minimax", "qwen", "openai", "future-provider"])
async def test_selection_cache_breakpoint_survives_complete_on_the_wire(tmp_path, monkeypatch, provider):
    from unittest.mock import AsyncMock

    from app.memory.retriever import _selection_descriptor
    from app.services.llm_client import OpenAICompatibleClient, _MeteredLLMClient
    from app.services.semantic_input_coverage import _render_sources

    requests, wire_bytes = [], []

    def respond(request):
        requests.append(json.loads(request.content))
        wire_bytes.append(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"selected_ids":[],"reason":"None"}'}}]})

    items = MemoryRetriever._attach_selection_candidate_ids(
        [
            MemoryItem(kind=MemoryKind.SEMANTIC, source="memory/knowledge/stable.md", content="Stable body", score=1.0),
        ]
    )
    monkeypatch.setattr(_MeteredLLMClient, "_record_usage", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:

        async def get_client(_self):
            return transport

        monkeypatch.setattr(OpenAICompatibleClient, "_get_client", get_client)
        retriever = MemoryRetriever(data_root=tmp_path)
        agent_id = uuid4()
        for query in ("First private query", "Second private query"):
            assert await retriever._select_with_model(
                items=items,
                agent_id=agent_id,
                tenant_id=None,
                query=query,
                model_config={"provider": provider, "model": "local-fake", "api_key": "local-fake"},
                selection_run_id="wire-contract",
            ) == ([], "None")

    descriptor = _selection_descriptor(items[0])
    observations = {str(descriptor["candidate_id"]): descriptor.pop("observations")}
    covered_index = "\n" + _render_sources(
        [
            (str(descriptor["candidate_id"]), json.dumps(descriptor, ensure_ascii=False, indent=2, default=str)),
            (
                "activation_observations",
                json.dumps(observations, ensure_ascii=False, separators=(",", ":"), default=str),
            ),
        ]
    )
    coverage_path = tmp_path / str(agent_id) / "memory/control/retrieval_reviews/wire-contract.coverage.json"
    manifest = json.loads(coverage_path.read_text())
    manifest_sha = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    coverage_ref = (
        f'<coverage_manifest_ref path="{coverage_path.as_posix()}" '
        f'sha256="{manifest_sha}" complete="true" source_chars="{manifest["source_chars"]}" />'
    )
    for request, raw_bytes, query in zip(requests, wire_bytes, ("First private query", "Second private query")):
        system, user = request["messages"]
        expected_user = f"{covered_index}\n\n{coverage_ref}\n\n<current_query>\n{query}\n</current_query>"
        if provider in ("minimax", "qwen"):
            stable, dynamic = user["content"]
            assert system["content"][0]["cache_control"] == {"type": "ephemeral"}
            assert stable["cache_control"] == {"type": "ephemeral"}
            assert stable["text"].endswith("</semantic_source>\n\n")
            assert 'ref="activation_observations"' in dynamic["text"]
            assert "cache_control" not in dynamic
            assert all(
                text not in stable["text"]
                for text in (
                    "First private query",
                    "Second private query",
                    "activation_rank",
                    "coverage_manifest_ref",
                    "<current_query>",
                )
            )
            assert stable["text"] + dynamic["text"] == expected_user
        else:
            assert isinstance(system["content"], str)
            assert user == {"role": "user", "content": expected_user}
            assert "cache_control" not in json.dumps(request)
            # Restore only the ranking sentence to pin the 34b3763f system bytes.
            baseline_system = system["content"].replace(
                "Index order is stable and carries no ranking; activation_rank in the observations "
                "records the original activation order (1 = strongest). Mechanical scores, ranking, "
                "recency, and graph signals are observations only.",
                "Mechanical scores, order, recency, and graph signals are observations only.",
            )
            assert hashlib.sha256(baseline_system.encode()).hexdigest() == (
                "e3a1b706c7a8c782a718930404b9b0a627f691796d0288c19c043e584eb56581"
            )
            expected_request = {
                "model": "local-fake",
                "messages": [system, {"role": "user", "content": expected_user}],
                "stream": False,
                "temperature": 0.1,
                "max_completion_tokens" if provider == "openai" else "max_tokens": 32_768,
            }
            assert raw_bytes == httpx.Request("POST", "https://local.invalid", json=expected_request).content
    assert requests[0]["messages"][0] == requests[1]["messages"][0]
    if provider in ("minimax", "qwen"):
        assert requests[0]["messages"][1]["content"][0] == requests[1]["messages"][1]["content"][0]


async def test_new_or_updated_episode_keeps_earlier_kind_index_bytes(tmp_path, monkeypatch):
    calls = []

    class Client:
        async def complete(self, **kwargs):
            calls.append(kwargs["messages"][-1].content)
            return SimpleNamespace(content='{"selected_ids":[],"reason":"None"}')

        async def close(self):
            pass

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda _: Client())
    stable_items = [
        MemoryItem(kind=MemoryKind.SEMANTIC, source="memory/knowledge/stable.md", content="Persistent knowledge"),
        MemoryItem(kind=MemoryKind.EXTERNAL, source="external", content="External description"),
    ]
    episode = MemoryItem(
        kind=MemoryKind.EPISODIC,
        source="session/z",
        content="Earlier episode",
        metadata={"session_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"},
    )
    new_episode = replace(
        episode,
        source="session/a",
        content="New episode",
        metadata={"session_id": "00000000-0000-0000-0000-000000000001"},
    )
    retriever = MemoryRetriever(data_root=tmp_path)
    for episodes in ([episode], [new_episode, episode], [replace(episode, content="Updated episode")]):
        await retriever._select_candidates(
            [*stable_items, *episodes],
            agent_id=uuid4(),
            tenant_id=None,
            query="Episode query",
            model_config={"provider": "fake", "model": "fake"},
        )
    prefixes = [prompt.split('<semantic_source ref="memory_candidate:episodic:', 1)[0].encode() for prompt in calls]
    assert all(b"Persistent knowledge" in prefix and b"External description" in prefix for prefix in prefixes)
    assert prefixes[0] == prefixes[1] == prefixes[2]


@pytest.mark.parametrize("max_chars", [20_000, 1_000])
async def test_covered_semantic_input_starts_with_one_manifest_ref_line(tmp_path, max_chars):
    from app.services.semantic_input_coverage import prepare_covered_semantic_input

    coverage_path = tmp_path / "coverage.json"

    async def review_chunk(_phase, _prompt):
        return "Source alpha: complete coverage notes."

    covered = await prepare_covered_semantic_input(
        phase="selection-format",
        sections=[("alpha", "Evidence " * 700)],
        max_chars=max_chars,
        coverage_path=coverage_path,
        review_chunk=review_chunk,
    )
    first_line, separator, body = covered.partition("\n")
    manifest = json.loads(coverage_path.read_text())
    manifest_sha = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert separator == "\n"
    assert first_line == (
        f'<coverage_manifest_ref path="{coverage_path.as_posix()}" '
        f'sha256="{manifest_sha}" complete="true" source_chars="{manifest["source_chars"]}" />'
    )
    assert body.startswith("\n<semantic_source " if manifest["mode"] == "inline_full" else "<model_coverage_notes>\n")


async def test_model_coverage_notes_stay_after_the_system_cache_breakpoint(tmp_path, monkeypatch):
    calls = []

    async def covered_input(**_kwargs):
        return '<coverage_manifest_ref path="dynamic" />\n<model_coverage_notes>Query-dependent notes</model_coverage_notes>'

    class Client:
        async def complete(self, **kwargs):
            calls.append(kwargs["messages"])
            return SimpleNamespace(content='{"selected_ids":[],"reason":"None"}')

        async def close(self):
            pass

    monkeypatch.setattr("app.services.semantic_input_coverage.prepare_covered_semantic_input", covered_input)
    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda _: Client())
    await MemoryRetriever(data_root=tmp_path)._select_candidates(
        [MemoryItem(kind=MemoryKind.SEMANTIC, content="Stable knowledge")],
        agent_id=uuid4(),
        tenant_id=None,
        query="Private query",
        model_config={"provider": "minimax", "model": "local-fake"},
    )
    system, user = calls[0]
    assert system.content[0]["cache_control"] == {"type": "ephemeral"}
    assert "Query-dependent notes" not in system.content[0]["text"]
    assert user.content == (
        "<model_coverage_notes>Query-dependent notes</model_coverage_notes>\n\n"
        '<coverage_manifest_ref path="dynamic" />\n\n<current_query>\nPrivate query\n</current_query>'
    )
