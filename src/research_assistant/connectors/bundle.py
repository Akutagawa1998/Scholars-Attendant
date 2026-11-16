"""Explicit, read-only Notion exports without invented atomic source versions."""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .common import ConnectorError, aware_timestamp, now, redact_payload
from .notion import normalize_mcp_page, page_id


def collection_namespace(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("collection://"):
        raise ConnectorError("invalid_configuration")
    return "collection://" + page_id(value.removeprefix("collection://"))


class NotionBundleAdapter:
    """An explicitly traversed collection export is still a best-effort view.

    The exporter can assert inventory traversal completion. Neither that flag
    nor a timestamp establishes an atomic Notion collection version, and page
    absence supplies no deletion evidence.
    """
    def __init__(self, bundle: dict[str, Any], *, max_pages: int = 5, require_complete: bool = False):
        if not isinstance(bundle, dict):
            raise ConnectorError("invalid_configuration")
        self.source_instance_id = bundle.get("source_instance_id")
        if (not isinstance(self.source_instance_id, str) or not self.source_instance_id.strip()
                or len(self.source_instance_id) > 200 or bundle.get("provider", "notion") != "notion"):
            raise ConnectorError("invalid_configuration")
        self.namespace = collection_namespace(bundle.get("namespace"))
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 100000:
            raise ConnectorError("invalid_configuration")
        pages = bundle.get("pages")
        if not isinstance(pages, list) or len(pages) > max_pages:
            raise ConnectorError("invalid_bounded_bundle")
        self.inventory_complete = bundle.get("inventory_complete") is True
        if require_complete and not self.inventory_complete:
            raise ConnectorError("incomplete_bundle_inventory")
        self._snapshots: dict[str, dict[str, Any]] = {}
        for entry in pages:
            snapshot = self._page(entry)
            key = snapshot["external_id"]
            if key in self._snapshots:
                raise ConnectorError("duplicate_source_identity")
            self._snapshots[key] = snapshot
        deleted = bundle.get("deleted", [])
        if not isinstance(deleted, list):
            raise ConnectorError("invalid_source_response")
        self._deleted: dict[str, dict[str, Any]] = {}
        for entry in deleted:
            snapshot = self._deletion(entry)
            key = snapshot["external_id"]
            if key in self._snapshots or key in self._deleted:
                raise ConnectorError("duplicate_source_identity")
            self._deleted[key] = snapshot

    def _identity(self, entry: Any) -> str:
        if not isinstance(entry, dict):
            raise ConnectorError("invalid_source_response")
        if (entry.get("provider", "notion") != "notion"
                or entry.get("source_instance_id", self.source_instance_id) != self.source_instance_id
                or collection_namespace(entry.get("namespace", self.namespace)) != self.namespace):
            raise ConnectorError("outside_registered_source")
        return page_id(entry.get("external_id"))

    def _membership(self, raw: Any, identity: str) -> None:
        if not isinstance(raw, dict):
            raise ConnectorError("invalid_source_response")
        if "id" in raw and page_id(raw["id"]) != identity:
            raise ConnectorError("source_identity_mismatch")
        parents = []
        for candidate in (raw, raw.get("page")):
            if not isinstance(candidate, dict):
                continue
            if candidate is not raw and "id" in candidate and page_id(candidate["id"]) != identity:
                raise ConnectorError("source_identity_mismatch")
            parent = candidate.get("parent")
            if isinstance(parent, dict) and "data_source_id" in parent:
                parents.append(page_id(parent["data_source_id"]))
        text = raw.get("text")
        if isinstance(text, str):
            ancestor = re.search(r"<ancestor-path\b[^>]*>(.*?)</ancestor-path\s*>", text, re.DOTALL | re.IGNORECASE)
            if ancestor:
                for tag in re.finditer(r"<parent-data-source\b[^>]*>", ancestor.group(1), re.IGNORECASE):
                    url = re.search(r'''\burl\s*=\s*(["'])(.*?)\1''', tag.group(), re.DOTALL | re.IGNORECASE)
                    if url:
                        source = url.group(2).strip().removeprefix("{{").removesuffix("}}").strip()
                        parents.append(collection_namespace(source).removeprefix("collection://"))
        registered = self.namespace.removeprefix("collection://")
        if not parents or any(parent != registered for parent in parents):
            raise ConnectorError("outside_registered_source")

    def _page(self, entry: Any) -> dict[str, Any]:
        identity = self._identity(entry)
        raw = entry.get("raw_payload")
        self._membership(raw, identity)
        version = entry.get("external_version", raw.get("page_last_edited_at"))
        if version is not None and not isinstance(version, str):
            raise ConnectorError("invalid_source_response")
        markdown_path = entry.get("markdown_path")
        passage_paths = entry.get("passage_paths")
        if markdown_path is None and not passage_paths and isinstance(raw.get("text"), str):
            markdown_path = ["text"]
        return normalize_mcp_page(raw, source_instance_id=self.source_instance_id, namespace=self.namespace,
            external_id=identity, markdown_path=markdown_path, passage_paths=passage_paths,
            external_version=version, source_url=entry.get("source_url"),
            occurred_at=entry.get("occurred_at", raw.get("page_last_edited_at")),
            completeness=entry.get("completeness", "unknown"), missing_parts=entry.get("missing_parts"))

    def _deletion(self, entry: Any) -> dict[str, Any]:
        identity = self._identity(entry)
        if entry.get("change") not in {"delete", "access_revoked"} or not isinstance(entry.get("raw_payload"), dict):
            raise ConnectorError("invalid_source_response")
        raw, redactions = redact_payload(entry["raw_payload"])
        if "id" in raw and page_id(raw["id"]) != identity:
            raise ConnectorError("source_identity_mismatch")
        version = entry.get("external_version")
        if version is not None and not isinstance(version, str):
            raise ConnectorError("invalid_source_response")
        missing = list(entry.get("missing_parts") or [])
        if redactions:
            missing.append("temporary_urls_or_credentials_redacted")
        return {
            "provider": "notion", "source_instance_id": self.source_instance_id, "namespace": self.namespace,
            "external_id": identity, "external_version": version,
            "source_url": "https://www.notion.so/" + identity.replace("-", ""), "raw_payload": raw,
            "observed_at": now(), "occurred_at": aware_timestamp(entry.get("occurred_at")),
            "completeness": "partial" if redactions else entry.get("completeness", "unknown"),
            "missing_parts": sorted(set(missing)), "change": entry["change"], "passages": [],
        }

    @property
    def snapshots(self) -> list[dict[str, Any]]:
        return deepcopy(list(self._snapshots.values()))

    def inventory(self) -> dict[str, Any]:
        return {
            "version": None, "object_versions": {key: value["external_version"] for key, value in self._snapshots.items()},
            "complete": self.inventory_complete, "consistent": False,
            "metadata": {"provider": "notion", "scope": "explicit_collection_bundle",
                         "atomic_source_version_available": False,
                         "missing_parts": ["best_effort_no_atomic_source_snapshot"]},
        }

    def fetch_objects(self, keys: list[str], expected_version: str | None) -> list[dict[str, Any]]:
        if expected_version is not None or not isinstance(keys, list):
            raise ConnectorError("invalid_configuration")
        identities = [page_id(key) for key in keys]
        if len(set(identities)) != len(identities):
            raise ConnectorError("invalid_configuration")
        if any(key not in self._snapshots for key in identities):
            raise ConnectorError("objects_missing_from_response")
        return deepcopy([self._snapshots[key] for key in identities])

    def deleted(self, since: int, expected_version: str | None) -> list[dict[str, Any]]:
        if isinstance(since, bool) or not isinstance(since, int) or since < 0 or expected_version is not None:
            raise ConnectorError("invalid_configuration")
        return deepcopy(list(self._deleted.values()))
