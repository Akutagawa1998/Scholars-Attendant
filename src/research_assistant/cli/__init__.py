"""Operator CLI and shared-service client."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from research_assistant.api.operations import OPERATIONS, error_result
from research_assistant import __version__


def _payload(filename: str) -> dict:
    raw = sys.stdin.read() if filename == "-" else Path(filename).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("The JSON request must be an object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-assistant")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--database-url", default=os.environ.get("RA_DATABASE_URL"))
    parser.add_argument("--service-url", default=os.environ.get("RA_SERVICE_URL"))
    parser.add_argument("--runtime-config", default=os.environ.get("RA_RUNTIME_CONFIG"), help="Explicit private database/service configuration JSON")
    parser.add_argument("--owner-id", default=os.environ.get("RA_OWNER_ID", "local"))
    parser.add_argument("--scope-id", default=os.environ.get("RA_SCOPE_ID", "private"))
    subcommands = parser.add_subparsers(dest="command", required=True)
    call = subcommands.add_parser("call", help="Call a domain operation locally or through HTTP")
    call.add_argument("operation", choices=sorted(OPERATIONS))
    call.add_argument("--json", default="-", metavar="FILE|-", help="JSON object, stdin by default")
    capture = subcommands.add_parser(
        "capture", help="Trusted operator capture; never invoke on behalf of an LLM"
    )
    capture.add_argument("--json", default="-", metavar="FILE|-")
    subcommands.add_parser("init", help="Initialize the explicitly configured local database")
    serve = subcommands.add_parser("serve", help="Start the loopback HTTP service")
    serve.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "::1", "localhost"])
    serve.add_argument("--port", default=8765, type=int)
    subcommands.add_parser("mcp", help="Run stdio MCP, proxying RA_SERVICE_URL")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        runtime = {}
        if args.runtime_config and not args.service_url and args.command != "mcp":
            from research_assistant.settings import read_runtime
            runtime = read_runtime(args.runtime_config)
            args.database_url = args.database_url or runtime["database_url"]
            if not os.environ.get("RA_OWNER_ID") and args.owner_id == "local":
                args.owner_id = runtime.get("owner_id", "local")
            if not os.environ.get("RA_SCOPE_ID") and args.scope_id == "private":
                args.scope_id = runtime.get("scope_id", "private")
        if args.command == "mcp":
            from research_assistant.api.client import ServiceClient
            from research_assistant.api.mcp import create_mcp

            if not args.service_url:
                raise ValueError("--service-url or RA_SERVICE_URL is required for MCP")
            create_mcp(ServiceClient(args.service_url, os.environ.get("RA_API_TOKEN", ""))).run(
                transport="stdio"
            )
            return 0

        if args.command == "serve":
            import uvicorn
            from research_assistant.api.http import create_app

            app = create_app(
                database_url=args.database_url, owner_id=args.owner_id, scope_id=args.scope_id,
                api_token=os.environ.get("RA_API_TOKEN") or runtime.get("api_token"),
                capture_token=os.environ.get("RA_CAPTURE_TOKEN") or runtime.get("capture_token"),
            )
            uvicorn.run(app, host=args.host, port=args.port)
            return 0

        if args.command == "init" and args.service_url:
            raise ValueError("init requires a local --database-url and no --service-url")
        if args.service_url:
            from research_assistant.api.client import ServiceClient

            token_name = "RA_CAPTURE_TOKEN" if args.command == "capture" else "RA_API_TOKEN"
            service = ServiceClient(args.service_url, os.environ.get(token_name, ""))
        else:
            from research_assistant.service import ResearchService

            if not args.database_url:
                raise ValueError("--database-url or RA_DATABASE_URL is required for local access")
            service = ResearchService(
                args.database_url, owner_id=args.owner_id, scope_id=args.scope_id
            )
            service.initialize()
        if args.command == "init":
            result = {"status": "ok", "message": "Database initialized"}
        else:
            operation = "capture.message" if args.command == "capture" else args.operation
            result = service.execute(
                operation, _payload(args.json), trusted_capture=args.command == "capture"
            )
    except (OSError, ValueError) as exc:
        # JSON parse failures report line/column only; never echo the request body.
        message = str(exc) if not isinstance(exc, json.JSONDecodeError) else "Invalid JSON request"
        result = error_result("invalid_request", message)
    except Exception as exc:
        from research_assistant.service import DomainError

        if not isinstance(exc, DomainError):
            result = error_result(
                "service_unavailable", "The domain service is unavailable", "unavailable"
            )
        else:
            result = error_result(
                exc.code, exc.message, exc.status if isinstance(exc.status, str) else "failed"
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"ok", "partial"} else 1
