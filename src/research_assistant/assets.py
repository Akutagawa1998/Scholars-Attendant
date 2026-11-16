"""Private immutable PDF blobs and resource-bounded literal page extraction.

A blob hash identifies bytes, never a scientific paper version or a user's
reading. The domain service must authorize access, bind provenance/version and
check reference counts before calling deletion on this storage adapter.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any
from uuid import uuid4

MAX_PDF_BYTES = 50 * 1024 * 1024
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_KEY = re.compile(r"sha256/([a-f0-9]{2})/([a-f0-9]{64})\.pdf\Z")


class AssetError(Exception):
    """A stable, content/path-free operational error."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _checked_pdf(data: bytes, max_bytes: int) -> bytes:
    if not isinstance(data, bytes) or not data.startswith(b"%PDF-"):
        raise AssetError("invalid_pdf_header")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_PDF_BYTES:
        raise AssetError("invalid_pdf_limit")
    if len(data) > max_bytes:
        raise AssetError("pdf_too_large")
    return data


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactStore:
    """POSIX local BlobStore with dirfd-based traversal and no model paths."""
    def __init__(self, root: str | Path, *, max_bytes: int = MAX_PDF_BYTES):
        self._root_fd: int | None = None
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_PDF_BYTES:
            raise AssetError("invalid_pdf_limit")
        self.max_bytes = max_bytes
        try:
            path = Path(root).expanduser().absolute()
            if path.is_symlink():
                raise AssetError("invalid_store_root")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._root_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.fchmod(self._root_fd, 0o700)
        except AssetError:
            raise
        except OSError:
            self.close()
            raise AssetError("store_unavailable") from None

    def __repr__(self):
        return "ArtifactStore(<private>)"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    def __del__(self):
        self.close()

    def _directory(self, shard: str, *, create: bool) -> int:
        if self._root_fd is None:
            raise AssetError("store_closed")
        parent = None
        try:
            if create:
                try:
                    os.mkdir("sha256", mode=0o700, dir_fd=self._root_fd)
                except FileExistsError:
                    pass
            parent = os.open("sha256", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._root_fd)
            if create:
                try:
                    os.mkdir(shard, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            result = os.open(shard, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            if create:
                os.fchmod(parent, 0o700)
                os.fchmod(result, 0o700)
            return result
        except FileNotFoundError:
            raise AssetError("blob_not_found") from None
        except OSError:
            raise AssetError("invalid_blob_path") from None
        finally:
            if parent is not None:
                os.close(parent)

    def _identity(self, relative_blob_key: str, expected_sha256: str) -> tuple[str, str]:
        if not isinstance(relative_blob_key, str) or not isinstance(expected_sha256, str) or not _HASH.fullmatch(expected_sha256):
            raise AssetError("invalid_blob_key")
        matched = _KEY.fullmatch(relative_blob_key)
        if not matched or matched[1] != matched[2][:2] or matched[2] != expected_sha256:
            raise AssetError("invalid_blob_key")
        return matched[1], matched[2] + ".pdf"

    def _read(self, directory: int, filename: str, expected: str) -> bytes:
        handle = None
        try:
            handle = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            if not stat.S_ISREG(os.fstat(handle).st_mode):
                raise AssetError("invalid_blob_path")
            if os.fstat(handle).st_size > self.max_bytes:
                raise AssetError("pdf_too_large")
            with os.fdopen(handle, "rb") as stream:
                handle = None
                data = stream.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise AssetError("pdf_too_large")
            if _hash(data) != expected:
                raise AssetError("blob_integrity_mismatch")
            return data
        except FileNotFoundError:
            raise AssetError("blob_not_found") from None
        except OSError:
            raise AssetError("invalid_blob_path") from None
        finally:
            if handle is not None:
                os.close(handle)

    def put_bytes(self, data: bytes, media_type: str = "application/pdf") -> dict[str, Any]:
        if media_type != "application/pdf":
            raise AssetError("unsupported_asset_media_type")
        data = _checked_pdf(data, self.max_bytes)
        identity = _hash(data)
        directory = self._directory(identity[:2], create=True)
        filename, staging = identity + ".pdf", ".tmp-" + uuid4().hex
        staged = False
        try:
            handle = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            staged = True
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Link is atomic and fails rather than replacing an existing
                # content identity. The staging file already contains all bytes.
                os.link(staging, filename, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                self._read(directory, filename, identity)
            os.unlink(staging, dir_fd=directory)
            staged = False
            os.fsync(directory)
        except AssetError:
            raise
        except OSError:
            raise AssetError("blob_write_failed") from None
        finally:
            if staged:
                try:
                    os.unlink(staging, dir_fd=directory)
                except OSError:
                    pass
            os.close(directory)
        return {"sha256": identity, "size": len(data), "relative_blob_key": f"sha256/{identity[:2]}/{identity}.pdf", "media_type": media_type}

    def read_blob(self, relative_blob_key: str, expected_sha256: str) -> bytes:
        shard, filename = self._identity(relative_blob_key, expected_sha256)
        directory = self._directory(shard, create=False)
        try:
            return self._read(directory, filename, expected_sha256)
        finally:
            os.close(directory)

    def delete_blob(self, relative_blob_key: str, expected_sha256: str) -> bool:
        shard, filename = self._identity(relative_blob_key, expected_sha256)
        try:
            directory = self._directory(shard, create=False)
        except AssetError as error:
            if error.code == "blob_not_found":
                return False
            raise
        try:
            self._read(directory, filename, expected_sha256)
            os.unlink(filename, dir_fd=directory)
            os.fsync(directory)
            return True
        except AssetError as error:
            if error.code == "blob_not_found":
                return False
            raise
        except OSError:
            raise AssetError("blob_delete_failed") from None
        finally:
            os.close(directory)


def _extract_in_worker(data: bytes, *, max_pages: int, max_page_stream_bytes: int, max_text_chars: int) -> dict[str, Any]:
    import logging
    from importlib.metadata import version
    from pypdf import PdfReader
    warnings: list[str] = ["layout_and_semantic_support_not_verified", "ocr_not_performed"]
    class SanitizedWarnings(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING and "parser_warning" not in warnings:
                warnings.append("parser_warning")
    logger = logging.getLogger("pypdf")
    logger.handlers = [SanitizedWarnings()]
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    try:
        reader = PdfReader(io.BytesIO(data), strict=False, root_object_recovery_limit=10000)
        if reader.is_encrypted:
            raise AssetError("encrypted_pdf_unsupported")
        count = len(reader.pages)
        if count == 0:
            raise AssetError("empty_pdf")
        if count > max_pages:
            raise AssetError("pdf_page_limit")
        pages = []
        has_missing, text_chars = False, 0
        for index, page in enumerate(reader.pages):
            page_warnings = []
            text_value = ""
            try:
                content = page.get_contents()
                if content is not None and len(content.get_data()) > max_page_stream_bytes:
                    page_warnings.append("page_content_stream_limit")
                else:
                    text_value = page.extract_text() or ""
                    encoded = text_value.encode("utf-8", errors="replace")
                    normalized = encoded.decode("utf-8")
                    if normalized != text_value:
                        page_warnings.append("invalid_unicode_replaced")
                        text_value = normalized
                    text_chars += len(text_value)
                    if text_chars > max_text_chars:
                        raise AssetError("pdf_text_limit")
                    if not text_value.strip():
                        page_warnings.append("no_extractable_text")
            except AssetError:
                raise
            except Exception:
                page_warnings.append("page_extraction_failed")
            if page_warnings:
                has_missing = True
            pages.append({"page_index": index, "text": text_value, "text_sha256": _hash(text_value.encode("utf-8")), "warnings": page_warnings})
        # A blank page and an image-only page can both lack text. Do not claim
        # OCR is required or that the page was scanned without that evidence.
        quality = "no_extractable_text" if not any(page["text"].strip() for page in pages) else "partial" if has_missing else "text_extracted"
        return {"source_sha256": _hash(data), "source_size": len(data), "parser_name": "pypdf", "parser_version": version("pypdf"),
                "pages": pages, "warnings": warnings, "quality": quality, "page_count": count}
    except AssetError:
        raise
    except Exception:
        raise AssetError("pdf_parse_failed") from None


def extract_pdf(data: bytes, *, max_bytes: int = MAX_PDF_BYTES, max_pages: int = 1000,
                max_page_stream_bytes: int = 16 * 1024 * 1024, max_text_chars: int = 5_000_000,
                timeout_seconds: float = 30, memory_limit_mb: int = 1024) -> dict[str, Any]:
    """Parse in a child process; no PDF actions/scripts or external fetch run.

    Hashes cover raw bytes and each extracted Unicode page separately. A text
    extraction result does not prove reading, layout fidelity or entailment.
    """
    _checked_pdf(data, max_bytes)
    limits = [(max_pages, 1, 10000), (max_page_stream_bytes, 1, 64 * 1024 * 1024),
              (max_text_chars, 1, 20_000_000), (memory_limit_mb, 128, 4096)]
    if any(not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high for value, low, high in limits):
        raise AssetError("invalid_extraction_limit")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
        raise AssetError("invalid_extraction_limit")
    arguments = [sys.executable, "-m", "research_assistant.assets", "--extract-worker",
                 str(max_pages), str(max_page_stream_bytes), str(max_text_chars), str(memory_limit_mb), str(math.ceil(timeout_seconds))]
    try:
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            output, _ = process.communicate(data, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise AssetError("pdf_parse_timeout") from None
        result = json.loads(output)
        if process.returncode != 0 or not isinstance(result, dict) or result.get("status") != "ok":
            code = result.get("error_code") if isinstance(result, dict) else None
            allowed = {"encrypted_pdf_unsupported", "empty_pdf", "pdf_page_limit", "pdf_text_limit", "pdf_parse_failed", "parser_unavailable"}
            raise AssetError(code if code in allowed else "pdf_worker_failed")
        return result["data"]
    except AssetError:
        raise
    except (OSError, ValueError, KeyError):
        raise AssetError("pdf_worker_failed") from None


def anchor_passage(extraction: dict[str, Any], page_index: int, quote: str, *, occurrence: int = 0) -> dict[str, Any]:
    """Literal page-text anchor; character offsets refer to extracted text."""
    if not isinstance(page_index, int) or isinstance(page_index, bool) or page_index < 0 or not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 0 or not isinstance(quote, str) or not quote:
        raise AssetError("invalid_pdf_anchor")
    if not isinstance(extraction, dict):
        raise AssetError("invalid_pdf_anchor")
    pages = extraction.get("pages")
    if not isinstance(pages, list) or page_index >= len(pages) or not isinstance(pages[page_index], dict) or pages[page_index].get("page_index") != page_index:
        raise AssetError("invalid_pdf_anchor")
    page = pages[page_index]
    value = page.get("text")
    source_hash = extraction.get("source_sha256")
    if not isinstance(value, str) or _hash(value.encode("utf-8")) != page.get("text_sha256") or not isinstance(source_hash, str) or not _HASH.fullmatch(source_hash):
        raise AssetError("invalid_pdf_anchor")
    start = -1
    for _ in range(occurrence + 1):
        start = value.find(quote, start + 1)
        if start < 0:
            raise AssetError("quote_not_found")
    return {"text": quote, "locator": {"kind": "pdf", "page_index": page_index,
        "text_start": start, "text_end": start + len(quote), "quote_hash": _hash(quote.encode("utf-8")),
        "source_sha256": extraction["source_sha256"], "page_text_sha256": page["text_sha256"],
        "extraction_version": extraction.get("parser_name", "") + ":" + extraction.get("parser_version", "")},
        "warnings": list(page.get("warnings", []))}


def _worker_main():
    try:
        max_pages, max_stream, max_chars, memory_mb, cpu_seconds = map(int, sys.argv[2:])
        import resource
        limited = True
        for kind, bounds in [(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2),
                             (resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))]:
            try:
                resource.setrlimit(kind, bounds)
            except (ValueError, OSError):
                # Still apply each supported limit. The parent enforces a
                # wall-clock deadline; this is not a claimed OS sandbox.
                limited = False
        data = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
        _checked_pdf(data, MAX_PDF_BYTES)
        result = _extract_in_worker(data, max_pages=max_pages, max_page_stream_bytes=max_stream, max_text_chars=max_chars)
        if not limited:
            result["warnings"].append("os_resource_limits_unavailable")
        print(json.dumps({"status": "ok", "data": result}, ensure_ascii=True))
    except ModuleNotFoundError:
        print(json.dumps({"status": "failed", "error_code": "parser_unavailable"}))
        sys.exit(1)
    except AssetError as error:
        print(json.dumps({"status": "failed", "error_code": error.code}))
        sys.exit(1)
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "pdf_parse_failed"}))
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) == 7 and sys.argv[1] == "--extract-worker":
        _worker_main()
    else:
        raise SystemExit("This module is an internal PDF extraction worker")
