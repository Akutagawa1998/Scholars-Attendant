"""Shared HTTP client used by CLI and MCP, with no private local memory copy."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

import httpx

from research_assistant.api.operations import OPERATIONS, error_result


def validate_service_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Service URL must be an HTTP(S) URL without credentials, query or fragment")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            raise ValueError("Non-loopback service URLs require HTTPS")
    return value.rstrip("/")


class ServiceClient:
    def __init__(self, service_url: str, token: str, *, timeout: float = 30):
        self.service_url = validate_service_url(service_url)
        if not token or not token.strip():
            raise ValueError("A service token is required")
        self._token = token
        self._timeout = timeout

    def execute(self, operation: str, payload: dict, *, trusted_capture: bool = False) -> dict:
        if trusted_capture:
            if operation != "capture.message":
                raise ValueError("Trusted capture is restricted to capture.message")
            path = "/capture"
        else:
            if operation not in OPERATIONS:
                return error_result("unknown_operation", "Operation is not exposed", "not_found")
            path = f"/v1/operations/{operation}"
        try:
            # Never follow a redirect with the service bearer token. Do not retry
            # writes after a timeout: the transaction may already have committed.
            with httpx.Client(
                timeout=self._timeout, follow_redirects=False, trust_env=False
            ) as client:
                response = client.post(
                    self.service_url + path,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            result = response.json()
            if not isinstance(result, dict) or "status" not in result:
                return error_result("invalid_service_response", "Service returned an invalid response")
            return result
        except (httpx.HTTPError, ValueError):
            # Do not echo request payloads, credentials or server debugging text.
            return error_result(
                "service_unavailable",
                "Service request failed; a write may have committed. Retry with the same idempotency key.",
                "unavailable",
            )
