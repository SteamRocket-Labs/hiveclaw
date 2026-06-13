"""B-fail-loud: 主模型配了却解析不到时,运行时必须 fail-loud,不得静默降级。

Web3研究员事故:agent 主模型指向别租户 DeepSeek → 按租户查 None → 静默 fallback
到 20万窗口的 MiniMax → deep research 反复压缩卡死。判定逻辑必须把"配了但没解析到"
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


def test_websocket_fails_loud_before_silent_fallback():
    """回归防护:web chat 里 fail-loud 判定必须在 config-level fallback 之前,
    否则主模型失效会先被静默 fallback 吃掉(Web3研究员事故)。"""
    import inspect

    import app.api.websocket as ws

    src = inspect.getsource(ws.websocket_chat)
    assert "primary_model_unavailable" in src
    assert src.index("primary_model_unavailable") < src.index("Config-level fallback")


def test_feishu_fails_loud_before_silent_fallback():
    """回归防护:飞书渠道同样必须先 fail-loud 再考虑 fallback。"""
    import inspect

    import app.api.feishu as fs

    src = inspect.getsource(fs)
    assert "primary_model_unavailable" in src
    assert src.index("primary_model_unavailable") < src.index("Config-level fallback: primary missing")
