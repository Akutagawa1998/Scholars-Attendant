"""Geometry uses synthetic in-memory PDFs only, never a user's library.

An explicitly installed local runtime is optional. No test installs a parser or
downloads a PDF. Set RA_TEST_GEOMETRY_PYTHON to a preinstalled compatible Python
to use a separate environment. The default is the current Python executable.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject, DictionaryObject, NameObject, NumberObject, RectangleObject,
)
import pytest

from research_assistant import pdf_geometry as geometry
from research_assistant.pdf_geometry import PdfGeometryError, extract_geometry

@pytest.fixture(scope="module")
def runtime():
    executable = Path(os.environ.get("RA_TEST_GEOMETRY_PYTHON", sys.executable))
    if not executable.is_absolute() or not executable.is_file():
        pytest.skip("optional explicitly installed PDFium runtime is absent")
    probe = subprocess.run(
        [str(executable), "-I", "-B", "-c", "import pypdfium2, pypdf"],
        capture_output=True, timeout=10, env={"PATH": "/usr/bin:/bin"},
    )
    if probe.returncode:
        pytest.skip("optional local PDFium runtime has no compatible parser")
    return executable


def make_pdf(*, pages=((0, 1),), content=b"BT /F1 12 Tf 50 250 Td (A) Tj ET", cmap=None,
             encrypted=False, inherited=False):
    writer = PdfWriter()
    for rotation, unit in pages:
        page = writer.add_blank_page(200, 300)
        page.mediabox = RectangleObject([10, 20, 210, 320])
        page.cropbox = RectangleObject([20, 30, 200, 300])
        page[NameObject("/Rotate")] = NumberObject(rotation)
        page[NameObject("/UserUnit")] = NumberObject(unit)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        if cmap is not None:
            stream = DecodedStreamObject()
            stream.set_data(cmap)
            font[NameObject("/ToUnicode")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        })
        stream = DecodedStreamObject()
        stream.set_data(content)
        page[NameObject("/Contents")] = writer._add_object(stream)
    if inherited:
        parent = writer._pages.get_object()
        for key in ("/MediaBox", "/CropBox", "/Rotate"):
            parent[NameObject(key)] = writer.pages[0][key]
            for page in writer.pages:
                del page[NameObject(key)]
    if encrypted:
        writer.encrypt("synthetic-only-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture(scope="module")
def basic(runtime):
    data = make_pdf()
    return data, extract_geometry(data, [0], python_executable=runtime)


def test_engine_hash_and_declared_coordinate_contract(basic):
    data, result = basic
    assert result["source_sha256"] == hashlib.sha256(data).hexdigest()
    assert result["engine"]["name"] == "pypdfium2"
    assert result["engine"]["version"] and result["engine"]["pdfium_version"]
    assert result["limits"]["memory_enforcement"] == "parent_rss_monitor"
    page = result["pages"][0]
    assert page["coordinate_space"] == "pdf_user_space"
    assert page["coordinate_semantics"] == "unrotated_unscaled"
    assert page["index_semantics"] == "pdfium_character_index"
    assert page["media_box"] == [10, 20, 210, 320]
    assert page["crop_box"] == [20, 30, 200, 300]
    assert page["characters"][0]["bbox"] == pytest.approx([50, 250, 58.016, 258.592], abs=0.001)
    assert page["characters"][0]["text"] == "A"
    assert page["characters"][0]["index_span"] == 1


def test_native_boxes_do_not_rebase_crop_rotate_or_scale_userunit(runtime):
    variants = [(rotation, unit) for rotation in (0, 90, 180, 270) for unit in (1, 2)]
    result = extract_geometry(make_pdf(pages=variants), list(range(8)), python_executable=runtime)
    box = result["pages"][0]["characters"][0]["bbox"]
    for page, (rotation, unit) in zip(result["pages"], variants):
        assert page["rotation"] == rotation
        assert page["user_unit"] == unit
        assert page["media_box"] == [10, 20, 210, 320]
        assert page["crop_box"] == [20, 30, 200, 300]
        assert page["characters"][0]["bbox"] == pytest.approx(box)


def test_page_tree_metadata_inheritance_is_resolved_without_changing_geometry(runtime):
    result = extract_geometry(make_pdf(pages=((90, 1),), inherited=True), [0], python_executable=runtime)
    page = result["pages"][0]
    assert (page["media_box"], page["crop_box"], page["rotation"]) == (
        [10, 20, 210, 320], [20, 30, 200, 300], 90,
    )
    assert page["characters"][0]["bbox"][0:2] == [50, 250]


def test_requested_page_order_and_omitted_zero_area_indexes_are_preserved(runtime):
    content = b"BT /F1 12 Tf 50 250 Td (A B) Tj 0 -20 Td (C) Tj ET"
    result = extract_geometry(make_pdf(pages=((0, 1), (90, 1)), content=content), [1, 0],
                              python_executable=runtime)
    assert [page["page_index"] for page in result["pages"]] == [1, 0]
    for page in result["pages"]:
        assert page["engine_character_count"] == 6
        assert page["skipped_characters"] == {"zero_area": 2, "unmapped_unicode": 0}
        assert sum(c["index_span"] for c in page["characters"]) == 4
        assert len(page["text_units"]) == 6
        assert "\r\n" in "".join(unit["text"] for unit in page["text_units"])
        text_by_index = {unit["index"]: unit for unit in page["text_units"]}
        for char in page["characters"]:
            assert text_by_index[char["index"]] == {
                key: value for key, value in char.items() if key != "bbox"
            }
        pairs = {c["text"]: c["bbox"] for c in page["characters"]}
        assert pairs["A"][0:2] == [50, 250]
        assert pairs["C"][0:2] == pytest.approx([50.6, 229.868], abs=0.001)
        assert all(c["bbox"][0] < c["bbox"][2] and c["bbox"][1] < c["bbox"][3]
                   for c in page["characters"])
    # Rotation may change engine reading order. The pairings remain correct.
    assert [c["index"] for c in result["pages"][1]["characters"]] == [0, 1, 2, 5]


CMAP = b"""/CIDInit /ProcSet findresource begin 12 dict begin begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Synthetic def /CMapType 2 def
1 begincodespacerange <00> <FF> endcodespacerange
2 beginbfchar <41> <D83DDE00> <42> <FB01> endbfchar
endcmap CMapName currentdict /CMap defineresource pop end end"""


def test_non_bmp_pair_and_ligature_do_not_shift_engine_indexes(runtime):
    data = make_pdf(content=b"BT /F1 12 Tf 50 250 Td (AB) Tj ET", cmap=CMAP)
    page = extract_geometry(data, [0], python_executable=runtime)["pages"][0]
    assert page["engine_character_count"] == 4
    assert [(c["index"], c["index_span"], c["text"]) for c in page["characters"]] == [
        (0, 2, "\U0001f600"), (2, 1, "f"), (3, 1, "i"),
    ]
    assert page["characters"][1]["bbox"] == page["characters"][2]["bbox"]
    assert page["skipped_characters"] == {"zero_area": 0, "unmapped_unicode": 0}
    assert "".join(unit["text"] for unit in page["text_units"]) == "\U0001f600fi"


@pytest.mark.parametrize("data,indices", [
    (b"not pdf", [0]), (bytearray(b"%PDF-1.7"), [0]), (b"%PDF-1.7", []),
    (b"%PDF-1.7", [True]), (b"%PDF-1.7", [-1]), (b"%PDF-1.7", [0, 0]),
    (b"%PDF-1.7", list(range(21))), (b"%PDF-1.7", [20000]),
])
def test_input_rejected_before_starting_a_worker(monkeypatch, data, indices):
    monkeypatch.setattr(geometry, "_run_worker", lambda *_: pytest.fail("must not start worker"))
    with pytest.raises(PdfGeometryError, match="^invalid_pdf_geometry_input$"):
        extract_geometry(data, indices, python_executable=Path(sys.executable))


def test_pdf_size_limit_is_checked_before_ipc(monkeypatch):
    monkeypatch.setattr(geometry, "MAX_PDF_BYTES", 8)
    with pytest.raises(PdfGeometryError, match="^pdf_geometry_pdf_too_large$"):
        extract_geometry(b"%PDF-1.7x", [0], python_executable=Path(sys.executable))


def test_runtime_must_be_an_explicit_existing_absolute_path(basic):
    for path in (Path("python3"), Path("/no-such-geometry-python"), "python3"):
        with pytest.raises(PdfGeometryError, match="^pdf_geometry_runtime_unavailable$"):
            extract_geometry(basic[0], [0], python_executable=path)


@pytest.mark.parametrize("kind,code", [
    ("corrupt", "pdf_geometry_invalid_pdf"),
    ("encrypted", "pdf_geometry_encrypted_pdf"),
    ("page", "pdf_geometry_page_out_of_range"),
    ("characters", "pdf_geometry_character_limit"),
])
def test_worker_fixed_errors_expose_no_parser_text_or_paths(runtime, kind, code):
    data = make_pdf(encrypted=kind == "encrypted")
    indices = [1] if kind == "page" else [0]
    if kind == "corrupt":
        data = b"%PDF-1.7\nSYNTHETIC PRIVATE TITLE and bad xref"
    if kind == "characters":
        data = make_pdf(content=b"BT /F1 0.1 Tf 50 250 Td " + (b"(" + b"A" * 30000 + b") Tj 0 -10 Td ") * 7 + b"ET")
    with pytest.raises(PdfGeometryError) as caught:
        extract_geometry(data, indices, python_executable=runtime)
    assert caught.value.code == code
    assert str(caught.value) == code
    assert caught.value.__cause__ is None


def test_worker_rejects_ipc_hash_mismatch(runtime):
    raw = geometry._run_worker(runtime, json.dumps({
        "pdf_base64": base64.b64encode(make_pdf()).decode("ascii"),
        "source_sha256": "0" * 64, "page_indices": [0],
    }).encode())
    assert json.loads(raw) == {"error": "pdf_geometry_input_mismatch"}


@pytest.mark.parametrize("mutation", ["hash", "unicode", "overlap", "box", "page", "count", "unknown", "text_units"])
def test_parent_validates_untrusted_result_structure(basic, mutation):
    data, valid = basic
    result = copy.deepcopy(valid)
    if mutation == "hash": result["source_sha256"] = "0" * 64
    if mutation == "unicode": result["pages"][0]["characters"][0]["text"] = "AB"
    if mutation == "overlap": result["pages"][0]["characters"].append(copy.deepcopy(result["pages"][0]["characters"][0]))
    if mutation == "box": result["pages"][0]["characters"][0]["bbox"][0] = float("nan")
    if mutation == "page": result["pages"][0]["page_index"] = 1
    if mutation == "count": result["page_count"] = True
    if mutation == "unknown": result["source_path"] = "synthetic-only-forbidden-path"
    if mutation == "text_units": result["pages"][0]["text_units"][0]["text"] = "B"
    with pytest.raises(PdfGeometryError, match="^pdf_geometry_invalid_response$"):
        geometry._validate_result(result, hashlib.sha256(data).hexdigest(), [0])


@pytest.mark.parametrize("mode,code", [
    ("unavailable", "pdf_geometry_resource_limit_unavailable"),
    ("oversized", "pdf_geometry_memory_limit"),
    ("wall", "pdf_geometry_timeout"),
    ("output", "pdf_geometry_output_limit"),
])
def test_parent_enforces_resource_limits_and_terminates_worker(runtime, monkeypatch, mode, code):
    if mode == "unavailable": monkeypatch.setattr(geometry, "_rss_bytes", lambda _: None)
    if mode == "oversized": monkeypatch.setattr(geometry, "_rss_bytes", lambda _: geometry.MAX_RSS_BYTES + 1)
    if mode == "wall": monkeypatch.setattr(geometry, "WALL_SECONDS", 0.000001)
    if mode == "output": monkeypatch.setattr(geometry, "MAX_OUTPUT_BYTES", 16)
    with pytest.raises(PdfGeometryError, match=f"^{code}$"):
        extract_geometry(make_pdf(), [0], python_executable=runtime)


def test_driver_error_and_invalid_json_do_not_forward_private_output(basic, monkeypatch):
    monkeypatch.setattr(geometry, "_run_worker", lambda *_: b"PRIVATE parser traceback /some/path")
    with pytest.raises(PdfGeometryError, match="^pdf_geometry_invalid_response$"):
        extract_geometry(basic[0], [0], python_executable=Path(sys.executable))
    monkeypatch.setattr(geometry, "_run_worker", lambda *_: b'{"error":"PRIVATE unknown error"}')
    with pytest.raises(PdfGeometryError, match="^pdf_geometry_worker_failed$"):
        extract_geometry(basic[0], [0], python_executable=Path(sys.executable))
