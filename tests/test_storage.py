import hashlib
from pathlib import Path

import pytest

from procurement_bot.storage import LocalContentAddressedStorage


def test_content_addressed_storage_is_idempotent(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_text("same payload", encoding="utf-8")
    storage = LocalContentAddressedStorage(tmp_path / "objects")

    first = storage.put_file(source, namespace="telegram", suffix=".txt")
    second = storage.put_file(source, namespace="telegram", suffix=".txt")

    assert first.object_key == second.object_key
    assert first.path.read_text(encoding="utf-8") == "same payload"
    assert first.path.stat().st_mode & 0o777 == 0o600


def test_storage_rejects_escaping_key(tmp_path: Path):
    storage = LocalContentAddressedStorage(tmp_path / "objects")
    with pytest.raises(ValueError, match="escaped"):
        storage.resolve("../outside")


def test_storage_repairs_corrupt_existing_content_addressed_object(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"expected content")
    storage = LocalContentAddressedStorage(tmp_path / "objects")
    first = storage.put_file(source, namespace="telegram", suffix=".txt")
    first.path.write_bytes(b"corrupt content")

    repaired = storage.put_file(source, namespace="telegram", suffix=".txt")

    assert repaired.object_key == first.object_key
    assert repaired.path.read_bytes() == source.read_bytes()
    assert repaired.sha256 == hashlib.sha256(repaired.path.read_bytes()).hexdigest()
    assert repaired.byte_size == len(source.read_bytes())


def test_storage_rejects_symlink_at_content_addressed_destination(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"expected content")
    storage = LocalContentAddressedStorage(tmp_path / "objects")
    first = storage.put_file(source, namespace="telegram", suffix=".txt")
    target = storage.root / "safe-target"
    target.write_bytes(b"safe target")
    first.path.unlink()
    first.path.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        storage.put_file(source, namespace="telegram", suffix=".txt")

    assert target.read_bytes() == b"safe target"
