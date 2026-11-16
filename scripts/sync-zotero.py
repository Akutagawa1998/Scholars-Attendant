#!/usr/bin/env python3
"""Read-only Zotero import into a separately configured private core DB.

No bibliography, credentials, URLs, raw provider errors or titles are printed.
The default bounded sample cannot establish a full-library checkpoint. --full
uses the durable coordinator for full/incremental item metadata synchronization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any
from uuid import UUID

from research_assistant.connectors import ConnectorError
from research_assistant.connectors.zotero import ZoteroReadOnlyClient
from research_assistant.service import DomainError, ResearchService


def object_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConnectorError("configuration_unavailable") from exc
    if not isinstance(data, dict):
        raise ConnectorError("invalid_configuration")
    return data


def request_key(snapshot: dict[str, Any]) -> str:
    # A retry of the same observed snapshot retains its key; a later poll gets
    # its own event. Canonical source deduplication follows version + content.
    material = {key: snapshot[key] for key in ("provider", "source_instance_id", "namespace", "external_id", "external_version", "raw_payload", "change", "observed_at")}
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "zotero:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def configured_service(runtime_path: Path) -> ResearchService:
    runtime = object_file(runtime_path) if not os.environ.get("RA_DATABASE_URL") else {}
    database_url = os.environ.get("RA_DATABASE_URL") or runtime.get("database_url")
    if not isinstance(database_url, str) or not database_url.startswith("postgresql+psycopg:"):
        raise ConnectorError("postgresql_configuration_required")
    owner_id = os.environ.get("RA_OWNER_ID") or runtime.get("owner_id", "local")
    scope_id = os.environ.get("RA_SCOPE_ID") or runtime.get("scope_id", "private")
    service = ResearchService(database_url, owner_id=owner_id, scope_id=scope_id)
    service.initialize()
    return service


def register_source(service: ResearchService, registration: dict[str, str]) -> None:
    registration_key = hashlib.sha256(json.dumps(registration, sort_keys=True).encode("utf-8")).hexdigest()
    service.execute("sources.register", {**registration, "idempotency_key": "zotero-register:" + registration_key}, trusted_capture=True)


def coordinator_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Allow only status/counters out of the durable coordinator envelope."""
    status = result.get("status")
    data = result.get("data")
    if status not in {"ok", "partial", "failed"} or not isinstance(data, dict):
        raise ConnectorError("invalid_sync_result")
    sync_status = data.get("status")
    if sync_status not in {"complete", "best_effort_complete", "partial", "failed", "interrupted", "running"}:
        raise ConnectorError("invalid_sync_result")
    safe_data: dict[str, Any] = {"status": sync_status, "checkpoint_advanced": data.get("checkpoint_advanced") is True}
    try:
        safe_data["sync_run_id"] = str(UUID(data.get("sync_run_id", "")))
    except (ValueError, TypeError, AttributeError):
        pass
    count = data.get("expected_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        safe_data["expected_count"] = count
    states = {"pending", "persisted", "unchanged", "suppressed", "deleted", "partial", "failed", "deferred", "quarantined"}
    counts = data.get("item_counts")
    if isinstance(counts, dict):
        safe_data["item_counts"] = {key: value for key, value in counts.items()
            if key in states and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
    safe_errors = {"source_unavailable", "unauthorized", "forbidden", "not_found", "rate_limited", "source_changed",
                   "invalid_response", "incomplete_response", "invalid_snapshot", "deletion_unreconciled",
                   "explicit_restore_required", "forgotten", "insufficient_permissions", "identity_mismatch",
                   "access_revoked", "authentication_failed", "source_version_regression",
                   "source_changed_during_pagination", "incomplete_source_response"}
    error = data.get("error_code")
    safe_data["error_code"] = error if error is None or (isinstance(error, str) and error in safe_errors) else "sync_unavailable"
    return {"status": status, "provider": "zotero", "read_only": True,
            "mode": "full_metadata", "original_files_acquired": False,
            "complete_library_sync": safe_data["checkpoint_advanced"],
            "checkpoint_committed": safe_data["checkpoint_advanced"], "data": safe_data}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, default=Path.home() / ".scholars-attendant" / "zotero.json")
    parser.add_argument("--runtime-config", type=Path, default=Path.home() / ".scholars-attendant" / "runtime" / "postgres.json")
    parser.add_argument("--max-items", type=int, default=5)
    parser.add_argument("--since", type=int)
    parser.add_argument("--include-deleted", action="store_true")
    parser.add_argument("--full", action="store_true", help="Synchronize all Zotero item metadata with durable incremental state")
    parser.add_argument("--probe", action="store_true", help="Read provider only; do not touch the core database")
    args = parser.parse_args(argv)
    if args.full and (args.since is not None or args.include_deleted or args.max_items != 5):
        parser.error("--full uses its durable cursor; do not combine with --since, --include-deleted or --max-items")
    try:
        credentials = object_file(args.credentials)
        api_key = credentials.get("api_key")
        library_type = credentials.get("library_type", "user")
        library_id = credentials.get("library_id") or credentials.get("user_id")
        with ZoteroReadOnlyClient(api_key, str(library_id), library_type=library_type) as connector:
            if args.full:
                if args.probe:
                    inventory = connector.inventory()
                    summary = {
                        "status": "ok", "provider": "zotero", "read_only": True,
                        "mode": "full_metadata", "probe_only": True,
                        "objects_listed": len(inventory["object_versions"]),
                        "inventory_complete": inventory["complete"] is True,
                        "inventory_consistent": inventory["consistent"] is True,
                        "complete_library_sync": False, "checkpoint_committed": False,
                        "original_files_acquired": False,
                    }
                else:
                    from research_assistant.sync import SyncCoordinator
                    service = configured_service(args.runtime_config)
                    registration = {"provider": "zotero", "source_instance_id": connector.source_instance_id,
                                    "namespace": connector.namespace}
                    register_source(service, registration)
                    result = SyncCoordinator(service).run(connector, **registration)
                    summary = coordinator_summary(result)
                print(json.dumps(summary, ensure_ascii=False))
                return 0 if summary["status"] == "ok" else 1
            batch = connector.fetch(max_items=args.max_items, since=args.since, include_deleted=args.include_deleted)
        summary = {
            "status": "ok", "provider": "zotero", "read_only": True,
            "objects_fetched": len(batch.snapshots), "pages_fetched": batch.pages_fetched,
            "library_version": batch.library_version, "complete_library_sync": False,
            "checkpoint_committed": False, "missing_parts": batch.missing_parts,
        }
        if args.probe:
            summary["probe_only"] = True
            print(json.dumps(summary, ensure_ascii=False))
            return 0
        service = configured_service(args.runtime_config)
        registration = {"provider": batch.provider, "source_instance_id": batch.source_instance_id, "namespace": batch.namespace}
        register_source(service, registration)
        receipts = []
        for snapshot in batch.snapshots:
            response = service.execute("sources.ingest", {**snapshot, "idempotency_key": request_key(snapshot)}, trusted_capture=True)
            if response["status"] not in {"ok", "partial"}:
                raise ConnectorError("ingestion_failed")
            # Only opaque IDs/counters may leave this runner on stdout.
            receipt = {key: value for key, value in response["data"].items()
                       if key in {"source_object_id", "source_snapshot_id", "source_version_id", "evidence_ids", "revision", "suppressed", "deduplicated"}}
            receipts.append(receipt)
        summary.update(objects_persisted=len(receipts), receipts=receipts)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except (ConnectorError, DomainError) as exc:
        print(json.dumps({"status": "failed", "error_code": exc.code}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "sync_unavailable"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
