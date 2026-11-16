"""Authenticated HTTP boundary; all research rules remain in ResearchService."""

from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse
from research_assistant import __version__

from research_assistant.api.operations import OPERATIONS, error_result


def create_app(
    service: Any = None,
    *,
    database_url: str | None = None,
    owner_id: str | None = None,
    scope_id: str | None = None,
    api_token: str | None = None,
    capture_token: str | None = None,
) -> FastAPI:
    """Build one fixed-owner service; callers cannot choose their principal.

    RA_CAPTURE_TOKEN belongs only to a trusted human-input adapter. Never pass
    it to an LLM client or expose capture as a normal MCP tool.
    """
    from research_assistant.service import DomainError, ResearchService

    api_secret = os.environ.get("RA_API_TOKEN", "") if api_token is None else api_token
    capture_secret = (
        os.environ.get("RA_CAPTURE_TOKEN", "") if capture_token is None else capture_token
    )
    if not api_secret or not api_secret.strip():
        raise ValueError("RA_API_TOKEN is required to start the HTTP service")
    if capture_secret and secrets.compare_digest(
        api_secret.encode("utf-8"), capture_secret.encode("utf-8")
    ):
        raise ValueError("RA_CAPTURE_TOKEN must differ from RA_API_TOKEN")
    if service is None:
        db = database_url or os.environ.get("RA_DATABASE_URL")
        if not db:
            raise ValueError("RA_DATABASE_URL or database_url is required")
        service = ResearchService(
            db,
            owner_id=owner_id or os.environ.get("RA_OWNER_ID", "local"),
            scope_id=scope_id or os.environ.get("RA_SCOPE_ID", "private"),
        )
        service.initialize()

    app = FastAPI(title="Scholars-Attendant research memory", version=__version__)
    app.state.research_service = service

    @app.middleware("http")
    async def authentication(request: Request, call_next):
        if request.url.path in {"/health", "/healthz"}:
            return await call_next(request)
        secret = capture_secret if request.url.path == "/capture" else api_secret
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        valid = (
            bool(secret)
            and scheme.lower() == "bearer"
            and secrets.compare_digest(supplied.encode("utf-8"), secret.encode("utf-8"))
        )
        if not valid:
            return JSONResponse(
                error_result("unauthorized", "A valid bearer token is required", "forbidden"),
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError):
        status = exc.status if isinstance(exc.status, str) else "failed"
        http_status = (
            exc.status
            if isinstance(exc.status, int)
            else {
                "forbidden": 403,
                "not_found": 404,
                "conflict": 409,
                "unavailable": 503,
            }.get(status, 422)
        )
        return JSONResponse(error_result(exc.code, exc.message, status), status_code=http_status)

    @app.get("/health")
    @app.get("/healthz", include_in_schema=False)
    def health() -> dict:
        # No DB credentials, owner IDs or record counts on this public endpoint.
        return {"status": "ok", "service": "scholars-attendant"}

    @app.post("/v1/operations/{operation}")
    def execute(operation: str, payload: dict = Body(...)) -> Any:
        if operation not in OPERATIONS:
            return JSONResponse(
                error_result("unknown_operation", "Operation is not exposed", "not_found"),
                status_code=404,
            )
        return run_operation(operation, payload, trusted_capture=False)

    def run_operation(operation: str, payload: dict, *, trusted_capture: bool) -> Any:
        try:
            return service.execute(operation, payload, trusted_capture=trusted_capture)
        except DomainError:
            raise
        except Exception:
            # Database exceptions can include bound private values. Do not let
            # their repr enter an HTTP response or uvicorn's traceback logs.
            return JSONResponse(
                error_result("service_unavailable", "The domain service is unavailable", "unavailable"),
                status_code=503,
            )

    @app.post("/capture")
    def capture(payload: dict = Body(...)) -> Any:
        return run_operation("capture.message", payload, trusted_capture=True)

    return app
