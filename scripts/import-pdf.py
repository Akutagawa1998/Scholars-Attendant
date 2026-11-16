#!/usr/bin/env python3
"""Import a local PDF through the private operator byte adapter.

Metadata and selected literal passages are read from a private JSON file. Its
explicit paper version must be verified independently of Zotero item.version.
No remote download or remote write is performed; stdout contains IDs/counters.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit

from research_assistant.assets import ArtifactStore, AssetError, MAX_PDF_BYTES
from research_assistant.asset_service import PdfIngestor, purge_pending_assets
from research_assistant.service import DomainError, ResearchService
from research_assistant.storage_paths import select_asset_root


def local_bytes(path: Path, limit: int) -> bytes:
    handle = None
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            raise AssetError("invalid_local_file")
        if info.st_size > limit:
            raise AssetError("local_file_too_large")
        with os.fdopen(handle, "rb") as stream:
            handle = None
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise AssetError("local_file_too_large")
        return value
    except OSError:
        raise AssetError("local_file_unavailable") from None
    finally:
        if handle is not None:
            os.close(handle)


def object_file(path: Path) -> dict:
    try:
        value = json.loads(local_bytes(path, 1024 * 1024))
    except (ValueError, UnicodeError):
        raise AssetError("invalid_pdf_metadata") from None
    if not isinstance(value, dict):
        raise AssetError("invalid_pdf_metadata")
    return value


def configured_service(runtime_path: Path):
    runtime = object_file(runtime_path) if not os.environ.get("RA_DATABASE_URL") else {}
    database_url = os.environ.get("RA_DATABASE_URL") or runtime.get("database_url")
    if not isinstance(database_url, str) or not database_url.startswith("postgresql+psycopg:"):
        raise AssetError("postgresql_configuration_required")
    service = ResearchService(database_url,
        owner_id=os.environ.get("RA_OWNER_ID") or runtime.get("owner_id", "local"),
        scope_id=os.environ.get("RA_SCOPE_ID") or runtime.get("scope_id", "private"))
    service.initialize()
    return service


def private_root(path: Path) -> Path:
    repository = Path(__file__).resolve().parents[1]
    if path.expanduser().resolve().is_relative_to(repository):
        raise AssetError("private_asset_root_required")
    return path


def import_metadata(value: dict) -> dict:
    required = {"identifier", "title", "version", "passages", "idempotency_key"}
    if not required <= value.keys() or set(value) - (required | {"source_url"}):
        raise AssetError("invalid_pdf_metadata")
    for key in required - {"passages"}:
        if not isinstance(value[key], str) or not value[key].strip():
            raise AssetError("invalid_pdf_metadata")
    if not isinstance(value["passages"], list) or not value["passages"]:
        raise AssetError("invalid_pdf_metadata")
    for passage in value["passages"]:
        if (not isinstance(passage, dict) or not {"page_index", "text", "section"} <= passage.keys()
                or set(passage) - {"page_index", "text", "section", "occurrence"}
                or not isinstance(passage["text"], str) or not passage["text"]
                or not isinstance(passage["section"], str)):
            raise AssetError("invalid_pdf_metadata")
    source_url = value.get("source_url", "")
    if not isinstance(source_url, str):
        raise AssetError("invalid_pdf_metadata")
    if source_url:
        parts = urlsplit(source_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
            raise AssetError("invalid_pdf_source_url")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, help="Explicit local PDF path; never downloaded automatically")
    parser.add_argument("--metadata", type=Path, help="Private JSON with identifier/title/version/passages/idempotency_key")
    parser.add_argument("--runtime-config", type=Path, default=Path.home() / ".scholars-attendant" / "runtime" / "postgres.json")
    parser.add_argument("--asset-root", type=Path, default=None,
        help="Override RA_ASSET_ROOT/private asset_root; configured external volumes must already be mounted")
    parser.add_argument("--purge-pending", action="store_true", help="Retry physical cleanup of explicitly forgotten source assets")
    args = parser.parse_args(argv)
    if args.purge_pending and (args.pdf or args.metadata):
        parser.error("--purge-pending cannot be combined with --pdf or --metadata")
    if not args.purge_pending and (not args.pdf or not args.metadata):
        parser.error("--pdf and --metadata are both required for import")
    service = None
    try:
        root = private_root(select_asset_root(args.asset_root,args.runtime_config))
        if not args.purge_pending:
            metadata = import_metadata(object_file(args.metadata))
            data = local_bytes(args.pdf, MAX_PDF_BYTES)
        service = configured_service(args.runtime_config)
        with ArtifactStore(root) as store:
            if args.purge_pending:
                print(json.dumps({"status": "ok", "cleanup": purge_pending_assets(service, store)}))
            else:
                response = PdfIngestor(service, store).ingest(data, **metadata)
                receipt = response["data"]
                print(json.dumps({"status": response["status"], "original_file_acquired": True, "remote_write": False,
                    "data": {key: receipt[key] for key in ("paper_id", "paper_version_id", "source_snapshot_id")},
                    "evidence_count": len(receipt["evidence_ids"])}))
        return 0
    except (AssetError, DomainError) as error:
        print(json.dumps({"status": "failed", "error_code": error.code}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "pdf_import_unavailable"}), file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.storage.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
