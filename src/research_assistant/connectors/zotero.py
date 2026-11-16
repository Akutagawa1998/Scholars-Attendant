"""Zotero Web API v3 read-only metadata synchronization.

This adapter deliberately does not download attachments, submit writes, infer
reading from tags/annotations, or advance a full-library sync checkpoint itself.
``fetch`` retains the bounded paper sample; the inventory/object/deleted methods
provide the full-library metadata protocol for a persistent sync coordinator.
"""
from __future__ import annotations

from typing import Any
import math
import re
import time
import httpx
from .common import ConnectorError, ReadOnlyHTTP, SyncBatch, aware_timestamp, now, redact_payload

PAPER_TYPES = frozenset({"journalArticle", "conferencePaper", "preprint"})
INSTANCE = "api.zotero.org:v3"


class ZoteroReadOnlyClient(ReadOnlyHTTP):
    origin = "https://api.zotero.org"

    def __init__(self, api_key: str, library_id: str, *, library_type: str = "user",
                 transport: httpx.BaseTransport | None = None, timeout: float = 30):
        if not api_key or library_type not in {"user", "group"} or not re.fullmatch(r"[0-9]+", str(library_id)):
            raise ConnectorError("invalid_configuration")
        self.library_type = library_type
        self.library_id = str(library_id)
        self.namespace = ("users/" if library_type == "user" else "groups/") + self.library_id
        self.source_instance_id = INSTANCE
        self._next_allowed_at = 0.0
        self._last_backoff_seconds = 0.0
        super().__init__({"Zotero-API-Key": api_key, "Zotero-API-Version": "3",
                          "Accept": "application/json"}, transport=transport, timeout=timeout)

    def request(self, method: str, path: str, *, params=None, json=None) -> tuple[Any, httpx.Response]:
        remaining = self._next_allowed_at - time.monotonic()
        if remaining > 0:
            raise ConnectorError("rate_limited", retry_after=remaining)
        raw, response = super().request(method, path, params=params, json=json)
        self._last_backoff_seconds = 0.0
        if "Backoff" in response.headers:
            try:
                backoff = float(response.headers["Backoff"])
            except ValueError as exc:
                raise ConnectorError("invalid_source_response") from exc
            if not math.isfinite(backoff) or backoff < 0:
                raise ConnectorError("invalid_source_response")
            self._last_backoff_seconds = backoff
            self._next_allowed_at = time.monotonic() + backoff
        return raw, response

    def _snapshot(self, item: dict[str, Any], observed: str) -> dict[str, Any]:
        item, redactions = redact_payload(item)
        data = item.get("data")
        if not isinstance(data, dict) or data.get("itemType") not in PAPER_TYPES:
            raise ConnectorError("invalid_source_response")
        key = item.get("key") or data.get("key")
        version = item.get("version", data.get("version"))
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9]{8}", key) or version is None:
            raise ConnectorError("invalid_source_response")
        passages = []
        for field in ("title", "abstractNote", "publicationTitle", "proceedingsTitle", "extra"):
            text = data.get(field)
            if isinstance(text, str) and text:
                passages.append({"text": text, "json_path": ["data", field], "section": field})
        if not passages:
            raise ConnectorError("missing_source_text")
        # Zotero item version identifies an object revision, not a paper version.
        return {
            "provider": "zotero", "source_instance_id": INSTANCE, "namespace": self.namespace,
            "external_id": key, "external_version": str(version),
            "source_url": self.origin + "/" + self.namespace + "/items/" + key,
            "raw_payload": item, "observed_at": observed,
            "occurred_at": aware_timestamp(data.get("dateModified")),
            "completeness": "partial" if redactions else "complete",
            "missing_parts": ["temporary_urls_or_credentials_redacted"] if redactions else [],
            "change": "upsert", "passages": passages,
        }

    @staticmethod
    def _version(value: Any, *, configuration: bool = False) -> str:
        code = "invalid_configuration" if configuration else "invalid_source_response"
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"[0-9]+", str(value)):
            raise ConnectorError(code)
        return str(int(value))

    @staticmethod
    def _key(value: Any, *, configuration: bool = False) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{8}", value):
            raise ConnectorError("invalid_configuration" if configuration else "invalid_source_response")
        return value

    def _library_version(self, response: httpx.Response, expected: str | None = None) -> str:
        value = response.headers.get("Last-Modified-Version")
        if value is None:
            raise ConnectorError("missing_library_version")
        version = self._version(value)
        if expected is not None and version != expected:
            raise ConnectorError("source_changed_during_pagination")
        return version

    @staticmethod
    def _unpaginated(response: httpx.Response, count: int) -> None:
        # format=versions is explicitly unbounded in the Zotero API. An
        # unexpected truncated response must never become an authoritative set.
        next_relation = r'''\brel\s*=\s*(?:"[^"]*\bnext\b[^"]*"|'[^']*\bnext\b[^']*'|next\b)'''
        if re.search(next_relation, response.headers.get("Link", ""), re.IGNORECASE):
            raise ConnectorError("incomplete_source_response")
        if "Total-Results" in response.headers:
            try:
                total = int(response.headers["Total-Results"])
            except ValueError as exc:
                raise ConnectorError("invalid_source_response") from exc
            if total != count:
                raise ConnectorError("incomplete_source_response")

    def verify_access(self) -> dict[str, bool]:
        """Prove this key can enumerate the configured library including notes.

        The key description is never returned or persisted. File access is a
        separate grant and is unnecessary for attachment metadata collection.
        """
        raw, _ = self.request("GET", "/keys/current")
        if not isinstance(raw, dict) or not isinstance(raw.get("access"), dict):
            raise ConnectorError("invalid_source_response")
        access = raw["access"]
        if self.library_type == "user":
            if isinstance(raw.get("userID"), bool) or str(raw.get("userID")) != self.library_id:
                raise ConnectorError("identity_mismatch")
            granted = access.get("user")
            if not isinstance(granted, dict) or granted.get("library") is not True or granted.get("notes") is not True:
                raise ConnectorError("insufficient_permissions")
        else:
            groups = access.get("groups")
            if not isinstance(groups, dict):
                raise ConnectorError("insufficient_permissions")
            grants = [grant for grant in (groups.get(self.library_id), groups.get("all")) if isinstance(grant, dict)]
            if (not any(grant.get("library") is True for grant in grants)
                    or any("notes" in grant and grant["notes"] is not True for grant in grants)):
                raise ConnectorError("insufficient_permissions")
        return {"library_access": True, "notes_access": True}

    def inventory(self) -> dict[str, Any]:
        """Return every item key/revision, including children and trash.

        This is an authoritative metadata inventory at one library version;
        attachments here describe Zotero objects, not acquired original files.
        A caller must verify the version again after saving all changed records.
        """
        # Recheck grants for every initial/final inventory: an API key with
        # notes disabled can expose a permissions-filtered, incomplete view.
        self.verify_access()
        raw, response = self.request("GET", "/" + self.namespace + "/items", params={
            "format": "versions", "includeTrashed": 1,
        })
        version = self._library_version(response)
        if not isinstance(raw, dict):
            raise ConnectorError("invalid_source_response")
        versions = {self._key(key): self._version(value) for key, value in raw.items()}
        if any(int(value) > int(version) for value in versions.values()):
            raise ConnectorError("source_changed_during_pagination")
        self._unpaginated(response, len(versions))
        return {
            "version": version, "object_versions": versions,
            "consistent": True, "complete": True,
            "metadata": {"provider": "zotero", "source_instance_id": INSTANCE,
                         "namespace": self.namespace, "scope": "all_item_metadata",
                         "include_trashed": True, "pages_fetched": 1,
                         "backoff_seconds": self._last_backoff_seconds,
                         "original_files_acquired": False},
        }

    def _full_snapshot(self, item: dict[str, Any], observed: str) -> dict[str, Any]:
        """Normalize every Zotero item type while retaining its source role."""
        item, redactions = redact_payload(item)
        data = item.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("itemType"), str) or not data["itemType"]:
            raise ConnectorError("invalid_source_response")
        key = self._key(item.get("key", data.get("key")))
        version = self._version(item.get("version", data.get("version")))
        if ("key" in data and self._key(data["key"]) != key) or (
            "version" in data and self._version(data["version"]) != version
        ):
            raise ConnectorError("invalid_source_response")
        library = item.get("library")
        if library is not None and (not isinstance(library, dict) or library.get("type") != self.library_type
                                    or str(library.get("id")) != self.library_id):
            raise ConnectorError("source_identity_mismatch")
        if "deleted" in data and (not isinstance(data["deleted"], (bool, int)) or data["deleted"] not in (0, 1)):
            raise ConnectorError("invalid_source_response")
        deleted = data.get("deleted") == 1
        passages = []
        if not deleted:
            for field in ("title", "abstractNote", "publicationTitle", "proceedingsTitle", "extra",
                          "note", "annotationText", "annotationComment"):
                value = data.get(field)
                if isinstance(value, str) and value:
                    passages.append({"text": value, "json_path": ["data", field], "section": field})
        return {
            "provider": "zotero", "source_instance_id": INSTANCE, "namespace": self.namespace,
            "external_id": key, "external_version": version,
            "source_url": self.origin + "/" + self.namespace + "/items/" + key,
            "raw_payload": item, "observed_at": observed,
            "occurred_at": aware_timestamp(data.get("dateModified")),
            "completeness": "partial" if redactions else "complete",
            "missing_parts": ["temporary_urls_or_credentials_redacted"] if redactions else [],
            "change": "delete" if deleted else "upsert", "passages": passages,
        }

    def fetch_objects(self, keys: list[str], expected_version: str) -> list[dict[str, Any]]:
        """Fetch specified current objects in batches of at most 50 keys.

        Missing or malformed records are failures to retry, never inferred
        deletions. The coordinator may isolate a failing key for quarantine.
        """
        expected = self._version(expected_version, configuration=True)
        if not isinstance(keys, list):
            raise ConnectorError("invalid_configuration")
        requested = [self._key(key, configuration=True) for key in keys]
        if len(set(requested)) != len(requested):
            raise ConnectorError("invalid_configuration")
        snapshots = []
        for start in range(0, len(requested), 50):
            batch_keys = requested[start:start + 50]
            raw, response = self.request("GET", "/" + self.namespace + "/items", params={
                "format": "json", "include": "data", "itemKey": ",".join(batch_keys),
                "includeTrashed": 1, "limit": 50,
            })
            self._library_version(response, expected)
            if not isinstance(raw, list):
                raise ConnectorError("invalid_source_response")
            self._unpaginated(response, len(raw))
            observed = now()
            current = {}
            for item in raw:
                if not isinstance(item, dict):
                    raise ConnectorError("invalid_source_response")
                snapshot = self._full_snapshot(item, observed)
                key = snapshot["external_id"]
                if key not in batch_keys or key in current:
                    raise ConnectorError("source_identity_mismatch")
                if int(snapshot["external_version"]) > int(expected):
                    raise ConnectorError("source_changed_during_pagination")
                current[key] = snapshot
            if set(current) != set(batch_keys):
                raise ConnectorError("objects_missing_from_response")
            snapshots.extend(current[key] for key in batch_keys)
        return snapshots

    def deleted(self, since: int, expected_version: str) -> list[dict[str, Any]]:
        """Return explicit permanent deletion observations at a library version."""
        if isinstance(since, bool) or not isinstance(since, int) or since < 0:
            raise ConnectorError("invalid_configuration")
        expected = self._version(expected_version, configuration=True)
        if since > int(expected):
            raise ConnectorError("invalid_configuration")
        raw, response = self.request("GET", "/" + self.namespace + "/deleted", params={"since": since})
        self._library_version(response, expected)
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise ConnectorError("invalid_source_response")
        keys = [self._key(key) for key in raw["items"]]
        if len(set(keys)) != len(keys):
            raise ConnectorError("invalid_source_response")
        observed = now()
        return [{
            "provider": "zotero", "source_instance_id": INSTANCE, "namespace": self.namespace,
            "external_id": key, "external_version": expected,
            "source_url": self.origin + "/" + self.namespace + "/items/" + key,
            "raw_payload": {"deleted_item_key": key, "library_version": expected},
            "observed_at": observed, "occurred_at": None, "completeness": "complete",
            "missing_parts": [], "change": "delete", "passages": [],
        } for key in keys]

    def fetch(self, *, max_items: int = 5, since: int | None = None,
              include_deleted: bool = False) -> SyncBatch:
        if not 1 <= max_items <= 100000 or (since is not None and (not isinstance(since, int) or since < 0)):
            raise ConnectorError("invalid_configuration")
        batch = SyncBatch([], "zotero", INSTANCE, self.namespace,
                          missing_parts=["nonpaper_items_and_children_not_requested"])
        start, exhausted, observed_version = 0, False, None
        while len(batch.snapshots) < max_items:
            params = {"format": "json", "include": "data", "start": start,
                      "limit": min(100, max_items - len(batch.snapshots)),
                      "itemType": "journalArticle || conferencePaper || preprint"}
            if since is not None:
                params["since"] = since
            raw, response = self.request("GET", "/" + self.namespace + "/items/top", params=params)
            batch.pages_fetched += 1
            version = response.headers.get("Last-Modified-Version")
            if version is None:
                raise ConnectorError("missing_library_version")
            if observed_version is not None and observed_version != version:
                raise ConnectorError("source_changed_during_pagination")
            observed_version = version
            if not isinstance(raw, list):
                raise ConnectorError("invalid_source_response")
            observed = now()
            for item in raw:
                if not isinstance(item, dict):
                    raise ConnectorError("invalid_source_response")
                # Enforce the selection locally even if a server ignores its filter.
                if not isinstance(item.get("data"), dict) or item["data"].get("itemType") not in PAPER_TYPES:
                    continue
                batch.snapshots.append(self._snapshot(item, observed))
                if len(batch.snapshots) >= max_items:
                    break
            start += len(raw)
            try:
                total = int(response.headers["Total-Results"]) if "Total-Results" in response.headers else None
            except ValueError as exc:
                raise ConnectorError("invalid_source_response") from exc
            has_next = 'rel="next"' in response.headers.get("Link", "")
            if not raw or (total is not None and start >= total) or (total is None and not has_next):
                exhausted = True
                break
            if batch.pages_fetched >= 10000:
                raise ConnectorError("pagination_limit")
        if not exhausted:
            batch.missing_parts.append("bounded_initial_sample")
        batch.library_version = observed_version
        if include_deleted:
            if since is None:
                raise ConnectorError("deletion_cursor_required")
            raw_deleted, response = self.request("GET", "/" + self.namespace + "/deleted", params={"since": since})
            batch.pages_fetched += 1
            if response.headers.get("Last-Modified-Version") != observed_version:
                raise ConnectorError("source_changed_during_pagination")
            if not isinstance(raw_deleted, dict) or not isinstance(raw_deleted.get("items"), list):
                raise ConnectorError("invalid_source_response")
            for key in raw_deleted["items"]:
                if not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9]{8}", key):
                    raise ConnectorError("invalid_source_response")
                batch.snapshots.append({
                    "provider": "zotero", "source_instance_id": INSTANCE, "namespace": self.namespace,
                    "external_id": key, "external_version": observed_version,
                    "source_url": self.origin + "/" + self.namespace + "/items/" + key,
                    "raw_payload": {"deleted_item_key": key, "library_version": observed_version},
                    "observed_at": now(), "occurred_at": None, "completeness": "complete",
                    "missing_parts": [], "change": "delete", "passages": [],
                })
        # This is a selected paper-metadata view, not all library items/deletions.
        # A caller must not advance the complete-library cursor using this batch.
        return batch
