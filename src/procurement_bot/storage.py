from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class StoredObject:
    object_key: str
    path: Path
    sha256: str
    byte_size: int


class LocalContentAddressedStorage:
    """Private local storage with an S3-compatible conceptual boundary."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def put_file(self, source: Path, *, namespace: str, suffix: str = "") -> StoredObject:
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        sha256 = digest.hexdigest()
        safe_suffix = suffix if suffix.startswith(".") and len(suffix) <= 20 else ""
        relative = Path(namespace) / sha256[:2] / f"{sha256}{safe_suffix}"
        destination = (self.root / relative).resolve()
        if not destination.is_relative_to(self.root):
            raise ValueError("object path escaped storage root")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not destination.exists():
            self._atomic_copy(source.open("rb"), destination)
        destination.chmod(0o600)
        return StoredObject(str(relative), destination, sha256, destination.stat().st_size)

    def resolve(self, object_key: str) -> Path:
        path = (self.root / object_key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("object key escaped storage root")
        return path

    @staticmethod
    def _atomic_copy(source: BinaryIO, destination: Path) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=".incoming-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            temporary.replace(destination)
        finally:
            source.close()
            temporary.unlink(missing_ok=True)
