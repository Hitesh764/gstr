"""
File storage interface.

Uploaded documents (GST notices today; the client document vault later) go
through this interface rather than touching the filesystem directly, so the
place they live can change without touching any calling code. A local-disk
backend exists now; an S3-compatible one is a second implementation of the
same four methods.

Rules every backend must honour:

* **Encrypt before writing.** Callers hand over plaintext; what reaches the
  disk or the bucket is ciphertext. A stolen disk image or bucket export
  yields nothing readable without ENCRYPTION_KEY.
* **Keys are generated here, never supplied by callers.** A caller-chosen
  path is a path-traversal bug waiting for its first user-controlled filename.
  The original filename is kept in the database, not in the key.
* **Keys are namespaced by firm.** `save()` takes the firm id and prefixes it,
  so a key is self-describing and `load()` can be asked to confirm a key
  belongs to the firm the caller believes it does.
"""
from abc import ABC, abstractmethod


class StorageError(RuntimeError):
    """A file could not be stored, found or read."""


class StorageConfigurationError(StorageError):
    """The configured backend is unknown or not available yet."""


class FileNotFoundInStorage(StorageError):
    pass


class FileStorage(ABC):
    @abstractmethod
    def save(self, *, firm_id: str, category: str, data: bytes) -> str:
        """Encrypt and store `data`. Returns the opaque key to keep in the database."""

    @abstractmethod
    def load(self, key: str, *, firm_id: str) -> bytes:
        """Return the decrypted bytes.

        Raises `StorageError` if `key` does not belong to `firm_id`, even if
        the file exists: a wrong-firm key is a bug or an attack, never a
        legitimate read.
        """

    @abstractmethod
    def delete(self, key: str, *, firm_id: str) -> None:
        """Remove a file. Missing files are not an error."""

    @abstractmethod
    def exists(self, key: str, *, firm_id: str) -> bool: ...
