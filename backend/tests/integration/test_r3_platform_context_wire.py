"""Check every selected section against an isolated-stack provider capture.

Set A4C_PLATFORM_WIRE_INVENTORY to the inventory produced by the local harness.
Captures contain private fixture content and remain outside the repository.
"""
import json
import os
from pathlib import Path

import pytest


@pytest.fixture(params=[(fixture, kind) for fixture in ("original", "clean")
    for kind in ("plain", "delegation", "memory", "trigger")], ids=lambda key: "-".join(key))
def turn(request):
    path = os.environ.get("A4C_PLATFORM_WIRE_INVENTORY")
    if not path:
        pytest.skip("Requires an isolated-stack wire inventory.")
    inventory = json.loads(Path(path).read_text())
    matches = [item for item in inventory["turns"] if (item["fixture"], item["kind"]) == request.param]
    assert len(matches) == 1
    return matches[0]


def test_normal_turn_dynamic_sections_reach_the_provider(turn):
    assert turn["requests"], "The real turn must reach a model request."
    for request in turn["requests"]:
        assert request["actual_provider_exact_message_match"], "Check the actual provider request after adapter formatting."
        assert any(section["selected"] for section in request["dynamic_sections"])
        for section in request["dynamic_sections"]:
            if section["selected"]:
                assert section["wire_contains_complete_section"], (
                    turn["fixture"], turn["kind"], request["lane"], section["name"], section["codes"])
