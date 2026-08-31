from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def test_validate_secrets_provider_config_rejects_plaintext_provider_in_production() -> None:
    from app.services.secrets_provider import validate_secrets_provider_config

    with pytest.raises(RuntimeError, match="SECRETS_MASTER_KEY is required"):
        validate_secrets_provider_config(None, debug=False)


def test_validate_secrets_provider_config_allows_plaintext_provider_only_in_debug() -> None:
    from app.services.secrets_provider import validate_secrets_provider_config

    validate_secrets_provider_config(None, debug=True)
    validate_secrets_provider_config("a" * 16, debug=False)


def test_historical_kdf_salt_remains_the_stable_encryption_domain() -> None:
    from app.services.secrets_provider import get_secrets_provider, init_secrets_provider

    master_key = "historical-migration-test-master-key"
    historical_salt = b"clawith-secrets-v1"
    historical_derived_key = HKDF(
        algorithm=SHA256(),
        length=32,
        salt=historical_salt,
        info=b"fernet-key",
    ).derive(master_key.encode("utf-8"))
    historical_fernet = Fernet(base64.urlsafe_b64encode(historical_derived_key))
    historical_ciphertext = historical_fernet.encrypt(b"credential").decode("ascii")

    provider = init_secrets_provider(master_key)

    assert get_secrets_provider() is provider
    assert provider.decrypt_strict(historical_ciphertext) == "credential"
    assert historical_fernet.decrypt(provider.encrypt("new credential").encode("ascii")) == b"new credential"

    provider_without_key = init_secrets_provider("different-migration-test-master-key")
    with pytest.raises(InvalidToken):
        provider_without_key.decrypt(historical_ciphertext)
    assert provider_without_key.decrypt("plaintext awaiting migration") == "plaintext awaiting migration"
