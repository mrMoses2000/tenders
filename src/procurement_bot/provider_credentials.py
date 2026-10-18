"""Private file-state checks for configured provider credential profiles.

This module deliberately does not retain credential bytes or compute/log a digest.
When a credential source changes path, it can compare the two private files in
bounded chunks to distinguish a source fallback from an actual token rotation.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CredentialFileVersion:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


def credential_file_version(path: Path) -> CredentialFileVersion | None:
    """Return nonsecret filesystem metadata for a nonempty regular file."""

    try:
        info = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        return None
    return CredentialFileVersion(
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        modified_ns=info.st_mtime_ns,
        changed_ns=info.st_ctime_ns,
    )


def same_credential_file_contents(left: Path, right: Path) -> bool:
    """Compare two credential files without retaining or exporting their bytes."""

    left_before = credential_file_version(left)
    right_before = credential_file_version(right)
    if left_before is None or right_before is None:
        return False
    if (left_before.device, left_before.inode) == (
        right_before.device,
        right_before.inode,
    ):
        return True
    if left_before.size != right_before.size:
        return False

    try:
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(16_384)
                right_chunk = right_handle.read(16_384)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    break
    except OSError:
        return False

    # A concurrent rewrite during the read must not validate a torn comparison.
    return (
        credential_file_version(left) == left_before
        and credential_file_version(right) == right_before
    )


def provider_auth_wait_reason(diagnostic_code: object) -> str | None:
    """Map only the exact typed AGY auth diagnostic to the failed-auth reason."""

    if diagnostic_code == "cli_auth_failed":
        return "provider_auth_failed"
    return None


def same_file_identity(left: Path, right: Path) -> bool:
    """Tell whether two paths currently refer to the same regular file."""

    left_version = credential_file_version(left)
    right_version = credential_file_version(right)
    return (
        left_version is not None
        and right_version is not None
        and (left_version.device, left_version.inode)
        == (right_version.device, right_version.inode)
    )
