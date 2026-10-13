"""
Encryption at rest for genuinely secret values.

What is encrypted here
----------------------
GSP consent tokens and TOTP secrets. These are credentials: if they leak,
someone can act as the firm against the GST portal or bypass a second
factor. They are never aggregated, filtered or sorted in SQL, so encrypting
them costs nothing operationally.

What is deliberately NOT encrypted here
---------------------------------------
Monetary columns (taxable_value, cgst, ...). The dashboards SUM and compare
these in SQL, and a column encrypted at the application layer is opaque to
the database: no SUM, no ORDER BY, no range filter. Encrypting them would
mean loading every row of every client into Python to add them up, which
does not survive a firm with 200 clients. Amounts are protected by
database-level and disk-level encryption instead, which is the standard
answer for data that must stay queryable.

GSTINs sit in between. They must be searchable, so `blind_index` produces a
deterministic keyed hash that can be indexed and matched exactly, without
storing the raw value in the index.
"""
import base64
import hashlib
import hmac
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _fernet() -> Fernet:
    key = settings.encryption_key
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not configured. Set it in backend/.env — see .env.example."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: Optional[str]) -> Optional[str]:
    """Encrypt a string for storage. None passes through unchanged."""
    if plaintext is None or plaintext == "":
        return plaintext
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: Optional[str]) -> Optional[str]:
    """Decrypt a stored string.

    Raises ValueError when the value cannot be decrypted, which in practice
    means the ENCRYPTION_KEY has changed since the value was written. That
    is worth failing loudly on rather than returning None, because silently
    dropping a GSP token looks identical to "consent was never granted".
    """
    if ciphertext is None or ciphertext == "":
        return ciphertext
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "Could not decrypt a stored secret. The ENCRYPTION_KEY has most likely "
            "changed since this value was written; the original key is required."
        ) from exc


def blind_index(value: Optional[str]) -> Optional[str]:
    """Deterministic keyed hash, for exact-match lookups on a secret column.

    Same input and key always yield the same output, so the column can be
    indexed and searched, while the raw value is not recoverable from it.
    Deterministic by design, which means it leaks equality — two identical
    GSTINs produce identical hashes. That is the trade required to search.
    """
    if value is None or value == "":
        return value
    key = settings.encryption_key.encode()
    normalized = value.strip().upper().encode("utf-8")
    return hmac.new(key, normalized, hashlib.sha256).hexdigest()


def hash_token(token: str) -> str:
    """Hash a bearer-style token (refresh, invite, password reset) for storage.

    Plain SHA-256 rather than a password hash: these tokens are already
    high-entropy random values, so they are not brute-forceable and do not
    need a slow KDF. Storing the hash means a database leak yields nothing
    replayable.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token(nbytes: int = 32) -> str:
    """A URL-safe random token for refresh/invite/reset flows."""
    import secrets
    return secrets.token_urlsafe(nbytes)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


def encrypt_bytes(data: bytes) -> bytes:
    """Encrypt a file's contents (Fernet: AES-128-CBC + HMAC-SHA256).

    Whole-file, in memory. Fine for the size limits enforced on uploads
    (notices are a few MB at most); a streaming scheme would be needed before
    accepting files an order of magnitude larger.
    """
    return _fernet().encrypt(data)


def decrypt_bytes(blob: bytes) -> bytes:
    """Decrypt bytes written by `encrypt_bytes`. Raises ValueError on a wrong key
    or a file that has been altered, for the same reason `decrypt` does."""
    try:
        return _fernet().decrypt(blob)
    except InvalidToken as exc:
        raise ValueError(
            "Could not decrypt a stored file. Either the ENCRYPTION_KEY has changed "
            "since it was written, or the file has been altered on disk."
        ) from exc
