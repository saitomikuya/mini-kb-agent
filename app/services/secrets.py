"""Fernet encryption and deliberately limited API-key presentation."""

from base64 import urlsafe_b64encode
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.services.auth import load_or_create_root_secret


_FERNET_SALT = b"mini-kb-agent.model-api-keys.v1"
_FERNET_INFO = b"fernet-key"


class SecretDecryptionError(RuntimeError):
    """Raised without ciphertext or plaintext when stored secret data is invalid."""


class APIKeyCipher:
    """Derive a purpose-specific Fernet key from the stable app root secret."""

    def __init__(self, secret_path: Path) -> None:
        self._secret_path = secret_path
        self._fernet: Fernet | None = None

    def encrypt(self, api_key: str) -> str:
        if not api_key:
            raise ValueError("API key must not be empty")
        return self._get_fernet().encrypt(api_key.encode("utf-8")).decode("ascii")

    def decrypt(self, encrypted_api_key: str) -> str:
        try:
            plaintext = self._get_fernet().decrypt(
                encrypted_api_key.encode("ascii")
            )
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise SecretDecryptionError("Stored API credential cannot be decrypted") from exc
        return plaintext.decode("utf-8")

    def _get_fernet(self) -> Fernet:
        if self._fernet is None:
            root_secret = load_or_create_root_secret(self._secret_path)
            derived = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=_FERNET_SALT,
                info=_FERNET_INFO,
            ).derive(root_secret)
            self._fernet = Fernet(urlsafe_b64encode(derived))
        return self._fernet


def mask_api_key(api_key: str) -> str:
    """Expose only a short prefix and final four characters of a credential."""
    suffix = api_key[-4:] if len(api_key) >= 4 else api_key
    if api_key.startswith("sk-"):
        return f"sk-****{suffix}"
    if len(api_key) >= 8:
        return f"{api_key[:3]}****{suffix}"
    return f"****{suffix}"
