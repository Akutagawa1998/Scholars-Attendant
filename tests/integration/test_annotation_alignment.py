"""Pure source-bound annotation alignment: invented text, no account or disk I/O."""
from copy import deepcopy
import hashlib
import json

import pytest

from research_assistant.annotation_alignment import align_annotations, AnnotationAlignmentError

PDF_HASH = hashlib.sha256(b"Synthetic original PDF identity").hexdigest()


def extraction(*texts):
    return {"source_sha256": PDF_HASH, "parser_name": "synthetic-test-parser", "parser_version": "1",
            "pages": [{"page_index": index, "text": value, "text_sha256": hashlib.sha256(value.encode()).hexdigest()}
                      for index, value in enumerate(texts)]}


def annotation(quote="Synthetic quote", index=0, kind="highlight", **overrides):
    return dict({"key": "SYNTH001", "annotationType": kind, "annotationText": quote,
        "annotationComment": "Synthetic reader comment", "annotationPageLabel": "xiv",
        "annotationPosition": json.dumps({"pageIndex": index, "rects": [[10, 20, 30, 40]]})}, **overrides)


def align(source, *annotations, **limits):
    return align_annotations(source, list(annotations), expected_pdf_sha256=PDF_HASH, **limits)


def test_exact_hashes_original_offsets_and_physical_page_separate_from_label():
    source = extraction("Before Synthetic quote after", "Other text")
    report = align(source, annotation())[0]
    assert report["page_index"] == 0 and report["page_number"] == 1
    assert report["provider_page_label"] == "xiv"
    assert report["raw_annotation_text"] == "Synthetic quote"
    assert report["raw_annotation_comment"] == "Synthetic reader comment"
    assert report["match_status"] == "exact" and report["candidate_count"] == 1
    match = report["matches"][0]
    assert match["literal_text"] == source["pages"][0]["text"][match["text_start"]:match["text_end"]] == "Synthetic quote"
    assert match["literal_sha256"] == hashlib.sha256(match["literal_text"].encode()).hexdigest()
    assert match["source_sha256"] == PDF_HASH
    assert match["page_text_sha256"] == source["pages"][0]["text_sha256"]
    assert not report["position_observation"]["geometric_verified"]


@pytest.mark.parametrize("page_text,quote,literal", [
    ("Before full\n  text after", "full text", "full\n  text"),
    ("Before full\r\n\ttext after", "full text", "full\r\n\ttext"),
    ("Before ＡＢＣ after", "ABC", "ＡＢＣ"),
    ("Before e\u0301 after", "é", "e\u0301"),
    ("Before é after", "e\u0301", "é"),
    ("Before \u1100\u1161\u11a8 after", "각", "\u1100\u1161\u11a8"),
    ("Before in\u00adside after", "inside", "in\u00adside"),
    ("Before in-\nside after", "inside", "in-\nside"),
    ("Before inside after", "in-\nside", "inside"),
    ("Before well-\nknown after", "well-known", "well-\nknown"),
    ("Before in\u2010\nside after", "inside", "in\u2010\nside"),
    ("Before \ufb03 after", "ffi", "\ufb03"),
    ("Before ㍿ after", "株式会社", "㍿"),
])
def test_normalized_match_retains_original_literal_slice(page_text, quote, literal):
    source = extraction(page_text)
    report = align(source, annotation(quote))[0]
    assert report["match_status"] == "normalized"
    assert report["raw_annotation_text"] == quote
    match, = report["matches"]
    assert match["literal_text"] == literal
    assert source["pages"][0]["text"][match["text_start"]:match["text_end"]] == literal
    assert match["literal_sha256"] == hashlib.sha256(literal.encode()).hexdigest()


@pytest.mark.parametrize("page_text,quote", [("\ufb03", "f"), ("\ufb03", "fi"), ("㍿", "会社"), ("abc", "ABC"), ("colour", "color")])
def test_partial_compatibility_glyphs_and_fuzzy_edits_do_not_create_quotes(page_text, quote):
    report = align(extraction(page_text), annotation(quote))[0]
    assert report["match_status"] == "unmatched" and not report["matches"]


@pytest.mark.parametrize("text,quote", [("foo foo", "foo"), ("foo bar and foo\nbar", "foo bar"),
    ("inside and in-\nside", "inside"), ("ABC and ＡＢＣ", "ABC"), ("aaa", "aa")])
def test_all_exact_and_normalized_candidates_remain_ambiguous(text, quote):
    report = align(extraction(text), annotation(quote))[0]
    assert report["match_status"] == "ambiguous" and report["candidate_count"] == 2
    assert len(report["matches"]) == 2 and "selected_match" not in report
    for match in report["matches"]:
        assert text[match["text_start"]:match["text_end"]] == match["literal_text"]


def test_candidate_cap_cannot_turn_repetition_into_unique_match():
    report = align(extraction("foo " * 100), annotation("foo"), max_candidates=1)[0]
    assert report["match_status"] == "ambiguous"
    assert report["candidate_count"] is None and report["candidate_count_lower_bound"] == 2
    assert report["candidates_truncated"] and len(report["matches"]) == 1
    assert "candidate_limit" in report["warnings"]


def test_never_searches_other_page_or_interprets_numeric_page_label():
    source = extraction("Wrong page.", "Synthetic quote")
    report = align(source, annotation(annotationPageLabel="2", annotationSortIndex="00001|000000|00000"))[0]
    assert report["page_index"] == 0 and report["page_number"] == 1 and report["provider_page_label"] == "2"
    assert report["match_status"] == "unmatched" and not report["matches"]


@pytest.mark.parametrize("position", [None, "not JSON", "[]", "{}", '{"pageIndex":true}', '{"pageIndex":0.0}',
    '{"pageIndex":"0"}', '{"pageIndex":-1}', '{"pageIndex":1}', '{"pageIndex":0,"pageIndex":0}',
    '{"pageIndex":0,"rects":[[1,2,3,NaN]]}'])
def test_invalid_position_never_falls_back_to_label_or_sort_index(position):
    report = align(extraction("Synthetic quote"), annotation(annotationPosition=position, annotationPageLabel="1"))[0]
    assert report["match_status"] == "invalid_position"
    assert report["page_index"] is None and report["page_number"] is None and not report["matches"]


@pytest.mark.parametrize("rects", [[[1, 2, 0, 4]], [[1, 2, 3]], [[True, 2, 3, 4]], [[0, 0, 2, float("inf")]],
    [[0, 0, 2, 10 ** 500]], "not rectangles", [[0, 0, 2, 3]] * 1001])
def test_geometry_validation_is_separate_from_textual_correspondence(rects):
    report = align(extraction("Synthetic quote"), annotation(annotationPosition={"pageIndex": 0, "rects": rects}))[0]
    assert report["match_status"] == "exact" and report["page_index"] == 0
    assert report["position_observation"]["rects_status"] == "invalid"
    assert not report["position_observation"]["rects"] and not report["position_observation"]["geometric_verified"]


@pytest.mark.parametrize("kind,canonical", [("note", "note"), ("text", "note"), ("image", "image"), ("ink", "ink")])
def test_comments_without_quotes_are_page_only(kind, canonical):
    report = align(extraction("Synthetic reader comment"), annotation("", kind=kind))[0]
    assert report["kind"] == canonical and report["match_status"] == "page_only"
    assert report["raw_annotation_comment"] == "Synthetic reader comment" and not report["matches"]


def test_unknown_types_spillover_ink_and_nonstring_labels_are_explicit():
    report = align(extraction("Synthetic quote", "rest"), annotation(kind="future-type", annotationPageLabel=5,
        annotationPosition={"pageIndex": 0, "nextPageRects": [[0, 0, 1, 1]], "paths": [[1, 2]], "rects": [[-5, -2, 1, 1]]}))[0]
    assert report["match_status"] == "exact" and report["kind"] == "unknown"
    assert report["provider_page_label"] is None
    assert {"unsupported_annotation_type", "cross_page_geometry_not_aligned", "ink_paths_not_aligned", "invalid_provider_page_label"} <= set(report["warnings"])
    assert report["position_observation"]["rects_status"] == "valid"


def test_hash_locks_reject_other_pdf_or_tampered_page():
    source = extraction("Synthetic quote")
    with pytest.raises(AnnotationAlignmentError, match="pdf_hash_mismatch"):
        align_annotations(source, [annotation()], expected_pdf_sha256="0" * 64)
    tampered = deepcopy(source)
    tampered["pages"][0]["text"] += " altered"
    with pytest.raises(AnnotationAlignmentError, match="page_text_hash_mismatch"):
        align(tampered, annotation())
    tampered = deepcopy(source)
    tampered["pages"][0]["page_index"] = False
    with pytest.raises(AnnotationAlignmentError, match="invalid_extraction_pages"):
        align(tampered, annotation())


def test_no_mutation_and_safe_resource_errors():
    source, data = extraction("Synthetic quote"), annotation()
    before = deepcopy((source, data))
    align(source, data)
    assert (source, data) == before
    with pytest.raises(AnnotationAlignmentError, match="annotation_alignment_limit"):
        align(source, annotation("x" * 100001))
    with pytest.raises(AnnotationAlignmentError, match="annotation_alignment_limit"):
        align(source, annotation(), max_candidates=True)


def test_long_combining_clusters_fail_bounded_without_normalizing_quadratically():
    source = extraction("a" + "\u0301" * 2000)
    with pytest.raises(AnnotationAlignmentError, match="annotation_normalization_limit"):
        align(source, annotation("á"))
