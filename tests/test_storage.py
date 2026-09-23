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
