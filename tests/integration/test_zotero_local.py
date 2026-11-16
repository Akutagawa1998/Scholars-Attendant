"""Local bridge tests use synthetic temporary directories, never a user library."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import patch
import pytest

from research_assistant.assets import MAX_PDF_BYTES
from research_assistant.connectors import ConnectorError
from research_assistant.connectors.zotero_local import read_local_pdf

KEY = "ABCDEFGH"
PDF = b"%PDF-1.7\n% SYNTHETIC local original\n%%EOF\n"
MD5 = hashlib.md5(PDF, usedforsecurity=False).hexdigest()


@pytest.fixture
def storage(tmp_path):
    root = tmp_path.resolve() / "storage"
    (root / KEY).mkdir(parents=True)
    return root


def test_original_bytes_are_read_only_and_hash_case_normalizes(storage):
    original = storage / KEY / "PRIVATE synthetic title.PDF"
    original.write_bytes(PDF)
    before = original.stat()
    assert read_local_pdf(storage, KEY, MD5.upper()) == PDF
    after = original.stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns)
    assert original.read_bytes() == PDF
    assert sorted(path.name for path in original.parent.iterdir()) == [original.name]


def test_hash_matching_selects_an_immediate_file_without_trusting_provider_name(storage):
    (storage / KEY / "wrong.pdf").write_bytes(PDF + b"different")
    (storage / KEY / "matching.pdf").write_bytes(PDF)
    assert read_local_pdf(storage, KEY, MD5) == PDF


def test_missing_root_key_or_usable_match_is_not_deletion(storage):
    assert read_local_pdf(storage / "not-here", KEY, MD5) is None
    assert read_local_pdf(storage, "IJKLMNOP", MD5) is None
    (storage / KEY / "wrong.pdf").write_bytes(PDF + b"different")
    assert read_local_pdf(storage, KEY, MD5) is None


def test_does_not_recurse_into_subdirectories_or_other_attachment_keys(storage):
    (storage / KEY / "subfolder").mkdir()
    (storage / KEY / "subfolder" / "matching.pdf").write_bytes(PDF)
    (storage / "IJKLMNOP").mkdir()
    (storage / "IJKLMNOP" / "matching.pdf").write_bytes(PDF)
    assert read_local_pdf(storage, KEY, MD5) is None


def test_non_pdf_extension_and_bad_magic_are_not_a_usable_original(storage):
    (storage / KEY / "matching.txt").write_bytes(PDF)
    assert read_local_pdf(storage, KEY, MD5) is None
    bad = b"HTML is not an original PDF"
    (storage / KEY / "bad.pdf").write_bytes(bad)
    assert read_local_pdf(storage, KEY, hashlib.md5(bad, usedforsecurity=False).hexdigest()) is None


def test_symlinked_pdf_is_never_followed(storage):
    outside = storage.parent / "private.pdf"
    outside.write_bytes(PDF)
    (storage / KEY / "linked.pdf").symlink_to(outside)
    with pytest.raises(ConnectorError, match="unsafe_local_storage_path") as caught:
        read_local_pdf(storage, KEY, MD5)
    assert "private.pdf" not in str(caught.value)


def test_symlinked_key_directory_is_never_followed(storage):
    actual = storage / "IJKLMNOP"
    actual.mkdir(); (actual / "matching.pdf").write_bytes(PDF)
    (storage / KEY).rmdir()
    (storage / KEY).symlink_to(actual, target_is_directory=True)
    with pytest.raises(ConnectorError, match="unsafe_local_storage_path"):
        read_local_pdf(storage, KEY, MD5)


def test_symlinked_root_or_ancestor_is_never_followed(storage):
    alias = storage.parent / "alias"
    alias.symlink_to(storage, target_is_directory=True)
    with pytest.raises(ConnectorError, match="unsafe_local_storage_path"):
        read_local_pdf(alias, KEY, MD5)
    wrapper = storage.parent / "wrapper"
    wrapper.symlink_to(storage.parent, target_is_directory=True)
    with pytest.raises(ConnectorError, match="unsafe_local_storage_path"):
        read_local_pdf(wrapper / "storage", KEY, MD5)


@pytest.mark.parametrize("key,md5", [("../ABCDE",MD5),("abcdefgh",MD5),(KEY,"bad"),(KEY,True)])
def test_invalid_identity_is_rejected_before_read(storage, key, md5):
    with pytest.raises(ConnectorError, match="invalid_local_attachment_configuration"):
        read_local_pdf(storage, key, md5)


def test_relative_or_parent_traversal_root_is_rejected(storage):
    with pytest.raises(ConnectorError, match="invalid_local_storage_root"):
        read_local_pdf(Path("relative/storage"), KEY, MD5)
    with pytest.raises(ConnectorError, match="invalid_local_storage_root"):
        read_local_pdf(storage / ".." / "storage", KEY, MD5)


def test_directory_enumeration_is_bounded(storage):
    for n in range(201):
        (storage / KEY / f"synthetic-{n}.txt").write_text("not inspected as PDF")
    with pytest.raises(ConnectorError, match="local_directory_entry_limit"):
        read_local_pdf(storage, KEY, MD5)


def test_large_file_is_rejected_from_stat_before_read(storage):
    target = storage / KEY / "oversized.pdf"
    with target.open("wb") as output:
        output.truncate(MAX_PDF_BYTES + 1)
    with patch("research_assistant.connectors.zotero_local.os.read", side_effect=AssertionError("must not read oversized data")):
        with pytest.raises(ConnectorError, match="local_pdf_too_large"):
            read_local_pdf(storage, KEY, MD5)


def test_nonregular_pdf_named_directory_is_not_read(storage):
    (storage / KEY / "directory.pdf").mkdir()
    assert read_local_pdf(storage, KEY, MD5) is None


def test_concurrent_file_change_is_rejected_before_return(storage):
    target = storage / KEY / "changing.pdf"
    target.write_bytes(PDF)
    real_read = os.read
    changed = False
    def altering_read(fd, count):
        nonlocal changed
        result = real_read(fd, count)
        if result and not changed:
            changed = True
            with target.open("ab") as output:
                output.write(b"\nchanged during read")
        return result
    with patch("research_assistant.connectors.zotero_local.os.read", side_effect=altering_read):
        with pytest.raises(ConnectorError, match="local_file_changed"):
            read_local_pdf(storage, KEY, MD5)


def test_file_replaced_after_open_is_rejected_by_dirfd_stat(storage):
    target = storage / KEY / "replaced.pdf"
    target.write_bytes(PDF)
    real_read = os.read
    changed = False
    def replacing_read(fd, count):
        nonlocal changed
        result = real_read(fd, count)
        if result and not changed:
            changed = True
            target.unlink()
            target.write_bytes(PDF)
        return result
    with patch("research_assistant.connectors.zotero_local.os.read", side_effect=replacing_read):
        with pytest.raises(ConnectorError, match="local_file_changed"):
            read_local_pdf(storage, KEY, MD5)
