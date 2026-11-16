#!/usr/bin/env python3
"""Import a private Notion connected-tool bundle, with no remote writes.

This is an explicit connector bridge, not an autonomous token-backed poller.
The bundle and production database configuration must remain outside Git.
Default imports are bounded; --full-bundle records durable traversal coverage
without claiming an atomic Notion version or advancing a source checkpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import UUID

from research_assistant.connectors import ConnectorError
from research_assistant.connectors.bundle import NotionBundleAdapter
from research_assistant.connectors.notion import normalize_mcp_page
from research_assistant.service import DomainError, ResearchService


def object_file(path):
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConnectorError("configuration_unavailable") from exc
    if not isinstance(result, dict):
        raise ConnectorError("invalid_configuration")
    return result


def configured_service(runtime_path):
    runtime = object_file(runtime_path) if not os.environ.get("RA_DATABASE_URL") else {}
    database_url = os.environ.get("RA_DATABASE_URL") or runtime.get("database_url")
    if not isinstance(database_url, str) or not database_url.startswith("postgresql+psycopg:"):
        raise ConnectorError("postgresql_configuration_required")
    service = ResearchService(database_url,
        owner_id=os.environ.get("RA_OWNER_ID") or runtime.get("owner_id", "local"),
        scope_id=os.environ.get("RA_SCOPE_ID") or runtime.get("scope_id", "private"))
    service.initialize()
    return service


def register_source(service, registration):
    registration_hash = hashlib.sha256(json.dumps(registration, sort_keys=True).encode()).hexdigest()
    service.execute("sources.register", {**registration, "idempotency_key": "notion-register:" + registration_hash}, trusted_capture=True)


def coordinator_summary(result):
    status, data = result.get("status"), result.get("data")
    if status not in {"ok", "partial", "failed"} or not isinstance(data, dict):
        raise ConnectorError("invalid_sync_result")
    sync_status = data.get("status")
    if sync_status not in {"complete", "best_effort_complete", "partial", "failed", "interrupted", "running"}:
        raise ConnectorError("invalid_sync_result")
    if data.get("checkpoint_advanced") is True:
        raise ConnectorError("unproven_source_checkpoint")
    # A connected-tool export never establishes an atomic source checkpoint,
    # regardless of any accidentally permissive coordinator result.
    safe = {"status": sync_status, "checkpoint_advanced": False}
    try:
        safe["sync_run_id"] = str(UUID(data.get("sync_run_id", "")))
    except (ValueError, TypeError, AttributeError):
        pass
    expected = data.get("expected_count")
    if isinstance(expected, int) and not isinstance(expected, bool) and expected >= 0:
        safe["expected_count"] = expected
    states = {"pending", "persisted", "unchanged", "suppressed", "deleted", "partial", "failed", "deferred", "quarantined"}
    if isinstance(data.get("item_counts"), dict):
        safe["item_counts"] = {key: value for key, value in data["item_counts"].items()
            if key in states and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
    errors = {"source_unavailable", "unauthorized", "forbidden", "not_found", "rate_limited", "source_changed",
              "invalid_response", "incomplete_response", "invalid_snapshot", "deletion_unreconciled",
              "explicit_restore_required", "forgotten"}
    error = data.get("error_code")
    safe["error_code"] = error if error is None or (isinstance(error, str) and error in errors) else "sync_unavailable"
    return {"status": status, "provider": "notion", "connection_mode": "connected_tool_bundle",
            "read_only": True, "mode": "full_bundle", "complete_source_sync": False,
            "checkpoint_committed": False, "atomic_source_version_available": False, "data": safe}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--runtime-config", type=Path, default=Path.home() / ".scholars-attendant" / "runtime" / "postgres.json")
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--full-bundle", action="store_true", help="Record explicitly complete collection traversal with durable best-effort coverage")
    args = parser.parse_args(argv)
    try:
        bundle = object_file(args.bundle)
        if args.full_bundle:
            from research_assistant.sync import SyncCoordinator
            adapter = NotionBundleAdapter(bundle, max_pages=args.max_pages, require_complete=True)
            service = configured_service(args.runtime_config)
            registration = {"provider": "notion", "source_instance_id": adapter.source_instance_id,
                            "namespace": adapter.namespace}
            register_source(service, registration)
            result = SyncCoordinator(service).run(adapter, **registration)
            summary = coordinator_summary(result)
            summary["redacted_pages"] = sum("temporary_urls_or_credentials_redacted" in snapshot["missing_parts"]
                                            for snapshot in adapter.snapshots)
            print(json.dumps(summary))
            return 0 if summary["data"]["status"] in {"complete", "best_effort_complete"} else 1
        pages = bundle.get("pages")
        if not isinstance(pages, list) or not pages or not 1 <= args.max_pages <= 100000 or len(pages) > args.max_pages:
            raise ConnectorError("invalid_bounded_bundle")
        snapshots, sanitized_count = [], 0
        for page in pages:
            if not isinstance(page, dict):
                raise ConnectorError("invalid_source_response")
            snapshot = normalize_mcp_page(
                page["raw_payload"], source_instance_id=bundle["source_instance_id"], namespace=bundle["namespace"],
                external_id=page["external_id"], markdown_path=page.get("markdown_path"),
                passage_paths=page.get("passage_paths"), external_version=page.get("external_version"),
                source_url=page.get("source_url"), occurred_at=page.get("occurred_at"),
                completeness=page.get("completeness", "unknown"), missing_parts=page.get("missing_parts"),
            )
            if "temporary_urls_or_credentials_redacted" in snapshot["missing_parts"]:
                sanitized_count += 1
            snapshots.append(snapshot)
        service = configured_service(args.runtime_config)
        registration = {key: snapshots[0][key] for key in ("provider", "source_instance_id", "namespace")}
        register_source(service, registration)
        receipts = []
        for snapshot in snapshots:
            # A new observation is a new idempotent event; source-version/content
            # deduplication belongs to the canonical ingestion service.
            request_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            response = service.execute("sources.ingest", {**snapshot, "idempotency_key": "notion:" + request_hash}, trusted_capture=True)
            if response["status"] not in {"ok", "partial"}:
                raise ConnectorError("ingestion_failed")
            receipts.append({key: value for key, value in response["data"].items()
                if key in {"source_object_id", "source_snapshot_id", "source_version_id", "evidence_ids", "revision", "suppressed", "deduplicated"}})
        print(json.dumps({"status": "ok", "provider": "notion", "connection_mode": "connected_tool_bundle",
            "read_only": True, "objects_persisted": len(receipts), "redacted_pages": sanitized_count,
            "complete_source_sync": False, "checkpoint_committed": False, "receipts": receipts}))
        return 0
    except (ConnectorError, DomainError) as exc:
        print(json.dumps({"status": "failed", "error_code": exc.code}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "sync_unavailable"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
