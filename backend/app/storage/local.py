"""Local-disk storage: encrypted files under one root directory."""
import os
import re
import uuid

from app.crypto import decrypt_bytes, encrypt_bytes
from app.storage.base import FileNotFoundInStorage, FileStorage, StorageError

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class LocalEncryptedStorage(FileStorage):
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    # -- key handling ------------------------------------------------------

    @staticmethod
    def _check_segment(value: str, what: str) -> str:
        if not _SAFE_SEGMENT.match(value or ""):
            raise StorageError(f"Invalid {what} for a storage key.")
        return value

    def _resolve(self, key: str, firm_id: str) -> str:
        """Map a key to a path, refusing anything that is not exactly
        `<firm_id>/<category>/<name>.enc` or that escapes the root."""
        parts = (key or "").split("/")
        if len(parts) != 3:
            raise StorageError("Malformed storage key.")
        key_firm, category, name = parts
        for part, what in ((key_firm, "firm"), (category, "category")):
            self._check_segment(part, what)
        if not re.match(r"^[a-f0-9]{32}\.enc$", name):
            raise StorageError("Malformed storage key.")
        if key_firm != firm_id:
            raise StorageError("This file does not belong to your firm.")
        path = os.path.abspath(os.path.join(self.root, key_firm, category, name))
        if os.path.commonpath([self.root, path]) != self.root:
            raise StorageError("Malformed storage key.")
        return path

    # -- interface ---------------------------------------------------------

    def save(self, *, firm_id: str, category: str, data: bytes) -> str:
        self._check_segment(firm_id, "firm")
        self._check_segment(category, "category")
        name = f"{uuid.uuid4().hex}.enc"
        key = f"{firm_id}/{category}/{name}"
        path = self._resolve(key, firm_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write to a temp name then rename, so a crash mid-write never leaves
        # a truncated file under a key the database already points at.
        tmp = f"{path}.part"
        with open(tmp, "wb") as fh:
            fh.write(encrypt_bytes(data))
        os.replace(tmp, path)
        return key

    def load(self, key: str, *, firm_id: str) -> bytes:
        path = self._resolve(key, firm_id)
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except FileNotFoundError as exc:
            raise FileNotFoundInStorage("The stored file is missing.") from exc
        try:
            return decrypt_bytes(blob)
        except ValueError as exc:
            raise StorageError(str(exc)) from exc

    def delete(self, key: str, *, firm_id: str) -> None:
        path = self._resolve(key, firm_id)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def exists(self, key: str, *, firm_id: str) -> bool:
        return os.path.isfile(self._resolve(key, firm_id))
