"""Parallel compaction preserves serial coverage and merge order."""

import asyncio
import json
import re
import time
from collections import defaultdict
from types import SimpleNamespace

from app.services import conversation_summarizer as summarizer
from app.services import llm_client


async def test_map_and_each_reduce_level_are_bounded_and_preserve_serial_output(monkeypatch):
    messages = [{"role": "user", "content": f"message-{index}:" + "x" * 900} for index in range(21)]
    config = {"provider": "openai", "model": "local-double", "max_input_tokens": 500}
    chunks = summarizer._build_summary_input_chunks(messages, provider="openai", max_input_tokens=500)
    refs = [ref for chunk in chunks for ref in chunk.coverage_refs]
    starts, finishes, active, peaks, requests = {}, {}, defaultdict(int), defaultdict(int), {}

    class ModelDouble:
        async def stream(self, *, messages, **kwargs):
            payload = messages[-1].content
            phase = re.search(r"<compaction_phase>(.*?)</compaction_phase>", payload)[1]
            stage = "map" if phase.startswith("map") else phase.split("_group_")[0]
            coverage = json.loads(re.search(r"<coverage_manifest>\n(.*?)\n</coverage_manifest>", payload)[1])
            requests[phase] = payload
            starts[phase] = time.perf_counter()
            active[stage] += 1
            peaks[stage] = max(peaks[stage], active[stage])
            # Earlier chunks finish later, so completion order cannot stand in for input order.
            ordinal = int(re.search(r"(?:map_chunk_|group_)(\d+)", phase)[1])
            await asyncio.sleep(0.005 * (5 - ordinal % 4))
            active[stage] -= 1
            finishes[phase] = time.perf_counter()
            return SimpleNamespace(content=f"<summary>{json.dumps(coverage)}</summary>")

        async def close(self):
            assert not any(active.values())

    monkeypatch.setattr(llm_client, "create_llm_client_from_config", lambda _: ModelDouble())
    result = await summarizer._llm_summarize(messages, config)

    # The serial reducer produces exactly this text and coverage manifest.
    expected = (
        json.dumps(refs)
        + "\n\n<compaction_coverage>"
        + json.dumps(refs, separators=(",", ":"))
        + "</compaction_coverage>"
    )
    assert result == expected
    assert peaks["map"] == 4
    assert peaks["reduce_level_1"] == 4
    assert all(peak <= 4 for peak in peaks.values())
    map_phases = [phase for phase in starts if phase.startswith("map")]
    assert starts[map_phases[1]] < finishes[map_phases[0]]
    reduce_phases = [phase for phase in starts if phase.startswith("reduce_level_1_")]
    assert starts[reduce_phases[1]] < finishes[reduce_phases[0]]
    assert max(finishes[phase] for phase in map_phases) <= min(starts[phase] for phase in reduce_phases)
    for index, chunk in enumerate(chunks, 1):
        payload = requests[f"map_chunk_{index}_of_{len(chunks)}"]
        assert f"<covered_input>\n{chunk.text}\n</covered_input>" in payload
        assert json.loads(re.search(r"<coverage_manifest>\n(.*?)\n</coverage_manifest>", payload)[1]) == list(
            chunk.coverage_refs
        )
    for phase, payload in requests.items():
        if phase.startswith("reduce"):
            children = re.findall(r'<chunk_summary index="\d+" coverage=.*?>\n(.*?)\n</chunk_summary>', payload)
            coverage = json.loads(re.search(r"<coverage_manifest>\n(.*?)\n</coverage_manifest>", payload)[1])
            assert [ref for child in children for ref in json.loads(child)] == coverage
