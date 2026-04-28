from types import SimpleNamespace
from uuid import uuid4


def test_choose_runtime_model_pair_does_not_invent_default_fallback_when_missing():
    from app.services.model_resolution import choose_runtime_model_pair

    primary = SimpleNamespace(id=uuid4(), model="qwen3.6-plus")
    default = SimpleNamespace(id=uuid4(), model="glm-5.1")

    model, fallback = choose_runtime_model_pair(primary, None, default)

    assert model is primary
    assert fallback is None


def test_choose_runtime_model_pair_drops_duplicate_fallback_instead_of_replacing_it():
    from app.services.model_resolution import choose_runtime_model_pair

    model_id = uuid4()
    primary = SimpleNamespace(id=model_id, model="qwen3.6-plus")
    duplicate_fallback = SimpleNamespace(id=model_id, model="qwen3.6-plus")
    default = SimpleNamespace(id=uuid4(), model="glm-5.1")

    model, fallback = choose_runtime_model_pair(primary, duplicate_fallback, default)

    assert model is primary
    assert fallback is None


def test_choose_runtime_model_pair_promotes_explicit_fallback_when_primary_missing():
    from app.services.model_resolution import choose_runtime_model_pair

    explicit_fallback = SimpleNamespace(id=uuid4(), model="glm-5.1")
    default = SimpleNamespace(id=uuid4(), model="deepseek-v4-pro")

    model, fallback = choose_runtime_model_pair(None, explicit_fallback, default)

    assert model is explicit_fallback
    assert fallback is None
