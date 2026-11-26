"""Local file storage for uploaded PDFs.

Security rules:
  * The user's filename is NEVER used on disk. Files are stored as
    "<user_uuid>/<random_hex>.pdf" -> no path traversal ("../../etc/passwd"),
    no overwriting other files, no special characters.
  * Every key is validated against a strict regex AND the resolved path must
    stay inside the storage root (defence in depth).
  * Uploads are streamed in 1 MB pieces, never read fully into memory, and the
    size limit is enforced while streaming.
  * Data is written to a ".part" file and atomically renamed when complete, so
    a crash never leaves a half-written file under a real key.

The interface (save / open path / delete) is small on purpose, so an S3
implementation can replace this class later without touching callers.
"""

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.core.logging import get_logger

logger = get_logger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MB
_KEY_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{32}\.pdf$")


class StorageError(Exception):
    pass


class FileTooLargeError(StorageError):
    pass


class EmptyFileError(StorageError):
    pass


@dataclass(frozen=True)
class StoredFile:
    key: str
    size: int
    sha256: str
    path: Path


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def save_stream(self, stream: BinaryIO, owner_id: uuid.UUID, max_bytes: int) -> StoredFile:
        key = f"{owner_id}/{uuid.uuid4().hex}.pdf"
        final_path = self.path_for(key)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = final_path.with_suffix(".part")

        digest = hashlib.sha256()
        size = 0
        try:
            with tmp_path.open("xb") as out:  # "x" = fail if the file already exists
                while chunk := stream.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileTooLargeError(f"File exceeds {max_bytes} bytes")
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise EmptyFileError("File is empty")
            os.replace(tmp_path, final_path)  # atomic on the same filesystem
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return StoredFile(key=key, size=size, sha256=digest.hexdigest(), path=final_path)

    def path_for(self, key: str) -> Path:
        if not _KEY_PATTERN.fullmatch(key):
            raise StorageError("Invalid storage key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):  # belt and braces
            raise StorageError("Invalid storage key")
        return path

    def delete(self, key: str) -> bool:
        """Delete a stored file. Returns False if it was already gone."""
        try:
            self.path_for(key).unlink()
            return True
        except FileNotFoundError:
            return False

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()
