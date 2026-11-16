"""Transport authorization tests; no external services or user records."""

from __future__ import annotations

import asyncio
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from research_assistant.api.client import ServiceClient, validate_service_url
from research_assistant.api.http import create_app
from research_assistant.api.mcp import create_mcp
from research_assistant.cli import main


class StubService:
    def __init__(self):
        self.calls = []

    def execute(self, operation, payload, *, trusted_capture=False):
        self.calls.append((operation, payload, trusted_capture))
        return {"status": "ok", "data": {"operation": operation}}


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.service = StubService()
        self.client = TestClient(
            create_app(self.service, api_token="model-token", capture_token="operator-token")
        )

    def test_authentication_precedes_body_processing(self):
        result = self.client.post("/v1/operations/memory.recall", content="invalid JSON")
        self.assertEqual(result.status_code, 401)
        self.assertEqual(self.service.calls, [])

    def test_model_token_cannot_capture_or_select_capture_operation(self):
        headers = {"Authorization": "Bearer model-token"}
        self.assertEqual(self.client.post("/capture", json={}, headers=headers).status_code, 401)
        response = self.client.post(
            "/v1/operations/capture.message", json={}, headers=headers
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.service.calls, [])

    def test_operator_capture_uses_separate_credential(self):
        response = self.client.post(
            "/capture", json={"message_id": "synthetic"},
            headers={"Authorization": "Bearer operator-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls, [("capture.message", {"message_id": "synthetic"}, True)])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_generic_route_never_promotes_user_claims(self):
        payload = {"speaker": "user", "trusted_capture": True}
        response = self.client.post(
            "/v1/operations/memory.propose", json=payload,
            headers={"Authorization": "Bearer model-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls, [("memory.propose", payload, False)])

    def test_health_discloses_no_domain_data(self):
        result = self.client.get("/health").json()
        self.assertEqual(result, {"status": "ok", "service": "scholars-attendant"})

    def test_database_errors_do_not_echo_bound_values(self):
        self.service.execute = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("SQL parameters contained private synthetic content")
        )
        response = self.client.post(
            "/v1/operations/memory.recall", json={},
            headers={"Authorization": "Bearer model-token"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "service_unavailable")
        self.assertNotIn("private synthetic content", response.text)

    def test_fail_closed_without_token_or_with_same_capture_token(self):
        with self.assertRaises(ValueError):
            create_app(self.service, api_token="", capture_token="")
        with self.assertRaises(ValueError):
            create_app(self.service, api_token="same", capture_token="same")


class SharedClientTests(unittest.TestCase):
    def test_non_loopback_plaintext_and_embedded_secrets_rejected(self):
        for url in (
            "http://example.invalid:8765", "http://user:token@localhost:8765",
            "http://localhost:8765?token=secret", "http://localhost:8765/#secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_service_url(url)
        self.assertEqual(validate_service_url("http://127.0.0.1:8765/"), "http://127.0.0.1:8765")

    def test_mcp_exposes_typed_operations_but_no_capture(self):
        server = create_mcp(StubService())
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        self.assertIn("memory_recall", names)
        self.assertNotIn("capture_message", names)
        tool = next(item for item in tools if item.name == "papers_mark_read")
        self.assertIn("payload", tool.inputSchema["properties"])
        self.assertIn("trusted_message_id", json.dumps(tool.inputSchema))

    def test_public_allowlist_matches_domain_operations(self):
        from research_assistant.api.operations import OPERATIONS
        from research_assistant.schemas import OPERATIONS as DOMAIN_OPERATIONS, OPERATOR_OPERATIONS

        self.assertEqual(OPERATIONS, set(DOMAIN_OPERATIONS) - OPERATOR_OPERATIONS)

    def test_mcp_does_not_fall_back_to_a_local_database(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
            create_mcp()

    def test_http_client_rejects_capture_on_generic_interface(self):
        client = ServiceClient("http://localhost:8765", "model-token")
        result = client.execute("capture.message", {})
        self.assertEqual(result["error_code"], "unknown_operation")

    def test_cli_rejects_non_object_without_echoing_input(self):
        with patch.dict("os.environ", {"RA_API_TOKEN": "model-token"}, clear=True), patch(
            "sys.stdin", StringIO('["private synthetic string"]')
        ), patch("sys.stdout", new_callable=StringIO) as output:
            exit_code = main(["--service-url", "http://localhost:8765", "call", "memory.recall"])
        self.assertEqual(exit_code, 1)
        self.assertNotIn("private synthetic string", output.getvalue())


class StdioIntegrationTests(unittest.TestCase):
    def test_sdk_client_reaches_shared_http_service(self):
        """Exercise MCP framing and typed dispatch against a real local HTTP process."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        source_path = str(Path(__file__).resolve().parents[2])
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        service_url = f"http://127.0.0.1:{port}"
        with tempfile.TemporaryDirectory(prefix="research-transport-test-") as directory:
            environment = dict(os.environ)
            environment.update(
                PYTHONPATH=source_path,
                RA_DATABASE_URL=f"sqlite:///{directory}/test.db",
                RA_API_TOKEN="synthetic-model-token",
                RA_CAPTURE_TOKEN="synthetic-operator-token",
                RA_OWNER_ID="transport-fixture",
                RA_SCOPE_ID="synthetic",
            )
            environment.pop("RA_SERVICE_URL", None)
            process = subprocess.Popen(
                [sys.executable, "-m", "research_assistant.cli", "serve", "--port", str(port)],
                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail("Synthetic HTTP service exited before becoming ready")
                    try:
                        if httpx.get(service_url + "/health", timeout=0.2, trust_env=False).is_success:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                else:
                    self.fail("Synthetic HTTP service did not become ready")

                statement = "My synthetic research priority is reliable memory."
                model_headers = {"Authorization": "Bearer synthetic-model-token"}
                with httpx.Client(base_url=service_url, trust_env=False) as client:
                    capture_response = client.post(
                        "/capture",
                        headers={"Authorization": "Bearer synthetic-operator-token"},
                        json={
                            "idempotency_key": "capture-transport-priority",
                            "message_id": "synthetic-priority-message",
                            "session_id": "synthetic-operator-session",
                            "text": statement,
                            "claims": [{
                                "subject_id": "synthetic-profile",
                                "predicate": "research_priority",
                                "value": {"priority": "reliable memory"},
                                "quote": statement,
                            }],
                        },
                    )
                    self.assertEqual(capture_response.status_code, 200)
                    capture = capture_response.json()["data"]
                    record_response = client.post(
                        "/v1/operations/memory.record_user_statement",
                        headers=model_headers,
                        json={
                            "idempotency_key": "record-transport-priority",
                            "trusted_message_id": capture["trusted_message_id"],
                        },
                    )
                    self.assertEqual(record_response.status_code, 200)
                    memory_id = record_response.json()["data"]["memory_id"]
                    exported = client.post(
                        "/v1/operations/export", json={}, headers=model_headers
                    ).json()["data"]
                original_evidence = next(
                    span for span in exported["evidence_spans"]
                    if span["source_version_id"] == capture["source_snapshot_id"]
                )
                self.assertEqual(original_evidence["text"], statement)
                evidence_id = original_evidence["id"]

                def assert_shared_memory(response):
                    self.assertEqual(response["status"], "ok")
                    fact = next(
                        item for item in response["data"]["reported_facts"]
                        if item["memory_id"] == memory_id
                    )
                    self.assertEqual(fact["value"], {"priority": "reliable memory"})
                    self.assertEqual(fact["epistemic_status"], "user_reported")
                    self.assertEqual(fact["evidence_ids"], [evidence_id])
                    citation = next(
                        item for item in response["citations"] if item["evidence_id"] == evidence_id
                    )
                    self.assertEqual(citation["source_snapshot_id"], capture["source_snapshot_id"])
                    self.assertEqual(citation["source_kind"], "user_capture")
                    self.assertEqual(
                        citation["locator"]["quote_hash"],
                        hashlib.sha256(statement.encode("utf-8")).hexdigest(),
                    )

                async def exercise():
                    parameters = StdioServerParameters(
                        command=sys.executable,
                        args=["-m", "research_assistant.api.mcp"],
                        env={
                            "PYTHONPATH": source_path,
                            "RA_SERVICE_URL": service_url,
                            "RA_API_TOKEN": "synthetic-model-token",
                        },
                    )
                    async with stdio_client(parameters) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            self.assertIn("memory_recall", {tool.name for tool in tools.tools})
                            result = await session.call_tool("memory_recall", {"payload": {}})
                            self.assertFalse(result.isError)
                            assert_shared_memory(result.structuredContent)

                asyncio.run(exercise())

                cli_environment = dict(environment)
                cli_environment.pop("RA_DATABASE_URL", None)
                cli_path = Path(sys.executable).with_name("research-assistant")
                self.assertTrue(cli_path.is_file(), "The installed CLI entrypoint must exist")
                cli_response = subprocess.run(
                    [str(cli_path), "--service-url", service_url, "call", "memory.recall"],
                    input="{}", text=True, capture_output=True, timeout=15, env=cli_environment,
                )
                self.assertEqual(cli_response.returncode, 0)
                assert_shared_memory(json.loads(cli_response.stdout))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
