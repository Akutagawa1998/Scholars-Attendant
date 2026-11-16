"""Original-byte identity and literal PDF extraction; synthetic documents only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import stat

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from research_assistant.assets import ArtifactStore, AssetError, anchor_passage, extract_pdf


def pdf(*page_texts: str, password: str | None = None) -> bytes:
    """Author small test-only PDFs without external assets or private material."""
    writer = PdfWriter()
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        if text:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            content = DecodedStreamObject()
            literal = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content.set_data(f"BT /F1 12 Tf 72 720 Td ({literal}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(content)
    if password:
        writer.encrypt(password)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_blob_identity_deduplication_private_modes_and_verified_delete(tmp_path):
    data = pdf("Synthetic source text.")
    root = tmp_path / "private"
    root.mkdir(mode=0o755)
    with ArtifactStore(root) as store:
        first = store.put_bytes(data)
        second = store.put_bytes(data)
        assert first == second
        assert first == {"sha256": digest(data), "size": len(data), "relative_blob_key": f"sha256/{digest(data)[:2]}/{digest(data)}.pdf", "media_type": "application/pdf"}
        assert str(tmp_path) not in repr(store) + str(first)
        path = root / first["relative_blob_key"]
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert store.read_blob(first["relative_blob_key"], first["sha256"]) == data
        assert list(path.parent.iterdir()) == [path]
        assert store.delete_blob(first["relative_blob_key"], first["sha256"])
        assert not path.exists()
        assert not store.delete_blob(first["relative_blob_key"], first["sha256"])
        with pytest.raises(AssetError, match="blob_not_found"):
            store.read_blob(first["relative_blob_key"], first["sha256"])
    with pytest.raises(AssetError, match="store_closed"):
        store.put_bytes(data)


def test_concurrent_put_is_immutable_and_leaves_no_staging_files(tmp_path):
    data = pdf("Atomic blob publication.")
    with ArtifactStore(tmp_path / "private") as store:
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: store.put_bytes(data), range(12)))
        assert all(row == rows[0] for row in rows)
        assert store.read_blob(rows[0]["relative_blob_key"], rows[0]["sha256"]) == data
    assert len(list((tmp_path / "private").rglob("*.pdf"))) == 1
    assert not list((tmp_path / "private").rglob(".tmp-*"))


def test_blob_rejects_traversal_wrong_hash_and_corruption_without_overwrite(tmp_path):
    data = pdf("Immutable source.")
    with ArtifactStore(tmp_path / "private") as store:
        row = store.put_bytes(data)
        for key in ["../outside.pdf", "/private/file.pdf", row["relative_blob_key"].replace("sha256/", "sha256/../"), row["relative_blob_key"].replace(digest(data)[:2] + "/", "ff/")]:
            with pytest.raises(AssetError, match="invalid_blob_key"):
                store.read_blob(key, row["sha256"])
        with pytest.raises(AssetError, match="invalid_blob_key"):
            store.delete_blob(row["relative_blob_key"], "0" * 64)
        path = tmp_path / "private" / row["relative_blob_key"]
        path.write_bytes(b"corrupt synthetic bytes")
        for operation in [lambda: store.put_bytes(data), lambda: store.read_blob(row["relative_blob_key"], row["sha256"]), lambda: store.delete_blob(row["relative_blob_key"], row["sha256"])]:
            with pytest.raises(AssetError, match="blob_integrity_mismatch"):
                operation()
        assert path.read_bytes() == b"corrupt synthetic bytes"


def test_blob_rejects_symlink_at_root_directory_and_blob(tmp_path):
    root = tmp_path / "root"
    target = tmp_path / "external"
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(AssetError, match="invalid_store_root"):
        ArtifactStore(root)
    root.unlink()
    data = pdf("No traversal.")
    with ArtifactStore(root) as store:
        (root / "sha256").symlink_to(target, target_is_directory=True)
        with pytest.raises(AssetError, match="invalid_blob_path"):
            store.put_bytes(data)
        (root / "sha256").unlink()
        row = store.put_bytes(data)
        blob = root / row["relative_blob_key"]
        blob.unlink()
        external = target / "outside.pdf"
        external.write_bytes(data)
        blob.symlink_to(external)
        with pytest.raises(AssetError, match="invalid_blob_path"):
            store.read_blob(row["relative_blob_key"], row["sha256"])
        with pytest.raises(AssetError, match="invalid_blob_path"):
            store.delete_blob(row["relative_blob_key"], row["sha256"])
        assert external.read_bytes() == data


@pytest.mark.parametrize("data,options,code", [(b"not a pdf", {}, "invalid_pdf_header"), (b"%PDF-" + b"x" * 16, {"max_bytes": 10}, "pdf_too_large"), (b"%PDF-", {"media_type": "text/plain"}, "unsupported_asset_media_type")])
def test_blob_rejects_invalid_media_and_byte_limits(tmp_path, data, options, code):
    with ArtifactStore(tmp_path, max_bytes=options.pop("max_bytes", 1024)) as store:
        with pytest.raises(AssetError, match=code):
            store.put_bytes(data, **options)


def test_extracts_original_page_text_and_literal_anchors():
    data = pdf("Synthetic quote. Synthetic quote.", "Second synthetic page.")
    result = extract_pdf(data)
    assert result["source_sha256"] == digest(data)
    assert result["source_size"] == len(data)
    assert result["parser_name"] == "pypdf" and result["parser_version"]
    assert result["page_count"] == 2 and result["quality"] == "text_extracted"
    assert {"layout_and_semantic_support_not_verified", "ocr_not_performed"} <= set(result["warnings"])
    assert set(result["warnings"]) <= {"layout_and_semantic_support_not_verified", "ocr_not_performed", "os_resource_limits_unavailable"}
    assert [page["page_index"] for page in result["pages"]] == [0, 1]
    for page in result["pages"]:
        assert page["text_sha256"] == digest(page["text"].encode("utf-8"))
        assert page["text_sha256"] != result["source_sha256"]
    anchor = anchor_passage(result, 0, "Synthetic quote.", occurrence=1)
    loc = anchor["locator"]
    assert loc["text_start"] == len("Synthetic quote. ")
    assert result["pages"][0]["text"][loc["text_start"]:loc["text_end"]] == anchor["text"]
    assert loc["source_sha256"] == digest(data)
    assert loc["page_index"] == 0 and "page_label" not in loc and "bbox" not in loc
    assert loc["extraction_version"].startswith("pypdf:")
    assert loc["quote_hash"] == digest(anchor["text"].encode())
    with pytest.raises(AssetError, match="quote_not_found"):
        anchor_passage(result, 1, "This sentence was not present.")
    tampered = deepcopy(result)
    tampered["pages"][0]["text"] += " Altered"
    with pytest.raises(AssetError, match="invalid_pdf_anchor"):
        anchor_passage(tampered, 0, "Altered")


def test_blank_pdf_does_not_invent_content_and_partial_pages_are_explicit():
    blank = extract_pdf(pdf(""))
    assert blank["quality"] == "no_extractable_text"
    assert blank["pages"][0]["text"] == ""
    assert blank["pages"][0]["warnings"] == ["no_extractable_text"]
    partial = extract_pdf(pdf("Synthetic text", ""))
    assert partial["quality"] == "partial"
    assert partial["pages"][1]["warnings"] == ["no_extractable_text"]
    bounded = extract_pdf(pdf("Synthetic content stream"), max_page_stream_bytes=1)
    assert bounded["pages"][0]["warnings"] == ["page_content_stream_limit"]
    assert bounded["pages"][0]["text"] == ""


@pytest.mark.parametrize("make_pdf,kwargs,code", [(lambda: b"%PDF-unparseable PRIVATE", {}, "pdf_parse_failed"), (lambda: pdf(), {}, "empty_pdf"), (lambda: pdf("secret", password="password"), {}, "encrypted_pdf_unsupported"), (lambda: pdf("one", "two"), {"max_pages": 1}, "pdf_page_limit"), (lambda: pdf("Too much synthetic text"), {"max_text_chars": 2}, "pdf_text_limit"), (lambda: pdf("deadline"), {"timeout_seconds": 0.000001}, "pdf_parse_timeout")])
def test_pdf_failures_and_resource_bounds_are_safe(make_pdf, kwargs, code):
    with pytest.raises(AssetError) as caught:
        extract_pdf(make_pdf(), **kwargs)
    assert str(caught.value) == code
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("kwargs", [{"max_pages": True}, {"max_text_chars": -1}, {"max_page_stream_bytes": 0}, {"timeout_seconds": float("nan")}, {"memory_limit_mb": 1}])
def test_invalid_resource_limits_fail_before_worker(kwargs):
    with pytest.raises(AssetError, match="invalid_extraction_limit"):
        extract_pdf(pdf("Synthetic"), **kwargs)
