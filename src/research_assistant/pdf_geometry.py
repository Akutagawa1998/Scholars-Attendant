"""Optional, local PDFium geometry in native, unrotated PDF user space.

The operator supplies a Python runtime which already contains pypdfium2 and
pypdf. This module installs nothing. The worker receives bytes over bounded
IPC, opens no PDF source files, and never interprets PDF JavaScript or actions.
Returned indexes identify PDFium characters, NOT canonical pypdf text offsets.
CropBox, MediaBox, Rotate and UserUnit are metadata; character boxes are never
rebased, rotated or scaled by this adapter. This behavior is fixture-checked for
the installed engine and is not a claim that every producer uses those units.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time
from typing import Any

MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_REQUESTED_PAGES = 20
MAX_DOCUMENT_PAGES = 20000
MAX_ENGINE_CHARACTERS = 200000
MAX_TEXT_BYTES = 800000
MAX_OUTPUT_BYTES = 48 * 1024 * 1024
MAX_INPUT_BYTES = ((MAX_PDF_BYTES + 2) // 3) * 4 + 4096
MAX_RSS_BYTES = 1024 * 1024 * 1024
WALL_SECONDS = 30.0
CPU_SECONDS = 25
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_VERSION = re.compile(r"[0-9][A-Za-z0-9.+_-]{0,63}\Z")
_ERRORS = frozenset({
    "invalid_pdf_geometry_input", "pdf_geometry_pdf_too_large",
    "pdf_geometry_runtime_unavailable", "pdf_geometry_engine_unavailable",
    "pdf_geometry_invalid_pdf", "pdf_geometry_encrypted_pdf",
    "pdf_geometry_page_limit", "pdf_geometry_page_out_of_range",
    "pdf_geometry_character_limit", "pdf_geometry_output_limit",
    "pdf_geometry_text_limit", "pdf_geometry_invalid_unicode",
    "pdf_geometry_invalid_coordinates", "pdf_geometry_input_mismatch",
    "pdf_geometry_resource_limit_unavailable", "pdf_geometry_memory_limit",
    "pdf_geometry_timeout", "pdf_geometry_worker_failed",
    "pdf_geometry_invalid_response",
})


class PdfGeometryError(Exception):
    """Fixed, content/path-free diagnostic code; parser errors never escape."""

    def __init__(self, code: str):
        self.code = code if code in _ERRORS else "pdf_geometry_worker_failed"
        super().__init__(self.code)


def _indices(value: Any) -> list[int]:
    if (not isinstance(value, list) or not 1 <= len(value) <= MAX_REQUESTED_PAGES
            or any(type(i) is not int or not 0 <= i < MAX_DOCUMENT_PAGES for i in value)
            or len(set(value)) != len(value)):
        raise PdfGeometryError("invalid_pdf_geometry_input")
    return value


def _checked_input(data: Any, page_indices: Any) -> tuple[bytes, list[int]]:
    if not isinstance(data, bytes) or not data.startswith(b"%PDF-"):
        raise PdfGeometryError("invalid_pdf_geometry_input")
    if len(data) > MAX_PDF_BYTES:
        raise PdfGeometryError("pdf_geometry_pdf_too_large")
    return data, _indices(page_indices)


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e12


def _box(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 4 and all(_finite(v) for v in value)
            and value[0] < value[2] and value[1] < value[3])


def _validate_result(result: Any, digest: str, indices: list[int]) -> dict[str, Any]:
    """Treat even the optional local parser's response as untrusted input."""
    bad = PdfGeometryError("pdf_geometry_invalid_response")
    if not isinstance(result, dict) or set(result) != {
        "engine", "source_sha256", "page_count", "pages", "limits",
    }:
        raise bad
    if result["source_sha256"] != digest:
        raise bad
    engine = result["engine"]
    if (not isinstance(engine, dict) or set(engine) != {"name", "version", "pdfium_version"}
            or engine["name"] != "pypdfium2"
            or any(not isinstance(engine[k], str) or not _VERSION.fullmatch(engine[k])
                   for k in ("version", "pdfium_version"))):
        raise bad
    count = result["page_count"]
    if type(count) is not int or not 1 <= count <= MAX_DOCUMENT_PAGES or max(indices) >= count:
        raise bad
    limits = result["limits"]
    if limits != {
        "cpu_seconds": CPU_SECONDS, "wall_seconds": WALL_SECONDS,
        "rss_bytes": MAX_RSS_BYTES, "memory_enforcement": "parent_rss_monitor",
    }:
        raise bad
    pages = result["pages"]
    if not isinstance(pages, list) or len(pages) != len(indices):
        raise bad
    total = total_text = 0
    for index, page in zip(indices, pages):
        if not isinstance(page, dict) or set(page) != {
            "page_index", "media_box", "crop_box", "rotation", "user_unit",
            "coordinate_space", "coordinate_semantics", "index_semantics",
            "engine_character_count", "skipped_characters", "characters", "text_units",
        }:
            raise bad
        if (type(page["page_index"]) is not int or page["page_index"] != index
                or not _box(page["media_box"]) or not _box(page["crop_box"])
                or type(page["rotation"]) is not int or page["rotation"] not in (0, 90, 180, 270)
                or not _finite(page["user_unit"]) or not 0 < page["user_unit"] <= 75000
                or page["coordinate_space"] != "pdf_user_space"
                or page["coordinate_semantics"] != "unrotated_unscaled"
                or page["index_semantics"] != "pdfium_character_index"):
            raise bad
        char_count = page["engine_character_count"]
        skipped = page["skipped_characters"]
        chars = page["characters"]
        units = page["text_units"]
        if (type(char_count) is not int or not 0 <= char_count <= MAX_ENGINE_CHARACTERS
                or not isinstance(skipped, dict) or set(skipped) != {"zero_area", "unmapped_unicode"}
                or any(type(v) is not int or not 0 <= v <= char_count for v in skipped.values())
                or not isinstance(chars, list) or len(chars) > char_count
                or not isinstance(units, list) or len(units) > char_count):
            raise bad
        total += char_count
        unit_map = {}
        last_end = unit_covered = 0
        for unit in units:
            if not isinstance(unit, dict) or set(unit) != {"index", "index_span", "text"}:
                raise bad
            i, span, text = unit["index"], unit["index_span"], unit["text"]
            if (type(i) is not int or type(span) is not int or span not in (1, 2)
                    or not last_end <= i < char_count or i + span > char_count
                    or not isinstance(text, str) or len(text) != 1 or ord(text) == 0
                    or 0xD800 <= ord(text) <= 0xDFFF or (span == 2 and ord(text) <= 0xFFFF)):
                raise bad
            last_end = i + span
            unit_covered += span
            total_text += len(text.encode("utf-8"))
            unit_map[i] = unit
        if unit_covered + skipped["unmapped_unicode"] != char_count:
            raise bad
        last_end = 0
        covered = 0
        for char in chars:
            if not isinstance(char, dict) or set(char) != {"index", "index_span", "text", "bbox"}:
                raise bad
            i, span, text = char["index"], char["index_span"], char["text"]
            if (type(i) is not int or type(span) is not int or span not in (1, 2)
                    or not last_end <= i < char_count or i + span > char_count or not isinstance(text, str)
                    or len(text) != 1 or ord(text) == 0 or 0xD800 <= ord(text) <= 0xDFFF
                    or (span == 2 and ord(text) <= 0xFFFF)
                    or not _box(char["bbox"])):
                raise bad
            if unit_map.get(i) != {"index": i, "index_span": span, "text": text}:
                raise bad
            last_end = i + span
            covered += span
        if covered + sum(skipped.values()) != char_count:
            raise bad
    if total > MAX_ENGINE_CHARACTERS or total_text > MAX_TEXT_BYTES:
        raise bad
    return result


def _rss_bytes(pid: int) -> int | None:
    """Read RSS only; no process environment, arguments or private source paths."""
    try:
        if sys.platform.startswith("linux"):
            raw = Path(f"/proc/{pid}/statm").read_text(encoding="ascii").split()
            return int(raw[1]) * os.sysconf("SC_PAGE_SIZE")
        if sys.platform == "darwin":
            import ctypes
            # macOS SDK sys/resource.h rusage_info_v0, RUSAGE_INFO_V0=0.
            # Native libproc works for our own child even when /bin/ps is
            # unavailable in a managed sandbox; no subprocess listing occurs.
            class Usage(ctypes.Structure):
                _fields_ = [("uuid", ctypes.c_ubyte * 16)] + [
                    (name, ctypes.c_uint64) for name in (
                        "user_time", "system_time", "package_wakeups", "interrupt_wakeups",
                        "pageins", "wired_size", "resident_size", "physical_footprint",
                        "start_time", "exit_time",
                    )
                ]
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            function = library.proc_pid_rusage
            function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            function.restype = ctypes.c_int
            usage = Usage()
            if function(pid, 0, ctypes.byref(usage)) == 0:
                return int(usage.resident_size)
        return None
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _run_worker(executable: Path, request: bytes) -> bytes:
    process = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(
            [str(executable), "-I", "-B", str(Path(__file__).resolve()), "--geometry-worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd="/", env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, close_fds=True,
        )
        assert process.stdin is not None and process.stdout is not None
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "input")
        selector.register(process.stdout, selectors.EVENT_READ, "output")
        view = memoryview(request)
        position = 0
        output = bytearray()
        started = time.monotonic()
        next_memory_check = started
        while selector.get_map():
            now = time.monotonic()
            if now - started > WALL_SECONDS:
                raise PdfGeometryError("pdf_geometry_timeout")
            if now >= next_memory_check and process.poll() is None:
                rss = _rss_bytes(process.pid)
                if rss is None and process.poll() is None:
                    raise PdfGeometryError("pdf_geometry_resource_limit_unavailable")
                if rss is not None and rss > MAX_RSS_BYTES:
                    raise PdfGeometryError("pdf_geometry_memory_limit")
                next_memory_check = time.monotonic() + 0.1
            for key, _ in selector.select(timeout=min(0.1, max(0.0, WALL_SECONDS - (now - started)))):
                if key.data == "input":
                    try:
                        position += os.write(key.fd, view[position:position + 65536])
                    except BrokenPipeError:
                        position = len(view)
                    if position == len(view):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                else:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    else:
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT_BYTES:
                            raise PdfGeometryError("pdf_geometry_output_limit")
        remaining = WALL_SECONDS - (time.monotonic() - started)
        try:
            process.wait(timeout=max(0.001, remaining))
        except subprocess.TimeoutExpired:
            raise PdfGeometryError("pdf_geometry_timeout") from None
        if process.returncode != 0:
            raise PdfGeometryError("pdf_geometry_worker_failed")
        return bytes(output)
    except PdfGeometryError:
        raise
    except (OSError, ValueError):
        raise PdfGeometryError("pdf_geometry_runtime_unavailable") from None
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


def extract_geometry(data: bytes, page_indices: list[int], *, python_executable: Path) -> dict[str, Any]:
    """Extract requested pages only, using an explicitly supplied local runtime.

    The worker's whole-document parsing is bounded at 50 MiB, 20,000 pages,
    25 CPU seconds, 30 wall seconds and 1 GiB sampled RSS. At most 20 requested
    pages and 200,000 engine characters are returned. Memory is checked by the
    parent every 100 ms; this is not a kernel address-space sandbox. Zero-area
    boxes are omitted from characters and counted; their text remains in
    text_units (including CR/LF). Unmapped Unicode is omitted and counted.
    Original PDFium indexes never shift when a character has no usable box.
    Each record also has index_span (normally 1). A valid UTF-16 surrogate pair
    sharing exactly one box is emitted as one Unicode scalar with index_span=2.
    """
    data, page_indices = _checked_input(data, page_indices)
    if (not isinstance(python_executable, Path) or not python_executable.is_absolute()
            or not python_executable.is_file() or not os.access(python_executable, os.X_OK)):
        raise PdfGeometryError("pdf_geometry_runtime_unavailable")
    digest = hashlib.sha256(data).hexdigest()
    request = json.dumps({
        "pdf_base64": base64.b64encode(data).decode("ascii"),
        "source_sha256": digest, "page_indices": page_indices,
    }, separators=(",", ":")).encode("ascii")
    if len(request) > MAX_INPUT_BYTES:
        raise PdfGeometryError("invalid_pdf_geometry_input")
    raw = _run_worker(python_executable, request)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise PdfGeometryError("pdf_geometry_invalid_response") from None
    if isinstance(result, dict) and set(result) == {"error"}:
        code = result["error"]
        raise PdfGeometryError(code if isinstance(code, str) else "pdf_geometry_invalid_response")
    return _validate_result(result, digest, page_indices)


def _worker_limits() -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS + 1))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, OSError, ValueError):
        raise PdfGeometryError("pdf_geometry_resource_limit_unavailable") from None


def _native_geometry(data: bytes, indices: list[int]) -> dict[str, Any]:
    try:
        import pypdfium2 as pdfium
        from pypdf import PdfReader
    except ImportError:
        raise PdfGeometryError("pdf_geometry_engine_unavailable") from None
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise PdfGeometryError("pdf_geometry_encrypted_pdf")
        page_count = len(reader.pages)
        if not 1 <= page_count <= MAX_DOCUMENT_PAGES:
            raise PdfGeometryError("pdf_geometry_page_limit")
        if max(indices) >= page_count:
            raise PdfGeometryError("pdf_geometry_page_out_of_range")
        pages = []
        total = text_bytes = 0
        with pdfium.PdfDocument(data) as document:
            if len(document) != page_count:
                raise PdfGeometryError("pdf_geometry_input_mismatch")
            for index in indices:
                # pypdf resolves inherited page-tree boxes/rotation. PDFium's
                # get_*box helper has an inheritance limitation; see its docs.
                meta = reader.pages[index]
                media = [float(v) for v in meta.mediabox]
                crop = [float(v) for v in meta.cropbox]
                raw_rotation = meta.get("/Rotate", 0)
                rotation = int(raw_rotation)
                unit = float(meta.get("/UserUnit", 1))
                if (not _box(media) or not _box(crop) or float(raw_rotation) != rotation
                        or rotation % 90 or not _finite(unit) or not 0 < unit <= 75000):
                    raise PdfGeometryError("pdf_geometry_invalid_coordinates")
                chars = []
                units = []
                skipped = {"zero_area": 0, "unmapped_unicode": 0}
                page = document[index]
                textpage = None
                try:
                    textpage = page.get_textpage()
                    char_count = textpage.count_chars()
                    total += char_count
                    if char_count < 0 or total > MAX_ENGINE_CHARACTERS:
                        raise PdfGeometryError("pdf_geometry_character_limit")
                    ci = 0
                    while ci < char_count:
                        # get_text_range(ci, 1) can insert or omit characters
                        # and is UCS-2 limited. This raw API returns one scalar
                        # at the EXACT engine index also used by get_charbox.
                        scalar = int(pdfium.raw.FPDFText_GetUnicode(textpage, ci))
                        span = 1
                        if 0xD800 <= scalar <= 0xDBFF and ci + 1 < char_count:
                            low = int(pdfium.raw.FPDFText_GetUnicode(textpage, ci + 1))
                            if not 0xDC00 <= low <= 0xDFFF:
                                raise PdfGeometryError("pdf_geometry_invalid_unicode")
                            scalar = 0x10000 + ((scalar - 0xD800) << 10) + low - 0xDC00
                            span = 2
                        if 0xD800 <= scalar <= 0xDFFF:
                            raise PdfGeometryError("pdf_geometry_invalid_unicode")
                        if not 0 < scalar <= 0x10FFFF:
                            skipped["unmapped_unicode"] += 1
                            ci += 1
                            continue
                        text = chr(scalar)
                        text_bytes += len(text.encode("utf-8"))
                        if text_bytes > MAX_TEXT_BYTES:
                            raise PdfGeometryError("pdf_geometry_text_limit")
                        units.append({"index": ci, "index_span": span, "text": text})
                        bbox = [float(v) for v in textpage.get_charbox(ci, loose=False)]
                        if span == 2 and bbox != [float(v) for v in textpage.get_charbox(ci + 1, loose=False)]:
                            raise PdfGeometryError("pdf_geometry_invalid_unicode")
                        if not all(_finite(v) for v in bbox) or bbox[0] > bbox[2] or bbox[1] > bbox[3]:
                            raise PdfGeometryError("pdf_geometry_invalid_coordinates")
                        if bbox[0] == bbox[2] or bbox[1] == bbox[3]:
                            skipped["zero_area"] += span
                            ci += span
                            continue
                        chars.append({"index": ci, "index_span": span, "text": text, "bbox": bbox})
                        ci += span
                finally:
                    if textpage is not None:
                        textpage.close()
                    page.close()
                pages.append({
                    "page_index": index, "media_box": media, "crop_box": crop,
                    "rotation": rotation % 360, "user_unit": unit,
                    "coordinate_space": "pdf_user_space",
                    "coordinate_semantics": "unrotated_unscaled",
                    "index_semantics": "pdfium_character_index",
                    "engine_character_count": char_count,
                    "skipped_characters": skipped, "characters": chars, "text_units": units,
                })
        return {
            "engine": {"name": "pypdfium2", "version": str(pdfium.PYPDFIUM_INFO.version),
                       "pdfium_version": str(pdfium.PDFIUM_INFO.version)},
            "source_sha256": hashlib.sha256(data).hexdigest(), "page_count": page_count,
            "pages": pages,
            "limits": {"cpu_seconds": CPU_SECONDS, "wall_seconds": WALL_SECONDS,
                       "rss_bytes": MAX_RSS_BYTES, "memory_enforcement": "parent_rss_monitor"},
        }
    except PdfGeometryError:
        raise
    except MemoryError:
        raise PdfGeometryError("pdf_geometry_memory_limit") from None
    except Exception:
        raise PdfGeometryError("pdf_geometry_invalid_pdf") from None


def _worker_main() -> int:
    try:
        _worker_limits()
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise PdfGeometryError("invalid_pdf_geometry_input")
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {"pdf_base64", "source_sha256", "page_indices"}:
            raise PdfGeometryError("invalid_pdf_geometry_input")
        if (not isinstance(request["pdf_base64"], str)
                or not isinstance(request["source_sha256"], str)
                or not _HASH.fullmatch(request["source_sha256"])):
            raise PdfGeometryError("invalid_pdf_geometry_input")
        data = base64.b64decode(request["pdf_base64"], validate=True)
        data, indices = _checked_input(data, request["page_indices"])
        if hashlib.sha256(data).hexdigest() != request["source_sha256"]:
            raise PdfGeometryError("pdf_geometry_input_mismatch")
        del raw, request
        result = _native_geometry(data, indices)
        output = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(output) > MAX_OUTPUT_BYTES:
            raise PdfGeometryError("pdf_geometry_output_limit")
    except PdfGeometryError as error:
        output = json.dumps({"error": error.code}).encode("ascii")
    except (ValueError, TypeError, binascii.Error, UnicodeError, RecursionError):
        output = b'{"error":"invalid_pdf_geometry_input"}'
    except Exception:
        output = b'{"error":"pdf_geometry_worker_failed"}'
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--geometry-worker"]:
        raise SystemExit(_worker_main())
    raise SystemExit(2)
