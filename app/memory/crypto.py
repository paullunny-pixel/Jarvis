"""Private-room encryption.

Content tagged PRIVATE (sobriety, meds, family) is encrypted at rest with a
key that lives only in the environment (PRIVATE_ROOM_KEY) — never in the
database. Fernet (AES-128-CBC + HMAC) from the `cryptography` package.
"""
from __future__ import annotations

import base64
import hashlib


def derive_fernet_key(secret: str) -> bytes:
    """Turn any passphrase into a valid 32-byte urlsafe-b64 Fernet key."""
    return base64.urlsafe_b64encode(hashlib.sha256(f"jarvis-private:{secret}".encode()).digest())


class PrivateBox:
    """Encrypts/decrypts private-room content. With no key configured it
    degrades to plaintext (flagged), so nothing breaks before the key is set —
    but Milestone 6 deployment requires the key."""

    def __init__(self, secret: str = "") -> None:
        self._fernet = None
        if secret:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(derive_fernet_key(secret))

    @property
    def active(self) -> bool:
        return self._fernet is not None

    def seal(self, plaintext: str) -> str:
        if self._fernet is None:
            return plaintext
        return "enc:" + self._fernet.encrypt(plaintext.encode()).decode()

    def open(self, stored: str) -> str:
        if stored.startswith("enc:"):
            if self._fernet is None:
                raise RuntimeError("Private content is encrypted but PRIVATE_ROOM_KEY is not set")
            return self._fernet.decrypt(stored[4:].encode()).decode()
        return stored
