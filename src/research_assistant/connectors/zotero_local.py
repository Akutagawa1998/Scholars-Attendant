"""Read-only local Zotero attachment bridge, with no traversal or persistence.

The caller provides the already authorized library attachment key and expected
provider MD5. Only immediate regular PDF files under storage/<attachment key>
are considered. The original library is neither moved nor modified. A matching
file supplies bytes; its local name/path never leaves this adapter.
"""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import re
import stat

from ..assets import MAX_PDF_BYTES
from .common import ConnectorError

MAX_DIRECTORY_ENTRIES = 200
_KEY = re.compile(r"[A-Z0-9]{8}\Z")
_MD5 = re.compile(r"[a-fA-F0-9]{32}\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _file_identity(info: os.stat_result):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _directory(path: Path) -> int | None:
    """Walk every absolute path component through O_NOFOLLOW directory FDs."""
    if not path.is_absolute() or any(part in {"..", "."} for part in path.parts):
        raise ConnectorError("invalid_local_storage_root")
    descriptor = None
    try:
        descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
        for component in path.parts[1:]:
            next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except FileNotFoundError:
        if descriptor is not None:
            os.close(descriptor)
        return None
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        code = "unsafe_local_storage_path" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "local_file_unavailable"
        raise ConnectorError(code) from None
    except (ValueError, TypeError):
        if descriptor is not None:
            os.close(descriptor)
        raise ConnectorError("invalid_local_storage_root") from None


def _candidate(key_descriptor: int, name: str, expected_md5: str) -> bytes | None:
    descriptor = None
    try:
        before_path = os.stat(name, dir_fd=key_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(before_path.st_mode):
            raise ConnectorError("unsafe_local_storage_path")
        if not stat.S_ISREG(before_path.st_mode):
            return None
        if before_path.st_size > MAX_PDF_BYTES:
            raise ConnectorError("local_pdf_too_large")
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=key_descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _file_identity(before) != _file_identity(before_path):
            raise ConnectorError("local_file_changed")
        total = 0
        chunks = bytearray()
        checksum = hashlib.md5(usedforsecurity=False)
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                raise ConnectorError("local_pdf_too_large")
            checksum.update(chunk)
            chunks.extend(chunk)
        after = os.fstat(descriptor)
        after_path = os.stat(name, dir_fd=key_descriptor, follow_symlinks=False)
        if (_file_identity(before) != _file_identity(after)
                or _file_identity(after) != _file_identity(after_path)
                or total != before.st_size):
            raise ConnectorError("local_file_changed")
        if not chunks.startswith(b"%PDF-") or checksum.hexdigest() != expected_md5:
            return None
        return bytes(chunks)
    except FileNotFoundError:
        raise ConnectorError("local_file_changed") from None
    except OSError as exc:
        code = "unsafe_local_storage_path" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "local_file_unavailable"
        raise ConnectorError(code) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_local_pdf(storage_root: Path, attachment_key: str, expected_md5: str) -> bytes | None:
    """Return a stable hash-matching original PDF, or None if no usable match.

    Missing root/key directories and unmatched immediate PDFs return None;
    symlinks, concurrent replacement, I/O failures and explicit bounds use a
    sanitized ConnectorError. No local path or private filename enters errors.
    Provider 404/cloud failure is not interpreted here and never means deletion.
    """
    if (not isinstance(storage_root, Path) or not isinstance(attachment_key, str)
            or not _KEY.fullmatch(attachment_key) or not isinstance(expected_md5, str)
            or not _MD5.fullmatch(expected_md5)):
        raise ConnectorError("invalid_local_attachment_configuration")
    try:
        storage_root = storage_root.expanduser()
    except (RuntimeError, ValueError, TypeError):
        raise ConnectorError("invalid_local_storage_root") from None
    root_descriptor = _directory(storage_root)
    if root_descriptor is None:
        return None
    key_descriptor = None
    try:
        try:
            key_descriptor = os.open(attachment_key, _DIRECTORY_FLAGS, dir_fd=root_descriptor)
        except FileNotFoundError:
            return None
        except OSError as exc:
            code = "unsafe_local_storage_path" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "local_file_unavailable"
            raise ConnectorError(code) from None
        try:
            with os.scandir(key_descriptor) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_DIRECTORY_ENTRIES:
                        raise ConnectorError("local_directory_entry_limit")
                    if not entry.name.lower().endswith(".pdf"):
                        continue
                    result = _candidate(key_descriptor, entry.name, expected_md5.lower())
                    if result is not None:
                        return result
        except OSError:
            raise ConnectorError("local_file_unavailable") from None
        return None
    finally:
        if key_descriptor is not None:
            os.close(key_descriptor)
        os.close(root_descriptor)
