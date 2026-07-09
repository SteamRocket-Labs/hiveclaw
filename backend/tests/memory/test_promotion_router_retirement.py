from __future__ import annotations

import importlib.util


def test_promotion_router_module_is_retired_until_runtime_entrypoint_exists() -> None:
    """No standalone promotion router may remain without a runtime consumer."""

    assert importlib.util.find_spec("app.memory.promotion_router") is None
