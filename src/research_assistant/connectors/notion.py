"""Notion fixed-version read-only API adapter and connected-tool snapshots.

Search is deliberately absent: registered data sources and explicit pages are
sync roots. MCP export normalization remains honest about unknown completeness.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID
import re
import httpx
from .common import ConnectorError, ReadOnlyHTTP, SyncBatch, aware_timestamp, now, redact_payload

NOTION_API_VERSION = "2025-09-03"


def page_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConnectorError("invalid_configuration") from exc


def normalize_mcp_page(raw_payload: dict[str, Any], *, source_instance_id: str, namespace: str,
                       external_id: str, markdown_path: list[str | int] | None = None,
                       passage_paths: list[dict[str, Any]] | None = None,
                       external_version: str | None = None, source_url: str | None = None,
                       occurred_at: str | None = None, completeness: str = "unknown",
                       missing_parts: list[str] | None = None) -> dict[str, Any]:
    """Save a sanitized, still source-reported MCP page snapshot with exact paths.

    passage_paths is a list of {json_path:[...],section:"body"|"title"|...}.
    Signed URLs embedded in Markdown/XML are redacted before hashing/evidence.
    This snapshot is not claimed to be the original HTTP byte stream.
    """
    if not source_instance_id or not namespace or completeness not in {"complete", "partial", "unknown"}:
        raise ConnectorError("invalid_configuration")
    cleaned, redactions = redact_payload(raw_payload)
    paths = list(passage_paths or [])
    if markdown_path is not None:
        paths.append({"json_path": markdown_path, "section": "page_markdown"})
    if not paths:
        raise ConnectorError("missing_source_text")
    passages = []
    for locator in paths:
        cursor: Any = cleaned
        path = locator.get("json_path")
        if not isinstance(path, list) or not all(isinstance(part, (str, int)) for part in path):
            raise ConnectorError("invalid_configuration")
        try:
            for part in path:
                cursor = cursor[part]
        except (KeyError, IndexError, TypeError) as exc:
            raise ConnectorError("invalid_source_response") from exc
        if not isinstance(cursor, str) or not cursor:
            raise ConnectorError("missing_source_text")
        passages.append({"text": cursor, "json_path": path, "section": locator.get("section", "body")})
    identity = page_id(external_id)
    safe_url, url_redactions = redact_payload(source_url or "https://www.notion.so/" + identity.replace("-", ""))
    redactions += url_redactions
    missing = list(missing_parts or [])
    if redactions:
        completeness = "partial"
        missing.append("temporary_urls_or_credentials_redacted")
    return {
        "provider": "notion", "source_instance_id": source_instance_id, "namespace": namespace,
        "external_id": identity, "external_version": external_version,
        "source_url": safe_url, "raw_payload": cleaned, "observed_at": now(),
        "occurred_at": aware_timestamp(occurred_at), "completeness": completeness,
        "missing_parts": sorted(set(missing)), "change": "upsert", "passages": passages,
    }


def connected_page_snapshot(*, source_instance_id: str, namespace: str,
                            external_id: str, raw_payload: dict[str, Any],
                            markdown_path: list[str | int], external_version: str | None = None,
                            source_url: str | None = None, occurred_at: str | None = None,
                            completeness: str = "unknown", missing_parts: list[str] | None = None) -> dict[str, Any]:
    return normalize_mcp_page(raw_payload, source_instance_id=source_instance_id, namespace=namespace,
        external_id=external_id, markdown_path=markdown_path, external_version=external_version,
        source_url=source_url, occurred_at=occurred_at, completeness=completeness, missing_parts=missing_parts)


class NotionReadOnlyClient(ReadOnlyHTTP):
    origin = "https://api.notion.com"

    def __init__(self, api_token: str, *, source_instance_id: str,
                 transport: httpx.BaseTransport | None = None, timeout: float = 30):
        if not api_token or not source_instance_id:
            raise ConnectorError("invalid_configuration")
        self.source_instance_id = source_instance_id
        super().__init__({"Authorization": "Bearer " + api_token,
                          "Notion-Version": NOTION_API_VERSION, "Accept": "application/json"},
                         transport=transport, timeout=timeout)

    def readonly_post(self, path: str) -> bool:
        return bool(re.fullmatch(r"/v1/data_sources/[0-9a-f-]{36}/query", path))

    def fetch_page(self, external_id: str, *, namespace: str) -> dict[str, Any]:
        identity = page_id(external_id)
        page, _ = self.request("GET", "/v1/pages/" + identity)
        if not isinstance(page, dict):
            raise ConnectorError("invalid_source_response")
        if namespace.startswith("collection://"):
            parent = page.get("parent", {})
            registered = page_id(namespace.removeprefix("collection://"))
            if not isinstance(parent, dict) or parent.get("data_source_id") != registered:
                raise ConnectorError("outside_registered_source")
        if page.get("in_trash") or page.get("archived"):
            return {
                "provider": "notion", "source_instance_id": self.source_instance_id, "namespace": namespace,
                "external_id": identity, "external_version": page.get("last_edited_time"),
                "source_url": "https://www.notion.so/" + identity.replace("-", ""),
                "raw_payload": page, "observed_at": now(), "occurred_at": aware_timestamp(page.get("last_edited_time")),
                "completeness": "complete", "missing_parts": [], "change": "delete", "passages": [],
            }
        markdown, _ = self.request("GET", "/v1/pages/" + identity + "/markdown")
        if not isinstance(markdown, dict) or not isinstance(markdown.get("markdown"), str):
            raise ConnectorError("invalid_source_response")
        after, _ = self.request("GET", "/v1/pages/" + identity)
        if not isinstance(after, dict) or after.get("last_edited_time") != page.get("last_edited_time"):
            raise ConnectorError("source_changed_during_read")
        if after.get("in_trash") or after.get("archived"):
            raise ConnectorError("source_changed_during_read")
        missing = []
        if markdown.get("truncated"):
            missing.append("markdown_truncated")
        if markdown.get("unknown_block_ids"):
            missing.append("unknown_blocks")
        if markdown.get("warnings"):
            missing.append("source_warnings")
        # File metadata may contain temporary signed URLs. Only fields used as
        # durable provenance are retained alongside the Markdown snapshot.
        durable_page = {key: page[key] for key in ("object", "id", "parent", "last_edited_time", "created_time", "archived", "in_trash") if key in page}
        raw_payload = {"page": durable_page, "page_markdown": markdown}
        return connected_page_snapshot(
            source_instance_id=self.source_instance_id, namespace=namespace, external_id=identity,
            raw_payload=raw_payload, markdown_path=["page_markdown", "markdown"],
            external_version=page.get("last_edited_time"), occurred_at=page.get("last_edited_time"),
            completeness="partial" if missing else "complete", missing_parts=missing,
        )

    def fetch_data_source(self, data_source_id: str, *, max_pages: int = 5) -> SyncBatch:
        identity = page_id(data_source_id)
        if not 1 <= max_pages <= 100000:
            raise ConnectorError("invalid_configuration")
        namespace = "collection://" + identity
        batch = SyncBatch([], "notion", self.source_instance_id, namespace)
        cursor, seen = None, set()
        while len(batch.snapshots) < max_pages:
            request = {"page_size": min(100, max_pages - len(batch.snapshots))}
            if cursor is not None:
                request["start_cursor"] = cursor
            raw, _ = self.request("POST", "/v1/data_sources/" + identity + "/query", json=request)
            batch.pages_fetched += 1
            if not isinstance(raw, dict) or not isinstance(raw.get("results"), list):
                raise ConnectorError("invalid_source_response")
            for page in raw["results"]:
                if not isinstance(page, dict) or not isinstance(page.get("id"), str):
                    raise ConnectorError("invalid_source_response")
                batch.snapshots.append(self.fetch_page(page["id"], namespace=namespace))
                if len(batch.snapshots) >= max_pages:
                    break
            if not raw.get("has_more"):
                # Query pagination cannot give a workspace-wide atomic snapshot.
                batch.missing_parts.append("best_effort_no_atomic_source_snapshot")
                break
            cursor = raw.get("next_cursor")
            if not isinstance(cursor, str) or cursor in seen:
                raise ConnectorError("invalid_pagination_cursor")
            seen.add(cursor)
            if len(batch.snapshots) >= max_pages:
                batch.missing_parts.append("bounded_initial_sample")
                break
        # A source enumeration does not prove all deleted/revoked pages were seen.
        # Leave the full-sync checkpoint unset until reconciliation is implemented.
        return batch
