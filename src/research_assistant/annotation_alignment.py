"""Pure, page-bound Zotero annotation alignment against fixed PDF extraction.

Provider positions and literal text correspondence are independent observations.
No rectangle is geometrically verified here, and a provider page label never
selects a PDF page. Official format references (observed 2026-10-21):
https://github.com/zotero/zotero/blob/master/chrome/content/zotero/xpcom/annotations.js
https://github.com/zotero/reader/blob/master/src/pdf/pdf-view.js
https://github.com/zotero/reader/blob/master/src/pdf/selection.js
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any
import unicodedata

from .assets import AssetError

ALIGNMENT_VERSION = "zotero-page-text-v1"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_KINDS = {"highlight": "highlight", "underline": "highlight", "note": "note", "text": "note", "image": "image", "ink": "ink"}
_LINE_BREAKS = frozenset("\n\r\u0085\u2028\u2029")
_LIMITS = {"annotations": 10000, "pages": 10000, "text_chars": 5_000_000,
           "quote_chars": 100000, "position_chars": 1024 * 1024, "rects": 1000,
           "normalized_chars": 5_000_000, "combining_cluster_chars": 1024}


class AnnotationAlignmentError(AssetError):
    """A stable failure code without content, provider identifiers or paths."""


def _hash(value: str) -> str:
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        raise AnnotationAlignmentError("invalid_extraction_unicode") from None


def _extraction_pages(extraction: dict, expected_pdf_sha256: str) -> list[dict]:
    if (not isinstance(expected_pdf_sha256, str) or not _SHA.fullmatch(expected_pdf_sha256)
            or not isinstance(extraction, dict) or extraction.get("source_sha256") != expected_pdf_sha256):
        raise AnnotationAlignmentError("pdf_hash_mismatch")
    pages = extraction.get("pages")
    if not isinstance(pages, list) or not pages or len(pages) > _LIMITS["pages"]:
        raise AnnotationAlignmentError("invalid_extraction_pages")
    total = 0
    for index, page in enumerate(pages):
        if (not isinstance(page, dict) or type(page.get("page_index")) is not int or page["page_index"] != index
                or not isinstance(page.get("text"), str)):
            raise AnnotationAlignmentError("invalid_extraction_pages")
        total += len(page["text"])
        if total > _LIMITS["text_chars"]:
            raise AnnotationAlignmentError("annotation_alignment_limit")
        if _hash(page["text"]) != page.get("text_sha256"):
            raise AnnotationAlignmentError("page_text_hash_mismatch")
    return pages


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_position_field")
        value[key] = item
    return value


def _rectangles(value):
    if value is None:
        return [], "absent"
    if not isinstance(value, list) or len(value) > _LIMITS["rects"]:
        return [], "invalid"
    result = []
    for rect in value:
        if (not isinstance(rect, list) or len(rect) != 4
                or any(type(coordinate) not in {int, float} or abs(coordinate) > 1_000_000
                       or not math.isfinite(coordinate) for coordinate in rect)
                or not rect[0] < rect[2] or not rect[1] < rect[3]):
            return [], "invalid"
        result.append(list(rect))
    return result, "valid" if result else "absent"


def _position(value, page_count):
    try:
        if isinstance(value, str):
            if len(value) > _LIMITS["position_chars"]:
                return None, {}, ["position_size_limit"]
            value = json.loads(value, object_pairs_hook=_unique_json_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_json_number")))
        if not isinstance(value, dict):
            return None, {}, ["invalid_annotation_position"]
        page_index = value.get("pageIndex")
        if type(page_index) is not int or not 0 <= page_index < page_count:
            return None, {}, ["invalid_annotation_page_index"]
        rects, rects_status = _rectangles(value.get("rects"))
        warnings = ["geometry_not_verified"]
        if rects_status == "invalid":
            warnings.append("invalid_annotation_rects")
        if "nextPageRects" in value:
            warnings.append("cross_page_geometry_not_aligned")
        if "paths" in value:
            warnings.append("ink_paths_not_aligned")
        return page_index, {"rects": rects, "rects_status": rects_status, "geometric_verified": False}, warnings
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None, {}, ["invalid_annotation_position"]


@dataclass
class _Normalized:
    text: str
    starts: array
    ends: array


def _nfkc_units(value: str):
    """Map canonical-combining clusters back to original character spans.

    Compatibility expansion can create several normalized characters from one
    glyph; canonical composition can merge several originals. Whole-cluster
    spans are conservative, and every candidate is re-normalized from its
    literal original slice before acceptance (preventing partial-glyph matches).
    """
    cluster, start, end = [], 0, 0
    for index, original in enumerate(value):
        for character in unicodedata.normalize("NFKD", original):
            if cluster and not unicodedata.combining(character):
                current = unicodedata.normalize("NFC", "".join(cluster))
                joined = unicodedata.normalize("NFC", current + character)
                if len(joined) >= len(current) + 1:
                    for output in current:
                        yield output, start, end
                    cluster = []
            if not cluster:
                start = index
            cluster.append(character)
            if len(cluster) > _LIMITS["combining_cluster_chars"]:
                raise AnnotationAlignmentError("annotation_normalization_limit")
            end = index + 1
    if cluster:
        for output in unicodedata.normalize("NFC", "".join(cluster)):
            yield output, start, end


def _normalize(value: str, *, dehyphenate: bool) -> _Normalized:
    chars, starts, ends = [], array("I"), array("I")
    for character, start, end in _nfkc_units(value):
        if character != "\u00ad":
            if len(chars) >= _LIMITS["normalized_chars"]:
                raise AnnotationAlignmentError("annotation_normalization_limit")
            chars.append(character)
            starts.append(start)
            ends.append(end)
    output, mapped_starts, mapped_ends = [], array("I"), array("I")
    index = 0
    while index < len(chars):
        character = chars[index]
        if character in {"-", "\u2010"} and index and chars[index - 1].isalpha():
            following, line_break = index + 1, False
            while following < len(chars) and chars[following].isspace():
                line_break |= chars[following] in _LINE_BREAKS
                following += 1
            if line_break and following < len(chars) and chars[following].isalpha():
                if not dehyphenate:
                    output.append(character)
                    mapped_starts.append(starts[index])
                    mapped_ends.append(ends[index])
                index = following
                continue
        if character.isspace():
            following = index + 1
            while following < len(chars) and chars[following].isspace():
                following += 1
            output.append(" ")
            mapped_starts.append(starts[index])
            mapped_ends.append(ends[following - 1])
            index = following
        else:
            output.append(character)
            mapped_starts.append(starts[index])
            mapped_ends.append(ends[index])
            index += 1
    return _Normalized("".join(output), mapped_starts, mapped_ends)


def _occurrences(value: str, quote: str):
    start = value.find(quote)
    while start >= 0:
        yield start, start + len(quote)
        start = value.find(quote, start + 1)


def _candidates(page, quote, cache, maximum):
    """Return literal spans, including normalized alternatives to an exact hit."""
    value = page["text"]
    candidates: dict[tuple[int, int], str] = {}
    truncated = False
    for start, end in _occurrences(value, quote):
        candidates[start, end] = "exact"
        if len(candidates) > maximum:
            return list(candidates.items())[:maximum], True
    for dehyphenate in (False, True):
        target = _normalize(quote, dehyphenate=dehyphenate).text
        if not target:
            continue
        if dehyphenate not in cache:
            cache[dehyphenate] = _normalize(value, dehyphenate=dehyphenate)
        normalized = cache[dehyphenate]
        for start, end in _occurrences(normalized.text, target):
            span = normalized.starts[start], normalized.ends[end - 1]
            literal = value[span[0]:span[1]]
            if _normalize(literal, dehyphenate=dehyphenate).text != target:
                continue
            candidates.setdefault(span, "normalized")
            if len(candidates) > maximum:
                truncated = True
                break
        if truncated:
            break
    return list(sorted(candidates.items()))[:maximum], truncated


def align_annotations(extraction: dict[str, Any], annotations: list[dict[str, Any]], *,
                      expected_pdf_sha256: str, max_candidates: int = 64) -> list[dict[str, Any]]:
    """Return source-bound reports; caller owns authorization/persistence.

    `text_start/text_end` index the original extracted page Unicode text. Only
    exact/normalized unique matches are resolved; ambiguous reports have no
    selected match. No annotation author or quote establishes user reading.
    """
    pages = _extraction_pages(extraction, expected_pdf_sha256)
    if (not isinstance(annotations, list) or len(annotations) > _LIMITS["annotations"]
            or type(max_candidates) is not int or not 1 <= max_candidates <= 1024):
        raise AnnotationAlignmentError("annotation_alignment_limit")
    reports, caches, quote_chars = [], {}, 0
    for annotation in annotations:
        if not isinstance(annotation, dict):
            raise AnnotationAlignmentError("invalid_annotation_data")
        quote = annotation.get("annotationText", "")
        if isinstance(quote, str):
            quote_chars += len(quote)
            if len(quote) > _LIMITS["quote_chars"] or quote_chars > _LIMITS["text_chars"]:
                raise AnnotationAlignmentError("annotation_alignment_limit")
        provider_type = annotation.get("annotationType")
        page_index, observation, warnings = _position(annotation.get("annotationPosition"), len(pages))
        provider_label = annotation.get("annotationPageLabel")
        if provider_label is not None and not isinstance(provider_label, str):
            warnings.append("invalid_provider_page_label")
        kind = _KINDS.get(provider_type, "unknown") if isinstance(provider_type, str) else "unknown"
        if kind == "unknown":
            warnings.append("unsupported_annotation_type")
        report = {"alignment_version": ALIGNMENT_VERSION, "annotation_key": annotation.get("key"),
            "provider_annotation_type": provider_type, "kind": kind,
            "raw_annotation_text": quote, "raw_annotation_comment": annotation.get("annotationComment", ""),
            "page_index": page_index, "page_number": page_index + 1 if page_index is not None else None,
            "provider_page_label": provider_label if isinstance(provider_label, str) else None,
            "source_sha256": expected_pdf_sha256,
            "page_text_sha256": pages[page_index]["text_sha256"] if page_index is not None else None,
            "position_observation": observation, "matches": [], "match_status": "invalid_position",
            "candidate_count": 0, "candidates_truncated": False, "warnings": warnings}
        if page_index is not None:
            if not isinstance(quote, str):
                report["match_status"] = "unmatched"
                warnings.append("invalid_annotation_text")
            elif not quote.strip() or not _normalize(quote, dehyphenate=True).text.strip():
                report["match_status"] = "page_only"
            else:
                page = pages[page_index]
                candidates, truncated = _candidates(page, quote, caches.setdefault(page_index, {}), max_candidates)
                for (start, end), method in candidates:
                    literal = page["text"][start:end]
                    report["matches"].append({"text_start": start, "text_end": end, "literal_text": literal,
                        "literal_sha256": _hash(literal), "source_sha256": expected_pdf_sha256,
                        "page_text_sha256": page["text_sha256"], "match_method": method})
                count = len(candidates)
                report["candidate_count"] = None if truncated else count
                report["candidates_truncated"] = truncated
                if truncated:
                    report["candidate_count_lower_bound"] = max_candidates + 1
                    warnings.append("candidate_limit")
                report["match_status"] = "ambiguous" if truncated or count > 1 else candidates[0][1] if count else "unmatched"
        reports.append(report)
    return reports
