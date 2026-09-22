"""Symmetric encryption for credentials the server has to present again.

A polled radar's password is sent to the radar on every poll, so it cannot be
hashed; it is Fernet ciphertext at rest under `POLLED_RADAR_SECRET_KEY`. The key
is read per call, never at import, so a missing or malformed key refuses to
store a credential without stopping the app from booting.
"""

import os

from cryptography.fernet import Fernet

ENV_KEY = "POLLED_RADAR_SECRET_KEY"


class SecretKeyUnavailable(RuntimeError):
    """The key is unset or not a Fernet key, so no credential can be stored."""


def _fernet() -> Fernet:
    raw = os.environ.get(ENV_KEY, "").strip()
    if not raw:
        raise SecretKeyUnavailable(f"{ENV_KEY} is not set, so credentials cannot be stored")
    try:
        return Fernet(raw)
    except ValueError as exc:
        raise SecretKeyUnavailable(f"{ENV_KEY} is not a valid Fernet key") from exc


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Raises `cryptography.fernet.InvalidToken` for ciphertext from another key."""
    return _fernet().decrypt(ciphertext.encode()).decode()
