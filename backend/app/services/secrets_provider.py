"""Envelope encryption for secrets at rest (API keys, channel credentials).

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a master key derived via HKDF.
The SecretsProvider protocol allows swapping in Vault/KMS without code changes.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Iterable
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

    def decrypt_strict(self, ciphertext: str) -> str: ...

    @property
    def key_id(self) -> str: ...


class FernetSecretsProvider:
    """Fernet-based secrets provider using HKDF-derived key from a master secret."""

    def __init__(self, master_key: str, *, previous_master_keys: Iterable[str] = ()) -> None:
        if not master_key or len(master_key) < 16:
            raise ValueError("SECRETS_MASTER_KEY must be at least 16 characters")
        keys = [master_key, *(key for key in previous_master_keys if key and key != master_key)]
        self._keyring: list[tuple[str, Fernet]] = []
        for key in keys:
            if len(key) < 16:
                raise ValueError("previous SECRETS_MASTER_KEY values must be at least 16 characters")
            derived = HKDF(
                algorithm=SHA256(),
                length=32,
                salt=b"clawith-secrets-v1",
                info=b"fernet-key",
            ).derive(key.encode("utf-8"))
            key_id = hashlib.sha256(derived).hexdigest()[:16]
            self._keyring.append((key_id, Fernet(base64.urlsafe_b64encode(derived))))

    @property
    def key_id(self) -> str:
        return self._keyring[0][0]

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return plaintext
        return self._keyring[0][1].encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt_with_key_id(self, ciphertext: str) -> tuple[str, str]:
        if not ciphertext:
            return ciphertext, self.key_id
        for key_id, fernet in self._keyring:
            try:
                return fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8"), key_id
            except InvalidToken:
                continue
        raise InvalidToken

    def decrypt_strict(self, ciphertext: str) -> str:
        return self.decrypt_with_key_id(ciphertext)[0]

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ciphertext
        try:
            return self.decrypt_strict(ciphertext)
        except InvalidToken:
            logger.warning("Failed to decrypt value (possibly plaintext pre-migration), returning raw")
            return ciphertext


class NoopSecretsProvider:
    """Pass-through provider for development when no master key is set."""

    def encrypt(self, plaintext: str) -> str:
        return plaintext

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext

    def decrypt_strict(self, ciphertext: str) -> str:
        return ciphertext

    @property
    def key_id(self) -> str:
        return "development-noop"


def is_encrypted(value: str) -> bool:
    """Check if a value looks like a Fernet token."""
    return bool(value) and value.startswith(_FERNET_PREFIX)


_provider: SecretsProvider | None = None


def validate_secrets_provider_config(master_key: str | None, *, debug: bool) -> None:
    """Fail closed before a production process can store secrets in plaintext."""
    if master_key or debug:
        return
    raise RuntimeError("SECRETS_MASTER_KEY is required when DEBUG=false; refusing plaintext secret storage")


def init_secrets_provider(
    master_key: str | None = None,
    *,
    previous_master_keys: Iterable[str] = (),
) -> SecretsProvider:
    """Initialize the global secrets provider. Called once at app startup."""
    global _provider
    if master_key:
        _provider = FernetSecretsProvider(master_key, previous_master_keys=previous_master_keys)
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
