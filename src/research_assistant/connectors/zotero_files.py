"""Bounded in-memory acquisition of authorized Zotero PDF attachment bytes.

Only the configured Zotero library's metadata and file endpoint are requested.
Storage redirects are manual and narrowly allowlisted; API credentials never
leave api.zotero.org. This module neither persists bytes nor returns temporary
storage URLs. It does not fetch a publisher URL or a local linked-file path.

Protocol references:
https://www.zotero.org/support/dev/web_api/v3/syncing#verify_key_access
https://www.zotero.org/support/dev/web_api/v3/file_upload#download_the_existing_file
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import ipaddress
import json
import math
import re
import time
from typing import Any, Literal
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from ..assets import MAX_PDF_BYTES
from .common import ConnectorError

API_ORIGIN = "https://api.zotero.org"
_METADATA_BYTES = 2 * 1024 * 1024
_STORAGE_HOSTS = frozenset({
    "files.zotero.net",
    "zoterofilestorage.s3.amazonaws.com",
    "zoterofilestorage.s3.us-east-1.amazonaws.com",
})
_PATH_STYLE_STORAGE_HOSTS = frozenset({"s3.amazonaws.com", "s3.us-east-1.amazonaws.com"})
_KEY = re.compile(r"[A-Z0-9]{8}\Z")
_MD5 = re.compile(r"[a-fA-F0-9]{32}\Z")


@dataclass(frozen=True)
class PdfFetchResult:
    status: Literal["ok", "unavailable"]
    data: bytes | None = field(default=None, repr=False)
    sha256: str | None = None
    md5: str | None = None
    byte_length: int = 0
    provenance: dict[str, Any] = field(default_factory=dict)
    provenance_sha256: str | None = None
    error_code: str | None = None
    retry_after: float | None = None


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _retry_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            when = parsedate_to_datetime(value)
            if when.utcoffset() is None:
                return None
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return min(3600.0, max(0.0, seconds))


def _storage_url(value: str) -> str:
    """Validate a redirect destination without echoing its signed URL."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 443}
                or parsed.fragment or "\\" in value or any(ord(c) < 32 for c in value)):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        allowed = host in _STORAGE_HOSTS
        if host in _PATH_STYLE_STORAGE_HOSTS:
            # Path-style S3 URLs are restricted to the Zotero storage bucket.
            allowed = parsed.path.startswith("/zoterofilestorage/")
        if not allowed:
            raise ValueError
        decoded_path = parsed.path
        for _ in range(3):
            decoded_path = unquote(decoded_path)
            if "\\" in decoded_path or any(part in {".", ".."} for part in decoded_path.split("/")) or any(ord(c) < 32 for c in decoded_path):
                raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise ConnectorError("unsafe_storage_redirect") from None


def _md5(value: Any, *, etag: bool = False) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConnectorError("invalid_file_metadata")
    if etag:
        value = value.strip().strip('"')
    if not _MD5.fullmatch(value):
        raise ConnectorError("file_etag_unverifiable" if etag else "invalid_file_metadata")
    return value.lower()


def _length(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConnectorError("invalid_file_length")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        try:
            result = int(value)
        except ValueError:
            raise ConnectorError("invalid_file_length") from None
    else:
        raise ConnectorError("invalid_file_length")
    if result < 0:
        raise ConnectorError("invalid_file_length")
    return result


def _pdf_type(value: Any) -> bool:
    return isinstance(value, str) and value.lower().split(";", 1)[0].strip() == "application/pdf"


class ZoteroFileClient:
    """Read-only PDF byte fetcher; constructor accepts no arbitrary endpoint."""
    def __init__(self, api_key: str, library_id: str, *, library_type: str = "user",
                 transport: httpx.BaseTransport | None = None, timeout: float = 30,
                 max_bytes: int = MAX_PDF_BYTES, max_redirects: int = 3):
        if (not isinstance(api_key, str) or not api_key or library_type not in {"user", "group"}
                or not re.fullmatch(r"[0-9]+", str(library_id))
                or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 10 <= timeout <= 60
                or isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_PDF_BYTES
                or isinstance(max_redirects, bool) or not isinstance(max_redirects, int) or not 0 <= max_redirects <= 5):
            raise ConnectorError("invalid_configuration")
        self.library_id = str(library_id)
        self.library_type = library_type
        self.namespace = ("users/" if library_type == "user" else "groups/") + self.library_id
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self._api_key = api_key
        self._not_before = 0.0
        self.request_counts = {"metadata": 0, "file": 0, "storage": 0}
        # Authentication is per request; no credential defaults can leak to S3.
        self._client = httpx.Client(transport=transport, timeout=float(timeout),
                                    follow_redirects=False, trust_env=False)

    def __repr__(self):
        return "ZoteroFileClient(<private>)"

    def close(self):
        self._client.close()
        self._api_key = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _headers(self, *, api: bool, pdf: bool = False) -> dict[str, str]:
        result = {"Accept": "application/pdf" if pdf else "application/json", "Accept-Encoding": "identity"}
        if api:
            result.update({"Zotero-API-Key": self._api_key, "Zotero-API-Version": "3"})
        return result

    def _check_backoff(self):
        remaining = self._not_before - time.monotonic()
        if remaining > 0:
            raise ConnectorError("source_backoff", retry_after=remaining)

    def _status(self, response: httpx.Response):
        backoff = _retry_seconds(response.headers.get("Backoff"))
        if backoff:
            self._not_before = max(self._not_before, time.monotonic() + backoff)
        if response.status_code == 429:
            wait = _retry_seconds(response.headers.get("Retry-After"))
            if wait is not None:
                self._not_before = max(self._not_before, time.monotonic() + wait)
            raise ConnectorError("rate_limited", retry_after=wait)
        if response.status_code == 401:
            raise ConnectorError("authentication_failed")
        if response.status_code == 403:
            raise ConnectorError("access_revoked")
        if response.status_code == 404:
            raise ConnectorError("not_found_or_inaccessible")
        if response.status_code >= 500:
            raise ConnectorError("source_unavailable", retry_after=_retry_seconds(response.headers.get("Retry-After")))
        if response.status_code >= 400:
            raise ConnectorError("invalid_source_request")

    def _api_json(self, path: str) -> tuple[dict[str, Any], httpx.Headers]:
        self._check_backoff()
        self.request_counts["metadata"] += 1
        try:
            with self._client.stream("GET", API_ORIGIN + path, headers=self._headers(api=True)) as response:
                self._status(response)
                if response.status_code != 200:
                    raise ConnectorError("redirect_refused" if 300 <= response.status_code < 400 else "invalid_source_response")
                body = self._read(response, _METADATA_BYTES, pdf=False)
                headers = response.headers
            raw = json.loads(body)
        except httpx.HTTPError:
            raise ConnectorError("source_unavailable") from None
        except (ValueError, UnicodeDecodeError):
            raise ConnectorError("invalid_source_response") from None
        if not isinstance(raw, dict):
            raise ConnectorError("invalid_source_response")
        return raw, headers

    @staticmethod
    def _read(response: httpx.Response, limit: int, *, pdf: bool) -> bytes:
        expected = _length(response.headers.get("Content-Length"))
        if expected is not None and expected > limit:
            raise ConnectorError("pdf_too_large" if pdf else "metadata_too_large")
        if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise ConnectorError("unsupported_content_encoding")
        buffer = bytearray()
        for chunk in response.iter_bytes(chunk_size=65536):
            if len(buffer) + len(chunk) > limit:
                raise ConnectorError("pdf_too_large" if pdf else "metadata_too_large")
            buffer.extend(chunk)
            if pdf and len(buffer) >= 5 and not buffer.startswith(b"%PDF-"):
                raise ConnectorError("invalid_pdf_header")
        if expected is not None and len(buffer) != expected:
            raise ConnectorError("file_length_mismatch")
        if pdf and not buffer.startswith(b"%PDF-"):
            raise ConnectorError("invalid_pdf_header")
        return bytes(buffer)

    def verify_access(self) -> dict[str, bool]:
        raw, _ = self._api_json("/keys/current")
        access = raw.get("access")
        if not isinstance(access, dict):
            raise ConnectorError("invalid_source_response")
        if self.library_type == "user":
            if isinstance(raw.get("userID"), bool) or str(raw.get("userID")) != self.library_id:
                raise ConnectorError("identity_mismatch")
            grant = access.get("user")
        else:
            groups = access.get("groups")
            if not isinstance(groups, dict):
                raise ConnectorError("file_access_not_granted")
            # Unknown/implicit file permission is not enough for acquisition.
            grant = groups.get(self.library_id, groups.get("all"))
        if not isinstance(grant, dict) or grant.get("library") is not True:
            raise ConnectorError("insufficient_permissions")
        if grant.get("files") is not True:
            raise ConnectorError("file_access_not_granted")
        return {"library": True, "notes": grant.get("notes") is True, "files": True}

    def _attachment_metadata(self, attachment_key: str):
        if not isinstance(attachment_key, str) or not _KEY.fullmatch(attachment_key):
            raise ConnectorError("invalid_attachment_key")
        provenance: dict[str, Any] = {"provider": "zotero", "source_instance_id": "api.zotero.org:v3",
            "namespace": self.namespace, "attachment_key": attachment_key}
        self.verify_access()
        raw, headers = self._api_json("/" + self.namespace + "/items/" + attachment_key)
        data = raw.get("data")
        if not isinstance(data, dict) or raw.get("key", attachment_key) != attachment_key or data.get("key", attachment_key) != attachment_key:
            raise ConnectorError("invalid_file_metadata")
        if (data.get("itemType") != "attachment" or not _pdf_type(data.get("contentType"))
                or data.get("linkMode") not in {"imported_file", "imported_url"}):
            raise ConnectorError("unsupported_attachment")
        library = raw.get("library")
        if library is not None and (not isinstance(library, dict) or str(library.get("id")) != self.library_id or library.get("type") != self.library_type):
            raise ConnectorError("identity_mismatch")
        version = raw.get("version", data.get("version"))
        if (isinstance(version, bool) or not isinstance(version, int) or version < 0
                or ("version" in data and (isinstance(data["version"], bool) or data["version"] != version))
                or ("Last-Modified-Version" in headers and headers["Last-Modified-Version"] != str(version))):
            raise ConnectorError("invalid_file_metadata")
        parent = data.get("parentItem")
        if parent is not None and parent is not False and (not isinstance(parent, str) or not _KEY.fullmatch(parent)):
            raise ConnectorError("invalid_file_metadata")
        expected_md5 = _md5(data.get("md5"))
        expected_length = _length(data.get("filesize"))
        enclosure = raw.get("links", {}).get("enclosure", {}) if isinstance(raw.get("links", {}), dict) else {}
        if isinstance(enclosure, dict) and enclosure.get("length") is not None:
            enclosure_length = _length(enclosure["length"])
            if expected_length is not None and enclosure_length != expected_length:
                raise ConnectorError("invalid_file_length")
            expected_length = enclosure_length
        if expected_length is not None and expected_length > self.max_bytes:
            raise ConnectorError("pdf_too_large")
        provenance.update(attachment_version=str(version), parent_item_key=parent or None,
            link_mode=data["linkMode"], content_type="application/pdf", metadata_sha256=_canonical_hash(raw),
            metadata_md5=expected_md5)
        provenance["expected_byte_length"] = expected_length
        return data, version, provenance, expected_length

    def attachment_metadata(self, attachment_key: str) -> dict[str, Any]:
        """Fresh file grant + attachment metadata, without a file GET.

        Used for idempotent acquisition runs: compare this attachment revision
        and metadata_md5 to the canonical source head and already verified blob.
        No raw metadata, remote filename, credential or URL is returned.
        """
        _, _, provenance, _ = self._attachment_metadata(attachment_key)
        return provenance

    def fetch_pdf(self, attachment_key: str) -> PdfFetchResult:
        if not isinstance(attachment_key, str) or not _KEY.fullmatch(attachment_key):
            raise ConnectorError("invalid_attachment_key")
        endpoint = API_ORIGIN + "/" + self.namespace + "/items/" + attachment_key + "/file"
        provenance: dict[str, Any] = {"provider": "zotero", "source_instance_id": "api.zotero.org:v3",
            "namespace": self.namespace, "attachment_key": attachment_key}
        try:
            data, version, provenance, expected_length = self._attachment_metadata(attachment_key)
            expected_md5 = provenance["metadata_md5"]
            payload, remote_hashes = self._file(endpoint)
            actual_md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
            if expected_length is not None and expected_length != len(payload):
                raise ConnectorError("file_length_mismatch")
            if (expected_md5 is not None and actual_md5 != expected_md5) or any(actual_md5 != value for value in remote_hashes):
                raise ConnectorError("file_hash_mismatch")
            # Bind bytes to a still-current attachment revision. The caller must
            # additionally compare this revision/MD5 with its canonical DB head.
            final_raw, final_headers = self._api_json("/" + self.namespace + "/items/" + attachment_key)
            final_data = final_raw.get("data")
            if (not isinstance(final_data, dict)
                    or final_raw.get("key", attachment_key) != attachment_key
                    or final_data.get("key", attachment_key) != attachment_key
                    or final_raw.get("version", final_data.get("version")) != version
                    or ("version" in final_data and (isinstance(final_data["version"], bool) or final_data["version"] != version))
                    or ("Last-Modified-Version" in final_headers and final_headers["Last-Modified-Version"] != str(version))
                    or _md5(final_data.get("md5")) != expected_md5
                    or not _pdf_type(final_data.get("contentType"))
                    or final_data.get("linkMode") != data["linkMode"]
                    or final_data.get("parentItem") != data.get("parentItem")):
                raise ConnectorError("attachment_changed_during_download")
            final_length = _length(final_data.get("filesize"))
            if final_length is not None and final_length != len(payload):
                raise ConnectorError("attachment_changed_during_download")
            provenance["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            provenance["metadata_md5_verified"] = expected_md5 is not None
            provenance["response_md5_verified"] = bool(remote_hashes)
            return PdfFetchResult(status="ok", data=payload, sha256=hashlib.sha256(payload).hexdigest(),
                md5=actual_md5, byte_length=len(payload), provenance=provenance,
                provenance_sha256=_canonical_hash(provenance))
        except ConnectorError as exc:
            if exc.code not in {"not_found_or_inaccessible", "source_unavailable", "rate_limited", "source_backoff"}:
                raise
            return PdfFetchResult(status="unavailable", provenance=provenance,
                provenance_sha256=_canonical_hash(provenance), error_code=exc.code, retry_after=exc.retry_after)
        except httpx.HTTPError:
            return PdfFetchResult(status="unavailable", provenance=provenance,
                provenance_sha256=_canonical_hash(provenance), error_code="source_unavailable")

    def _file(self, endpoint: str) -> tuple[bytes, list[str]]:
        url, seen, hashes = endpoint, set(), []
        for redirect_count in range(self.max_redirects + 1):
            self._check_backoff()
            if url in seen:
                raise ConnectorError("storage_redirect_loop")
            seen.add(url)
            # API authentication is allowed only on the original file request.
            api_request = redirect_count == 0
            self.request_counts["file" if api_request else "storage"] += 1
            with self._client.stream("GET", url, headers=self._headers(api=api_request, pdf=True)) as response:
                self._status(response)
                if response.headers.get("Zotero-File-Compressed", "").lower() == "yes":
                    raise ConnectorError("compressed_attachment_unsupported")
                provided_hash = _md5(response.headers.get("Zotero-File-MD5"))
                if provided_hash:
                    hashes.append(provided_hash)
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count >= self.max_redirects:
                        raise ConnectorError("too_many_storage_redirects")
                    location = response.headers.get("Location")
                    if not location:
                        raise ConnectorError("unsafe_storage_redirect")
                    url = _storage_url(urljoin(url, location))
                    continue
                if response.status_code != 200:
                    raise ConnectorError("invalid_source_response")
                media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if media not in {"", "application/pdf", "application/octet-stream"}:
                    raise ConnectorError("invalid_pdf_media_type")
                etag = _md5(response.headers.get("ETag"), etag=True)
                if etag:
                    hashes.append(etag)
                return self._read(response, self.max_bytes, pdf=True), hashes
        raise ConnectorError("too_many_storage_redirects")
