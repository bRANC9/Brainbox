"""Secret encryption backend abstraction (Phase 5, terv.md 26).

MVP backend encrypts with Fernet (AES-128-CBC + HMAC) using a key derived from
BRAINBOX_SECRET_KEY (falling back to SECRET_KEY so dev works out of the box).
The interface allows swapping in HashiCorp Vault / Azure Key Vault later.
"""

from __future__ import annotations

import base64
import hashlib
from abc import ABC, abstractmethod

from django.conf import settings


class SecretBackendError(RuntimeError):
    pass


class SecretBackend(ABC):
    name = "base"

    @abstractmethod
    def encrypt(self, plaintext: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def decrypt(self, token: str) -> str:
        raise NotImplementedError


def _derive_key() -> bytes:
    raw = settings.BRAINBOX_SECRET_KEY or settings.SECRET_KEY or "brainbox-dev"
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())


class DatabaseSecretBackend(SecretBackend):
    name = "database"

    def __init__(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise SecretBackendError(
                "The 'cryptography' package is required for the secret vault."
            ) from exc
        self._fernet = Fernet(_derive_key())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - invalid token / key mismatch
            raise SecretBackendError("Unable to decrypt secret (key changed?).") from exc


def get_secret_backend() -> SecretBackend:
    return DatabaseSecretBackend()
