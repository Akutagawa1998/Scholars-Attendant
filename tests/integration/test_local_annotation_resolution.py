"""Pure synthetic reviews: no library, provider, filesystem assets or DB writes."""
from copy import deepcopy
import hashlib

import pytest

from research_assistant.annotation_alignment import align_annotations
from research_assistant.annotation_review import AnnotationReviewService
from research_assistant.local_annotation_resolution import (
    ALGORITHM_VERSION, NORMALIZATION_VERSION, LocalResolutionError, resolve_local_annotation,
)


PDF_HASH = hashlib.sha256(b"synthetic local-resolution PDF").hexdigest()


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def capture(text, quote, rects=None):
    page = dict(page_index=0, text=text, text_sha256=sha(text))
    annotation = dict(key="SYNTH001", itemType="annotation", annotationType="highlight",
        annotationText=quote, annotationPageLabel="17",
        annotationPosition=dict(pageIndex=0, rects=rects if rects is not None else [[0, 99, 1000, 102]]))
    extraction = dict(source_sha256=PDF_HASH, parser_name="pypdf", parser_version="6.21.1", pages=[page])
    report = align_annotations(extraction, [annotation], expected_pdf_sha256=PDF_HASH)[0]
    for candidate in report["matches"]:
        candidate.pop("literal_text", None)
    return dict(alignment_id="synthetic-alignment", annotation_snapshot_id="synthetic-annotation",
        pdf_source_id="synthetic-pdf", attachment_link_id="synthetic-link", raw_annotation=annotation,
        v1_report=report, canonical_page=page, pdf_sha256=PDF_HASH,
        canonical_parser=dict(name="pypdf", version="6.21.1"), input_token=sha("synthetic capture dependencies"))


def geometry(text, *, row_break="\n"):
    characters, text_units, native_index, x, y = [], [], 0, 0, 100
    for value in text:
        width = 2 if ord(value) > 0xFFFF else 1
        unit = dict(index=native_index, index_span=width, text=value)
        text_units.append(unit)
        if value not in "\r\n":
            characters.append(dict(**unit, bbox=[float(x), float(y), float(x + 1), float(y + 1)]))
        if value == row_break:
            x, y = 0, y + 100
        elif value != "\r":
            x += 1
        native_index += width
    return dict(source_sha256=PDF_HASH,
        engine=dict(name="pypdfium2", version="5.14.0", pdfium_version="156.0.8076.0"),
        pages=[dict(page_index=0, media_box=[0.0, 0.0, 1000.0, 1000.0], crop_box=[0.0, 0.0, 1000.0, 1000.0],
            rotation=0, user_unit=1.0, characters=characters, text_units=text_units,
            engine_character_count=native_index,
            skipped_characters=dict(zero_area=len(text_units) - len(characters), unmapped_unicode=0))])


def resolve(captured, engine):
    before = deepcopy((captured, engine))
    result = resolve_local_annotation(captured, engine)
    assert (captured, engine) == before
    # Validate each ordinary result against the actual durable service schema.
    checked = AnnotationReviewService._validate_report(result, captured)
    assert checked["status"] == result["status"]
    assert result["parameters"]["normalization_version"] == NORMALIZATION_VERSION
    assert "literal_text" not in str(result)
    assert "path" not in result
    return result


def repeated():
    quote = "repeated quoted phrase"
    line1 = "Alpha context is uniquely different before " + quote + " and alpha ending context."
    line2 = "Beta context is independently unique before " + quote + " and beta ending context."
    text = line1 + "\n" + line2
    start = line2.index(quote)
    captured = capture(text, quote, [[float(start), 199.0, float(start + len(quote)), 202.0]])
    assert captured["v1_report"]["match_status"] == "ambiguous"
    return captured, geometry(text)


def test_repeated_phrase_uses_unique_context_and_geometry_selecting_original_literal():
    captured, engine = repeated()
    result = resolve(captured, engine)
    assert ALGORITHM_VERSION == "zotero-local-review-v2"
    assert result["status"] == "resolved_by_geometry"
    assert result["geometry"]["candidate_index"] == 1
    assert result["geometry"]["overlap"] == 1.0 and result["geometry"]["runner_up_overlap"] == 0.0
    original = captured["v1_report"]["matches"][1]
    assert result["selected_spans"] == [{key: original[key] for key in ("text_start", "text_end", "literal_sha256")}]
    assert result["parameters"]["engine_version"] == "5.14.0+pdfium.156.0.8076.0"


@pytest.mark.parametrize("rects", [[[0.0, 1.0, 1000.0, 2.0]], [[0.0, 90.0, 1000.0, 210.0]]])
def test_wrong_rect_or_rect_covering_both_quotes_does_not_resolve(rects):
    captured, engine = repeated()
    captured["raw_annotation"]["annotationPosition"]["rects"] = rects
    result = resolve(captured, engine)
    assert result["status"] == "unresolved" and result["selected_spans"] == []


@pytest.mark.parametrize("literal,quote", [("word. ”", "word.”"), ("( 17)", "(17)"), ("（ １７）", "(17)")])
def test_only_defined_punctuation_rules_create_original_literal_text_results(literal, quote):
    captured = capture("Unique preceding context " + literal + " unique following context.", quote)
    assert captured["v1_report"]["match_status"] == "unmatched"
    result = resolve(captured, {})
    assert result["status"] == "resolved_by_local_text" and result["basis"] == "canonical_literal"
    selected, = result["selected_spans"]
    assert captured["canonical_page"]["text"][selected["text_start"]:selected["text_end"]] == literal
    assert selected["literal_sha256"] == sha(literal)
    assert "geometry" not in result


def test_punctuation_equality_can_map_to_independent_engine_with_geometry():
    canonical = "Unique preceding context with content word. ” unique following context."
    engine_text = canonical.replace(". ”", ".”")
    captured = capture(canonical, "word.”")
    result = resolve(captured, geometry(engine_text))
    assert result["status"] == "resolved_by_local_text" and result["basis"] == "geometry_and_text"
    assert result["geometry"]["overlap"] == 1.0 and "candidate_index" not in result["geometry"]


def test_punctuation_local_text_can_remain_independent_when_parser_context_differs():
    canonical = "Canonical reading order before word. ” canonical reading order afterward."
    engine_text = "Different independent order before word.” different independent order afterward."
    captured = capture(canonical, "word.”")
    result = resolve(captured, geometry(engine_text))
    assert result["status"] == "resolved_by_local_text" and result["basis"] == "canonical_literal"
    assert "geometry" not in result and result["parameters"]["engine"] == "pypdf"


@pytest.mark.parametrize("literal,quote", [("A BC", "AB C"), ("foo bar", "foobar"), ("foo ,", "foo,"), ("( word", "(word")])
def test_compact_whitespace_word_boundary_or_unsupported_punctuation_never_resolves(literal, quote):
    captured = capture("Unique preceding context " + literal + " unique following context.", quote)
    result = resolve(captured, {})
    assert result["status"] == "unresolved" and result["selected_spans"] == []


def test_independent_engine_can_locate_word_boundary_difference_but_cannot_select_compact_span():
    captured = capture("Unique preceding context A BC unique following context.", "AB C")
    result = resolve(captured, geometry("Unique preceding context AB C unique following context."))
    assert result["status"] == "located_with_parser_difference"
    assert result["selected_spans"] == [] and result["geometry"]["overlap"] == 1.0


def test_repeated_punctuation_candidates_remain_unresolved_without_geometry():
    captured = capture("word. ” and word. ”", "word.”")
    result = resolve(captured, {})
    assert result["status"] == "unresolved" and not result["selected_spans"]


@pytest.mark.parametrize("field,value", [("rotation", 90), ("rotation", True), ("user_unit", 2.0),
    ("media_box", [10.0, 0.0, 1000.0, 1000.0]), ("crop_box", [1.0, 0.0, 900.0, 900.0])])
def test_unsupported_coordinates_remain_explicitly_unresolved(field, value):
    captured, engine = repeated()
    engine["pages"][0][field] = value
    result = resolve(captured, engine)
    assert result["status"] == "unresolved" and not result["selected_spans"]


@pytest.mark.parametrize("target", ["source", "page", "candidate", "engine", "token"])
def test_locked_source_page_and_candidate_hashes_reject_mismatch(target):
    captured, engine = repeated()
    if target == "source":
        captured["pdf_sha256"] = "0" * 64
    elif target == "page":
        captured["canonical_page"]["text"] += "changed"
    elif target == "candidate":
        captured["v1_report"]["matches"][0]["literal_sha256"] = "0" * 64
    elif target == "engine":
        engine["source_sha256"] = "0" * 64
    else:
        captured["input_token"] = "not-a-hash"
    with pytest.raises(LocalResolutionError, match="local_resolution_"):
        resolve_local_annotation(captured, engine)


def test_native_pdfium_version_participates_in_durable_proof_parameters():
    captured, engine = repeated()
    first = resolve(captured, engine)
    engine["engine"]["pdfium_version"] = "157.0.0"
    second = resolve(captured, engine)
    assert first["parameters"]["engine_version"] != second["parameters"]["engine_version"]
    del engine["engine"]["pdfium_version"]
    assert resolve(captured, engine)["status"] == "unresolved"


def test_unmapped_native_unit_cannot_be_deleted_to_form_an_exact_quote():
    captured, engine = repeated()
    page = engine["pages"][0]
    page["skipped_characters"]["unmapped_unicode"] = 1
    page["engine_character_count"] += 1
    for unit in page["text_units"] + page["characters"]:
        if unit["index"] >= 10:
            unit["index"] += 1
    assert resolve(captured, engine)["status"] == "unresolved"


@pytest.mark.parametrize("mutation", ["gap", "count", "missing_text_units"])
def test_native_text_units_must_cover_whole_page_without_invented_gap_spaces(mutation):
    captured, engine = repeated()
    page = engine["pages"][0]
    if mutation == "gap":
        for unit in page["text_units"] + page["characters"]:
            if unit["index"] >= 10:
                unit["index"] += 1
    elif mutation == "count":
        page["engine_character_count"] += 1
    else:
        del page["text_units"]
        assert resolve(captured, engine)["status"] == "unresolved"
        return
    with pytest.raises(LocalResolutionError, match="invalid_geometry"):
        resolve_local_annotation(captured, engine)


def test_crlf_text_units_preserve_word_breaks_and_non_bmp_native_spans():
    text = "Unique 😀 preceding context before quoted\r\nphrase and following context."
    captured = capture(text, "quoted phrase", [[0.0, 99.0, 1000.0, 202.0]])
    engine = geometry(text)
    result = resolve(captured, engine)
    assert result["status"] == "resolved_by_local_text"
    assert result["basis"] == "geometry_and_text"
    assert any(unit["index_span"] == 2 for unit in engine["pages"][0]["text_units"])


def test_contexts_with_different_word_boundaries_cannot_map_via_compact_text():
    quote = "identical quote"
    a = "A BC " * 14
    b = "AB C " * 14
    canonical = a + quote + a + "\n" + b + quote + b
    # Different native reading order and different word boundaries invalidate
    # both canonical-to-engine identities, even though deleting spaces agrees.
    engine_text = b + quote + b + "\n" + a + quote + a
    start = len(b)
    captured = capture(canonical, quote, [[float(start), 99.0, float(start + len(quote)), 102.0]])
    result = resolve(captured, geometry(engine_text))
    # Exact contexts are allowed to map across reading order; selected identity
    # must be canonical second occurrence rather than positional first.
    assert result["status"] == "resolved_by_geometry" and result["geometry"]["candidate_index"] == 1


def test_short_repeated_context_cannot_disambiguate_by_occurrence_order():
    captured = capture("quote\nquote", "quote", [[0.0, 199.0, 5.0, 202.0]])
    result = resolve(captured, geometry("quote\nquote"))
    assert result["status"] == "located_with_parser_difference" and not result["selected_spans"]


@pytest.mark.parametrize("field,value", [("nextPageRects", [[0, 0, 2, 3]]), ("paths", [[1, 2]])])
def test_cross_page_and_ink_geometry_do_not_upgrade_current_page_match(field, value):
    captured, engine = repeated()
    captured["raw_annotation"]["annotationPosition"][field] = value
    assert resolve(captured, engine)["status"] == "unresolved"


@pytest.mark.parametrize("value,width", [("\ud800", 1), ("x", 2), ("😀", 1)])
def test_native_units_require_valid_scalars_and_utf16_span(value, width):
    captured, engine = repeated()
    engine["pages"][0]["text_units"][0].update(text=value, index_span=width)
    with pytest.raises(LocalResolutionError, match="invalid_geometry"):
        resolve_local_annotation(captured, engine)
