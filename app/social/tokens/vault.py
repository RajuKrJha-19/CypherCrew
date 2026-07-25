"""Token vault: Fernet encryption-at-rest for platform tokens.

Access/refresh tokens are secrets. They are Fernet-encrypted with a key
that lives OUTSIDE the database (config.SOCIAL_TOKEN_KEY) and are only ever
decrypted in-memory at call time - never logged.

Key rotation: SOCIAL_TOKEN_KEY may be a comma-separated list. The first key
encrypts new values (the "primary"); all keys can decrypt, via MultiFernet,
so you rotate by prepending a new key and re-encrypting lazily. Each stored
row records `token_key_version` for observability.
"""

from cryptography.fernet import Fernet, MultiFernet


class VaultDisabled(RuntimeError):
    """Raised when a token operation is attempted but no key is configured."""


class TokenVault:
    def __init__(self, keys: list[str], version: int = 1):
        if not keys:
            raise ValueError("TokenVault requires at least one key")
        self._fernet = MultiFernet([Fernet(k.encode() if isinstance(k, str) else k)
                                    for k in keys])
        self.version = version

    @classmethod
    def from_config(cls, config) -> "TokenVault | None":
        """Build from a Flask config (or any mapping). Returns None when no
        key is set, so callers can treat "no vault" as "storing tokens is
        disabled" rather than crashing at import time."""
        raw = (config.get("SOCIAL_TOKEN_KEY") or "").strip()
        if not raw:
            return None
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not keys:
            return None
        version = int(config.get("SOCIAL_TOKEN_KEY_VERSION", 1) or 1)
        return cls(keys, version)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()


def get_vault():
    """The app-bound vault, cached on the app. Raises VaultDisabled when no
    key is configured so the connect flow fails closed (never stores a
    plaintext token)."""
    from flask import current_app

    vault = current_app.extensions.get("social_token_vault")
    if vault is None and "social_token_vault" not in current_app.extensions:
        vault = TokenVault.from_config(current_app.config)
        current_app.extensions["social_token_vault"] = vault
    if vault is None:
        raise VaultDisabled(
            "SOCIAL_TOKEN_KEY is not set; cannot encrypt/decrypt tokens."
        )
    return vault
