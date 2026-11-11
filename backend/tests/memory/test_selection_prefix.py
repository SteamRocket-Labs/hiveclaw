"""Semantic selection keeps a reusable index prefix and its output contract."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

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
            },
        )
        for name, score in [("beta", 0.9), ("alpha", 0.2)]
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
    print("A2E_PREFIX", len(prefixes[0]), hashlib.sha256(prefixes[0]).hexdigest())
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
