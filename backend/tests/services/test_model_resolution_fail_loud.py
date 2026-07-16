"""B-fail-loud: 主模型配了却解析不到时,运行时必须 fail-loud,不得静默降级。

Web3研究员事故:agent 主模型指向别租户 DeepSeek → 按租户查 None → 静默 fallback
和"压根没配主模型"区分开:前者报错,后者才允许走 fallback/默认。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.services.model_resolution import primary_model_unavailable


def test_fail_loud_when_primary_configured_but_unresolved():
    agent = SimpleNamespace(primary_model_id=uuid4())
    assert primary_model_unavailable(agent, None) is True


def test_no_fail_when_primary_resolves():
    mid = uuid4()
    agent = SimpleNamespace(primary_model_id=mid)
    assert primary_model_unavailable(agent, SimpleNamespace(id=mid)) is False


def test_no_fail_when_no_primary_configured():
    # 没配主模型 — 不算失效,允许后续走 fallback/默认,不应 fail-loud
    agent = SimpleNamespace(primary_model_id=None)
    assert primary_model_unavailable(agent, None) is False


def test_websocket_subscription_is_independent_of_model_resolution():
    """Session transport must become ready before model/provider resolution.

    Model fail-loud remains a RuntimeTask outcome; it must not close or delay
    an otherwise authorized replay subscription.
    """
    import inspect

    import app.api.websocket as ws

    src = inspect.getsource(ws.websocket_chat)
    assert "build_session_ready" in src
    assert "load_session_catchup_window" in src
    assert "primary_model_unavailable" not in src
    assert "select(LLMModel)" not in src


def test_feishu_fails_loud_before_silent_fallback():
    """回归防护:飞书渠道同样必须先 fail-loud 再考虑 fallback。"""
    import inspect

    import app.api.feishu as fs
    import app.services.channel_agent_runtime as channel_runtime

    assert "call_agent_llm" in inspect.getsource(fs)
    src = inspect.getsource(channel_runtime.call_agent_llm)
    assert "primary_model_unavailable" in src
    assert src.index("primary_model_unavailable") < src.index("if not model and fallback_model")
