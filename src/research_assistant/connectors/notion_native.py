"""Assess native Notion fetch facts without assuming a tool envelope schema.

The exporter supplies paths confirmed against the actual tool response. This
module does no fetching, Markdown/XML interpretation, page traversal or writes.
Completeness here describes the fetched page representation, not child pages,
database rows, comments, transcripts, attachments or an atomic workspace view.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .common import ConnectorError, aware_timestamp


_FIELDS = frozenset({"truncated", "unknown_block_count", "unknown_block_ids", "page_last_edited_at"})
_MISSING = object()


def _paths(paths: Mapping[str, list[str | int] | tuple[str | int, ...]]) -> dict[str, tuple[str | int, ...]]:
    if not isinstance(paths, Mapping) or any(key not in _FIELDS for key in paths):
        raise ConnectorError("invalid_configuration")
    result = {}
    for field, path in paths.items():
        if (not isinstance(path, (list, tuple)) or not path
                or any(type(part) not in (str, int) or (type(part) is int and part < 0) for part in path)):
            raise ConnectorError("invalid_configuration")
        result[field] = tuple(path)
    return result


def _at(raw: dict[str, Any], path: tuple[str | int, ...] | None) -> Any:
    if path is None:
        return _MISSING
    value: Any = raw
    for part in path:
        if type(part) is str and isinstance(value, dict):
            if part not in value:
                return _MISSING
            value = value[part]
        elif type(part) is int and isinstance(value, list):
            if part >= len(value):
                return _MISSING
            value = value[part]
        else:
            return _MISSING
    return value


def native_page_kwargs(raw_payload: dict[str, Any], *,
        paths: Mapping[str, list[str | int] | tuple[str | int, ...]]) -> dict[str, Any]:
    """Return fact-derived keyword arguments for ``normalize_mcp_page``.

    The raw object passes through unchanged. No conventional field location is
    assumed: even identically named top-level fields need an explicit path.
    Retain that mapping with the private export provenance; it is not a claim
    about the connector's output schema.

    Only literal ``False``, integer ``0`` (excluding bool), and an empty list
    for the three native completeness fields prove an untruncated, known-block
    representation. Missing/malformed facts remain unknown; explicit gaps are
    partial. A native edit timestamp stays the provider revision verbatim; an
    absent/invalid timestamp stays unknown rather than using observation time.
    Redaction and other source warnings remain the normalizer/caller's concern.
    """
    if not isinstance(raw_payload, dict):
        raise ConnectorError("invalid_source_response")
    locations = _paths(paths)
    truncated = _at(raw_payload, locations.get("truncated"))
    count = _at(raw_payload, locations.get("unknown_block_count"))
    identities = _at(raw_payload, locations.get("unknown_block_ids"))
    missing = []
    explicit_gap = False
    if type(truncated) is not bool:
        missing.append("native_truncation_unknown")
    elif truncated:
        missing.append("markdown_truncated")
        explicit_gap = True
    if type(count) is not int or count < 0:
        missing.append("native_unknown_block_count_unknown")
    elif count > 0:
        missing.append("unknown_blocks")
        explicit_gap = True
    if not isinstance(identities, list) or any(not isinstance(item, str) or not item for item in identities):
        missing.append("native_unknown_block_ids_unknown")
    elif identities:
        missing.append("unknown_blocks")
        explicit_gap = True

    revision = _at(raw_payload, locations.get("page_last_edited_at"))
    if aware_timestamp(revision) is None:
        revision = None
    return {
        "raw_payload": raw_payload,
        "completeness": "partial" if explicit_gap else "unknown" if missing else "complete",
        "missing_parts": sorted(set(missing)),
        "external_version": revision,
        "occurred_at": revision,
    }
