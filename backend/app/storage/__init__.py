"""Storage backends. `get_storage()` returns the configured one."""
from functools import lru_cache

from app.config import settings
from app.storage.base import (
    FileNotFoundInStorage, FileStorage, StorageConfigurationError, StorageError,
)
from app.storage.local import LocalEncryptedStorage

__all__ = [
    "FileStorage", "StorageError", "StorageConfigurationError",
    "FileNotFoundInStorage", "LocalEncryptedStorage", "get_storage",
]


@lru_cache
def get_storage() -> FileStorage:
    backend = (settings.storage_backend or "local").lower()
    if backend == "local":
        return LocalEncryptedStorage(settings.storage_local_dir)
    if backend == "s3":
        raise StorageConfigurationError(
            "STORAGE_BACKEND=s3 is not built yet. Use STORAGE_BACKEND=local until "
            "an S3-compatible provider has been chosen."
        )
    raise StorageConfigurationError(
        f"Unknown STORAGE_BACKEND {settings.storage_backend!r}. Use 'local'."
    )
