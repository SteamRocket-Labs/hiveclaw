"""Envelope encryption for secrets at rest (API keys, channel credentials).

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a master key derived via HKDF.
The SecretsProvider protocol allows swapping in Vault/KMS without code changes.
"""

from __future__ import annotations

import base64
import logging
from typing import Protocol, runtime_checkable

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

_FERNET_PREFIX = "gAAAAA"


@runtime_checkable
class SecretsProvider(Protocol):
    """Interface for encrypting/decrypting secrets at rest."""

    def encrypt(self, plaintext: str) -> str: ...

    def decrypt(self, ciphertext: str) -> str: ...


class FernetSecretsProvider:
    """Fernet-based secrets provider using HKDF-derived key from a master secret."""

    def __init__(self, master_key: str) -> None:
        if not master_key or len(master_key) < 16:
            raise ValueError("SECRETS_MASTER_KEY must be at least 16 characters")
        derived = HKDF(
            algorithm=SHA256(),
            length=32,
            salt=b"clawith-secrets-v1",
            info=b"fernet-key",
        ).derive(master_key.encode("utf-8"))
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return plaintext
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ciphertext
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken:
            logger.warning("Failed to decrypt value (possibly plaintext pre-migration), returning raw")
            return ciphertext


class NoopSecretsProvider:
    """Pass-through provider for development when no master key is set."""

    def encrypt(self, plaintext: str) -> str:
        return plaintext

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext


def is_encrypted(value: str) -> bool:
    """Check if a value looks like a Fernet token."""
    return bool(value) and value.startswith(_FERNET_PREFIX)


_provider: SecretsProvider | None = None


def init_secrets_provider(master_key: str | None = None) -> SecretsProvider:
    """Initialize the global secrets provider. Called once at app startup."""
    global _provider
    if master_key:
        _provider = FernetSecretsProvider(master_key)
        logger.info("SecretsProvider initialized with Fernet encryption")
    else:
        _provider = NoopSecretsProvider()
        logger.warning("SECRETS_MASTER_KEY not set — secrets stored in plaintext (dev mode only)")
    return _provider


def get_secrets_provider() -> SecretsProvider:
    """Get the global secrets provider instance."""
    if _provider is None:
        raise RuntimeError("SecretsProvider not initialized — call init_secrets_provider() at startup")
    return _provider
