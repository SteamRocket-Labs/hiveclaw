from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_web_chat_terminal_delivery_uses_durable_outbox_not_direct_provider_send():
    source = (ROOT / "app/services/web_chat_runtime.py").read_text(encoding="utf-8")
    assert "enqueue_channel_delivery" in source
    assert "async def _deliver_run_result_to_channel" not in source
    assert "await ChannelDeliveryService.send_text" not in source


def test_runtime_worker_drains_channel_delivery_outbox():
    source = (ROOT / "app/services/runtime_task_worker.py").read_text(encoding="utf-8")
    assert "ChannelDeliveryOutboxService" in source
    assert "drain_channel_delivery_outbox_once" in source


def test_business_task_terminal_projection_uses_the_same_channel_delivery_outbox():
    source = (ROOT / "app/services/business_task_runtime.py").read_text(encoding="utf-8")
    assert "_enqueue_business_task_channel_delivery" in source
    assert "enqueue_channel_delivery" in source
