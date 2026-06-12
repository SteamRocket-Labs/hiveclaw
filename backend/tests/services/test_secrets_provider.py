from __future__ import annotations

import pytest


def test_validate_secrets_provider_config_rejects_plaintext_provider_in_production() -> None:
    from app.services.secrets_provider import validate_secrets_provider_config

    with pytest.raises(RuntimeError, match="SECRETS_MASTER_KEY is required"):
        validate_secrets_provider_config(None, debug=False)


def test_validate_secrets_provider_config_allows_plaintext_provider_only_in_debug() -> None:
    from app.services.secrets_provider import validate_secrets_provider_config

    validate_secrets_provider_config(None, debug=True)
    validate_secrets_provider_config("a" * 16, debug=False)
