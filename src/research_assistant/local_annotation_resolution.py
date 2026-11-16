"""Pure local annotation review; immutable v1 evidence remains unchanged.

Reports contain hashes/positions only. Geometry is never semantic support or
user-reading evidence. Local text correspondence is algorithmic evidence;
operator approval is separate. The caller owns trusted captures, worker
provenance, authorization and durable recording.
"""
from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import hashlib
import math
import re
import unicodedata

from .annotation_alignment import _normalize, _Normalized, _occurrences, _candidates, _position
from .assets import AssetError

ALGORITHM_VERSION = "zotero-local-review-v2"
NORMALIZATION_VERSION = "punct-boundary-v2"
OVERLAP_THRESHOLD = 0.85
MARGIN_THRESHOLD = 0.5
TOLERANCE = 0.5
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,95}\Z")
_MAX_TEXT = 5_000_000
_MAX_CANDIDATES = 128


class LocalResolutionError(AssetError):
    """A stable, content-free failure code."""


def _hash(text):
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except (AttributeError, UnicodeError):
        raise LocalResolutionError("local_resolution_invalid_text") from None


def _sha(value):
    return isinstance(value, str) and bool(_HASH.fullmatch(value))


def _token(value):
    return isinstance(value, str) and bool(_TOKEN.fullmatch(value))


def _finite(value):
    return type(value) in {float, int} and abs(value) <= 1_000_000 and math.isfinite(value)


def _scalar(value, width):
    return (isinstance(value, str) and len(value) == 1 and not 0xD800 <= ord(value) <= 0xDFFF
            and width == (2 if ord(value) > 0xFFFF else 1))


def _box(value):
    return (isinstance(value, list) and len(value) == 4 and all(_finite(x) for x in value)
            and value[0] < value[2] and value[1] < value[3])


def _base(capture):
    parser = capture.get("canonical_parser", {})
    version = parser.get("version") if isinstance(parser, dict) else None
    params = {"normalization_version": NORMALIZATION_VERSION, "coordinate_system": "canonical_text"}
    if _token(version):
        params.update(engine="pypdf", engine_version=version)
    return {"status": "unresolved", "method": "none", "basis": "no_unique_match",
            "parameters": params, "selected_spans": []}


def _span(page, start, end):
    return {"text_start": start, "text_end": end, "literal_sha256": _hash(page["text"][start:end])}


def _validate_capture(capture):
    if (not isinstance(capture, dict) or not _sha(capture.get("pdf_sha256"))
            or not _sha(capture.get("input_token")) or not isinstance(capture.get("raw_annotation"), dict)
            or not isinstance(capture.get("v1_report"), dict)):
        raise LocalResolutionError("local_resolution_invalid_capture")
    report, page = capture["v1_report"], capture.get("canonical_page")
    if report.get("source_sha256") != capture["pdf_sha256"]:
        raise LocalResolutionError("local_resolution_hash_mismatch")
    parser = capture.get("canonical_parser")
    if (not isinstance(parser, dict) or parser.get("name") != "pypdf" or not _token(parser.get("version"))):
        raise LocalResolutionError("local_resolution_invalid_parser")
    if page is None:
        return None
    if (not isinstance(page, dict) or type(page.get("page_index")) is not int or not 0 <= page["page_index"] < 1000
            or not isinstance(page.get("text"), str) or len(page["text"]) > _MAX_TEXT):
        raise LocalResolutionError("local_resolution_invalid_page")
    if _hash(page["text"]) != page.get("text_sha256") or report.get("page_text_sha256") != page["text_sha256"]:
        raise LocalResolutionError("local_resolution_hash_mismatch")
    index, _, _ = _position(capture["raw_annotation"].get("annotationPosition"), page["page_index"] + 1)
    if index != page["page_index"] or type(report.get("page_index")) is not int or report["page_index"] != index:
        raise LocalResolutionError("local_resolution_page_mismatch")
    quote = capture["raw_annotation"].get("annotationText", "")
    if not isinstance(quote, str) or len(quote) > 100000:
        raise LocalResolutionError("local_resolution_invalid_text")
    candidates = report.get("matches", [])
    if not isinstance(candidates, list) or len(candidates) > _MAX_CANDIDATES:
        raise LocalResolutionError("local_resolution_invalid_candidates")
    for candidate in candidates:
        if (not isinstance(candidate, dict) or type(candidate.get("text_start")) is not int
                or type(candidate.get("text_end")) is not int
                or not 0 <= candidate["text_start"] < candidate["text_end"] <= len(page["text"])):
            raise LocalResolutionError("local_resolution_invalid_candidates")
        span = _span(page, candidate["text_start"], candidate["text_end"])
        if (span["literal_sha256"] != candidate.get("literal_sha256")
                or candidate.get("source_sha256", capture["pdf_sha256"]) != capture["pdf_sha256"]
                or candidate.get("page_text_sha256", page["text_sha256"]) != page["text_sha256"]):
            raise LocalResolutionError("local_resolution_hash_mismatch")
        if "literal_text" in candidate and candidate["literal_text"] != page["text"][span["text_start"]:span["text_end"]]:
            raise LocalResolutionError("local_resolution_hash_mismatch")
    return page


def _punct_view(value, *, dehyphenate):
    """Only remove space at Po→Pf or Ps→Nd boundaries after frozen v1 rules."""
    source = _normalize(value, dehyphenate=dehyphenate)
    chars, starts, ends = [], array("I"), array("I")
    for index, character in enumerate(source.text):
        remove = False
        if character.isspace() and 0 < index < len(source.text) - 1:
            left, right = unicodedata.category(source.text[index - 1]), unicodedata.category(source.text[index + 1])
            remove = (left, right) in {("Po", "Pf"), ("Ps", "Nd")}
        if not remove:
            chars.append(character); starts.append(source.starts[index]); ends.append(source.ends[index])
    return _Normalized("".join(chars), starts, ends)


def _text_candidates(value, quote):
    found = set()
    for dehyphenate in (False, True):
        page, target = _punct_view(value, dehyphenate=dehyphenate), _punct_view(quote, dehyphenate=dehyphenate)
        if not target.text:
            continue
        for left, right in _occurrences(page.text, target.text):
            span = page.starts[left], page.ends[right - 1]
            literal = _punct_view(value[span[0]:span[1]], dehyphenate=dehyphenate)
            if literal.text == target.text:
                found.add(span)
                if len(found) > _MAX_CANDIDATES:
                    return [], True
    return sorted(found), False


@dataclass
class _EnginePage:
    text: str
    units: list
    metadata: dict
    engine: dict


def _engine_page(geometry, capture, page_index):
    if not geometry:
        return None, True
    if not isinstance(geometry, dict):
        raise LocalResolutionError("local_resolution_invalid_geometry")
    hashes = [geometry[key] for key in ("source_sha256", "pdf_sha256") if key in geometry]
    if not hashes or any(value != capture["pdf_sha256"] for value in hashes):
        raise LocalResolutionError("local_resolution_hash_mismatch")
    engine, pages = geometry.get("engine"), geometry.get("pages")
    if (not isinstance(engine, dict) or engine.get("name") not in {"pypdfium2", "pdfium"}
            or not _token(engine.get("version")) or not isinstance(pages, list) or len(pages) > 1000):
        raise LocalResolutionError("local_resolution_invalid_geometry")
    if not _token(engine.get("pdfium_version")):
        return None, False
    proof_version = engine["version"] + "+pdfium." + engine["pdfium_version"]
    if not _token(proof_version):
        return None, False
    selected = [page for page in pages if isinstance(page, dict) and type(page.get("page_index")) is int and page["page_index"] == page_index]
    if len(selected) != 1:
        return None, False
    page = selected[0]
    media, crop = page.get("media_box"), page.get("crop_box")
    supported = (_box(media) and _box(crop) and media[:2] == [0, 0] and crop[:2] == [0, 0]
        and crop[2] <= media[2] and crop[3] <= media[3]
        and type(page.get("rotation")) is int and page["rotation"] == 0
        and _finite(page.get("user_unit")) and page["user_unit"] == 1)
    if not supported:
        return None, False
    characters = page.get("characters", page.get("chars"))
    if not isinstance(characters, list) or len(characters) > _MAX_TEXT:
        raise LocalResolutionError("local_resolution_invalid_geometry")
    boxes, last_end = {}, -1
    for char in characters:
        if (not isinstance(char, dict) or type(char.get("index")) is not int or char["index"] < 0
                or char["index"] < last_end or type(char.get("index_span", 1)) is not int
                or char.get("index_span", 1) not in {1, 2}
                or not _scalar(char.get("text"), char.get("index_span", 1)) or not _box(char.get("bbox"))):
            raise LocalResolutionError("local_resolution_invalid_geometry")
        boxes[char["index"]] = (char["text"], char.get("index_span", 1), char["bbox"])
        last_end = char["index"] + char.get("index_span", 1)
    skipped = page.get("skipped_characters")
    count = page.get("engine_character_count")
    if (not isinstance(skipped, dict) or type(skipped.get("unmapped_unicode")) is not int
            or type(skipped.get("zero_area")) is not int
            or not 0 <= skipped["unmapped_unicode"] <= _MAX_TEXT * 2
            or not 0 <= skipped["zero_area"] <= _MAX_TEXT * 2
            or type(count) is not int or not 0 <= count <= _MAX_TEXT * 2):
        raise LocalResolutionError("local_resolution_invalid_geometry")
    # An unknown native unit could be an erased word boundary or quotation glyph.
    if skipped["unmapped_unicode"] or "text_units" not in page:
        return None, False
    source_units = page["text_units"]
    if not isinstance(source_units, list) or len(source_units) > _MAX_TEXT:
        raise LocalResolutionError("local_resolution_invalid_geometry")
    text_parts, units, offset, last_end = [], [], 0, 0
    for unit in source_units:
        if (not isinstance(unit, dict) or type(unit.get("index")) is not int or unit["index"] < 0
                or unit["index"] != last_end or type(unit.get("index_span", 1)) is not int
                or unit.get("index_span", 1) not in {1, 2}
                or not _scalar(unit.get("text"), unit.get("index_span", 1))):
            raise LocalResolutionError("local_resolution_invalid_geometry")
        index, value, width = unit["index"], unit["text"], unit.get("index_span", 1)
        entry = boxes.pop(index, None)
        if entry and entry[:2] != (value, width):
            raise LocalResolutionError("local_resolution_invalid_geometry")
        box = entry[2] if entry else None
        units.append((offset, offset + len(value), value, box))
        text_parts.append(value); offset += len(value); last_end = index + width
    if boxes or last_end != count:
        raise LocalResolutionError("local_resolution_invalid_geometry")
    text = "".join(text_parts)
    if len(text) > _MAX_TEXT or ("text" in page and page["text"] != text) or ("text_sha256" in page and page["text_sha256"] != _hash(text)):
        raise LocalResolutionError("local_resolution_hash_mismatch")
    return _EnginePage(text, units, page, engine), True


def _inside(box, rects):
    x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return any(rect[0] - TOLERANCE <= x <= rect[2] + TOLERANCE and rect[1] - TOLERANCE <= y <= rect[3] + TOLERANCE for rect in rects)


def _coverage(engine, span, rects):
    units = [unit for unit in engine.units if unit[0] < span[1] and unit[1] > span[0]
             and not unit[2].isspace() and unit[2] != "\u00ad"]
    if not units or any(unit[3] is None for unit in units):
        return 0.0, False
    return sum(_inside(unit[3], rects) for unit in units) / len(units), True


def _view_span(view, span):
    left, right = bisect_left(view.starts, span[0]), bisect_right(view.ends, span[1])
    if right <= left or any(view.starts[i] < span[0] or view.ends[i] > span[1] for i in range(left, right)):
        return None
    return left, right


def _unique_at(value, needle, expected):
    return value.find(needle) == expected and value.find(needle, expected + 1) < 0


def _context_match(canonical, canonical_span, engine, engine_span, cache):
    for dehyphenate in (False, True):
        if dehyphenate not in cache:
            cache[dehyphenate] = (_punct_view(canonical, dehyphenate=dehyphenate),
                                  _punct_view(engine, dehyphenate=dehyphenate))
        left, right = cache[dehyphenate]
        ls, rs = _view_span(left, canonical_span), _view_span(right, engine_span)
        if ls is None or rs is None or left.text[ls[0]:ls[1]] != right.text[rs[0]:rs[1]]:
            continue
        for width in (64, 48, 32, 24, 16, 12):
            prefix, suffix = left.text[max(0, ls[0] - width):ls[0]], left.text[ls[1]:ls[1] + width]
            if sum(not c.isspace() for c in prefix + suffix) < 12:
                continue
            middle = left.text[ls[0]:ls[1]]
            window = prefix + middle + suffix
            target_start = rs[0] - len(prefix)
            if (target_start >= 0 and right.text[target_start:rs[1] + len(suffix)] == window
                    and _unique_at(left.text, window, ls[0] - len(prefix))
                    and _unique_at(right.text, window, target_start)):
                return True
    return False


def _observation(engine, page, rects, span=None):
    clip = "".join(unit[2] for unit in engine.units if unit[3] is not None and _inside(unit[3], rects))
    result = {"engine": "pdfium", "text_sha256": _hash(engine.text), "clip_text_sha256": _hash(clip),
              "page_text_sha256": page["text_sha256"], "char_count": len(engine.units), "page_index": page["page_index"]}
    if span is not None:
        result["literal_sha256"] = _hash(engine.text[span[0]:span[1]])
    return result


def _text_result(base, page, candidate):
    result = {**base, "status": "resolved_by_local_text", "method": "local_text_normalization", "basis": "canonical_literal",
              "selected_spans": [_span(page, *candidate)]}
    result["engine_observation"] = {"engine": "pypdf", "text_sha256": page["text_sha256"], "page_text_sha256": page["text_sha256"],
        "literal_sha256": result["selected_spans"][0]["literal_sha256"], "char_count": len(page["text"]), "page_index": page["page_index"]}
    return result


def resolve_local_annotation(capture: dict, geometry: dict) -> dict:
    """Produce a schema-safe review; perform no I/O or domain mutation.

    Word boundaries are preserved in both quotation and context comparisons.
    Local text correspondence does not imply operator approval or reading.
    """
    page = _validate_capture(capture)
    result = _base(capture)
    if page is None:
        return result
    raw, original = capture["raw_annotation"], capture["v1_report"]
    quote = raw.get("annotationText", "")
    if not quote.strip() or original.get("candidates_truncated"):
        return result
    _, _, position_warnings = _position(raw.get("annotationPosition"), page["page_index"] + 1)
    if {"cross_page_geometry_not_aligned", "ink_paths_not_aligned"}.intersection(position_warnings):
        return result
    original_candidates = [(entry["text_start"], entry["text_end"]) for entry in original.get("matches", [])]
    strict, truncated = _candidates(page, quote, {}, _MAX_CANDIDATES)
    if truncated or set(original_candidates) != {span for span, _ in strict}:
        raise LocalResolutionError("local_resolution_v1_mismatch")
    canonical, truncated = _text_candidates(page["text"], quote)
    if truncated:
        return result
    engine, supported = _engine_page(geometry, capture, page["page_index"])
    if not supported:
        return result
    if engine is None:
        return _text_result(result, page, canonical[0]) if original.get("match_status") == "unmatched" and len(canonical) == 1 else result
    index, observation, _ = _position(raw.get("annotationPosition"), page["page_index"] + 1)
    rects = [[float(x) for x in rect] for rect in observation.get("rects", [])]
    result["engine_observation"] = _observation(engine, page, rects)
    if index != page["page_index"] or not rects or observation.get("rects_status") != "valid":
        return result
    occurrences, truncated = _candidates({"text": engine.text}, quote, {}, _MAX_CANDIDATES)
    if truncated:
        return result
    if not occurrences:
        return _text_result(result, page, canonical[0]) if original.get("match_status") == "unmatched" and len(canonical) == 1 else result
    scores = [_coverage(engine, span, rects) for span, _ in occurrences]
    if not all(complete for _, complete in scores):
        return result
    ranked = sorted(range(len(scores)), key=lambda i: scores[i][0], reverse=True)
    winner = ranked[0]
    overlap = scores[winner][0]
    runner = scores[ranked[1]][0] if len(ranked) > 1 else 0.0
    if sum(score >= OVERLAP_THRESHOLD for score, _ in scores) != 1 or overlap < OVERLAP_THRESHOLD or overlap - runner < MARGIN_THRESHOLD:
        return result
    engine_span = occurrences[winner][0]
    selected_pool = original_candidates if original.get("match_status") == "ambiguous" else canonical
    context_cache = {}
    mappings = [[i for i, (span, _) in enumerate(occurrences)
                 if _context_match(page["text"], candidate, engine.text, span, context_cache)] for candidate in selected_pool]
    matched = [i for i, mappings_for_candidate in enumerate(mappings) if mappings_for_candidate == [winner]]
    result["parameters"] = dict(engine="combined", engine_version=engine.engine["version"] + "+pdfium." + engine.engine["pdfium_version"],
        normalization_version=NORMALIZATION_VERSION, overlap_threshold=OVERLAP_THRESHOLD,
        margin_threshold=MARGIN_THRESHOLD, tolerance=TOLERANCE, coordinate_system="pdf_points_bottom_left")
    result["engine_observation"] = _observation(engine, page, rects, engine_span)
    geometry_report = {"overlap": float(overlap), "runner_up_overlap": float(runner), "rects": rects}
    bijective = all(len(indices) == 1 for indices in mappings) and len({indices[0] for indices in mappings}) == len(mappings)
    if len(matched) == 1 and (original.get("match_status") != "ambiguous" or bijective):
        selected = matched[0]
        result.update(status="resolved_by_geometry" if original.get("match_status") == "ambiguous" else "resolved_by_local_text",
            method="geometry_overlap" if original.get("match_status") == "ambiguous" else "geometry_and_local_text",
            basis="geometry_unique" if original.get("match_status") == "ambiguous" else "geometry_and_text",
            selected_spans=[_span(page, *selected_pool[selected])], geometry=geometry_report)
        if original.get("match_status") == "ambiguous":
            result["geometry"]["candidate_index"] = selected
        return result
    if original.get("match_status") == "unmatched" and len(canonical) == 1:
        # Independent literal equality under the limited punctuation rules is
        # sufficient for text correspondence; do not claim a geometry mapping.
        return _text_result(_base(capture), page, canonical[0])
    # An independently located quote can differ from the frozen canonical parser.
    # Never select a merely compact-whitespace span, even if it is unique.
    result.update(status="located_with_parser_difference", method="geometry_and_local_text", basis="parser_difference",
                  selected_spans=[], geometry=geometry_report)
    return result
