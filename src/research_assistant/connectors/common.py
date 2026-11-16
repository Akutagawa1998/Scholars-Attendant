"""Connector transport and source snapshots contain no credentials or authority."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import httpx


class ConnectorError(Exception):
    """Sanitized operational error; never carries a URL, token or response body."""
    def __init__(self, code: str, *, retry_after: float | None = None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


@dataclass
class SyncBatch:
    snapshots: list[dict[str, Any]]
    provider: str
    source_instance_id: str
    namespace: str
    pages_fetched: int = 0
    library_version: str | None = None
    complete: bool = False
    cursor_candidate: str | None = None
    missing_parts: list[str] = field(default_factory=list)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def aware_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def checked_json(response: httpx.Response) -> Any:
    code = response.status_code
    if code == 403:
        raise ConnectorError("access_revoked")
    if code == 401:
        raise ConnectorError("authentication_failed")
    if code == 404:
        # Notion/Zotero cannot prove deletion from a not-found response.
        raise ConnectorError("not_found_or_inaccessible")
    if code == 429:
        try:
            retry_after = max(0.0, float(response.headers.get("Retry-After", "0")))
        except ValueError:
            retry_after = None
        raise ConnectorError("rate_limited", retry_after=retry_after)
    if 300 <= code < 400:
        raise ConnectorError("redirect_refused")
    if code >= 500:
        raise ConnectorError("source_unavailable")
    if code >= 400:
        raise ConnectorError("invalid_source_request")
    try:
        return response.json()
    except ValueError as exc:
        raise ConnectorError("invalid_source_response") from exc


class ReadOnlyHTTP:
    """Fixed-origin transport; redirects and ambient proxies are disabled."""
    origin: str

    def readonly_post(self, path: str) -> bool:
        return False

    def __init__(self, headers: dict[str, str], *, transport: httpx.BaseTransport | None = None, timeout: float = 30):
        self._client = httpx.Client(
            headers=headers, transport=transport, timeout=timeout,
            follow_redirects=False, trust_env=False,
        )

    def request(self, method: str, path: str, *, params=None, json=None) -> tuple[Any, httpx.Response]:
        if method not in {"GET", "POST"} or not path.startswith("/") or path.startswith("//"):
            raise ConnectorError("invalid_source_request")
        if method == "POST" and not self.readonly_post(path):
            raise ConnectorError("remote_write_forbidden")
        # POST is used only by a fixed query endpoint in the Notion adapter.
        try:
            response = self._client.request(method, self.origin + path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise ConnectorError("source_unavailable") from exc
        return checked_json(response), response

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def redact_payload(value: Any) -> tuple[Any, int]:
    """Copy a provider payload, removing credential fields and signed URL data.

    The result is a saved sanitized connector snapshot, not original HTTP bytes.
    Embedded Markdown/XML/HTML URLs are checked as well as standalone strings.
    """
    import re
    import html
    from urllib.parse import parse_qsl, urlsplit, urlunsplit
    from ..source_privacy import CREDENTIAL_KEYS as credential_keys, SIGNED_QUERY_KEYS as signed_keys, URL_PATTERN as url_pattern
    count = 0

    def redact_url(match):
        nonlocal count
        raw = match.group(0)
        decoded = html.unescape(raw)
        try:
            parsed = urlsplit(decoded)
            sensitive = parsed.username is not None or parsed.password is not None or any(
                key.lower().replace("-", "_") in signed_keys for key, _ in parse_qsl(parsed.query)
            )
            if not sensitive:
                return raw
            host = parsed.hostname or "redacted.invalid"
            if ":" in host:
                host = "[" + host + "]"
            if parsed.port is not None:
                host += ":" + str(parsed.port)
            count += 1
            return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except ValueError:
            count += 1
            return "https://redacted.invalid/invalid-source-url"

    def walk(node):
        nonlocal count
        if isinstance(node, dict):
            result = {}
            for key, child in node.items():
                if str(key).lower().replace("-", "_") in credential_keys:
                    count += 1
                    continue
                result[key] = walk(child)
            return result
        if isinstance(node, list):
            return [walk(child) for child in node]
        if isinstance(node, str):
            return url_pattern.sub(redact_url, node)
        return node

    cleaned = walk(value)
    return cleaned, count
