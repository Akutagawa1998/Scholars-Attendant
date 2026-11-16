"""Operator-only PDF ingestion and source-authorized private blob lifecycle.

The raw bytes are an asset identity, not a scientific paper-version claim. The
operator supplies an explicit paper version independently of connector versions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any

from sqlalchemy import select, update, text as sql_text

from . import storage as db
from .assets import ArtifactStore, AssetError, anchor_passage, extract_pdf


def _byte_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint(identity: str) -> str:
    return _byte_hash(("pdf-asset:sha256:" + identity).encode())


def _blob_lock(c, identity: str):
    """Serialize shared-byte registration/deletion across owner/scope boundaries."""
    if c.dialect.name == "postgresql":
        lock_key = int.from_bytes(hashlib.sha256(("pdf-blob:" + identity).encode()).digest()[:8], "big", signed=True)
        c.execute(sql_text("SELECT pg_advisory_xact_lock(:identity)"), {"identity": lock_key})
    # SQLite fixture transactions already use BEGIN IMMEDIATE.


def _content(extraction: dict[str, Any]) -> str:
    # The page separator is explicit; passage anchors remain page-local.
    return "\n\f\n".join(page["text"] for page in extraction["pages"])


@dataclass(repr=False)
class _PdfAssetContext:
    """In-process capability; unavailable to HTTP/MCP request models."""
    original_sha256: str
    byte_size: int
    relative_blob_key: str
    media_type: str
    extraction: dict[str, Any] = field(repr=False)
    store: ArtifactStore = field(repr=False)
    data: bytes = field(repr=False)

    def __repr__(self):
        return "_PdfAssetContext(<private>)"


class AssetOperations:
    """Hooks used by ResearchService within its canonical mutation transaction."""

    def _dependent_asset_sources(self, c, source_ids):
        selected = set(source_ids)
        for link in c.execute(select(db.attachment_asset_links).where(self._scope(db.attachment_asset_links))).mappings():
            if link['metadata_snapshot_id'] in selected or link['parent_snapshot_id'] in selected:
                selected.add(link['pdf_source_id'])
        return selected

    def _linked_asset_available(self, c, source_id):
        """A derived attachment stays visible only through a current valid link."""
        from .service import DomainError
        from .zotero_assets import attachment_binding
        source = self._row(c, db.sources, source_id)
        if source['kind'] != 'zotero_attachment_pdf':
            return True
        if source['access_state'] != 'accessible' or self._blocked(c, identity=source_id):
            return False
        asset = c.execute(select(db.source_assets.c.id).where(self._scope(db.source_assets),
            db.source_assets.c.source_version_id == source_id, db.source_assets.c.state == 'active')).first()
        if not asset:
            return False
        for link in c.execute(select(db.attachment_asset_links).where(self._scope(db.attachment_asset_links),
                db.attachment_asset_links.c.pdf_source_id == source_id)).mappings():
            try:
                binding = attachment_binding(self, c, link['metadata_snapshot_id'], link['provenance'])
                if (binding[2]['id'] == link['attachment_object_id']
                        and (binding[4]['id'] if binding[4] else None) == link['parent_object_id']
                        and (binding[5]['id'] if binding[5] else None) == link['parent_snapshot_id']):
                    return True
            except DomainError:
                continue
        return False

    def _validate_asset_context(self, c, params, context):
        from .service import DomainError, digest
        if not isinstance(context, _PdfAssetContext) or params.get("media_type") != "application/pdf" or context.media_type != "application/pdf":
            raise DomainError("forbidden", "pdf_adapter_required", "PDF imports require the operator byte ingestion adapter")
        extraction = context.extraction
        identity = _byte_hash(context.data)
        if (identity != context.original_sha256 or len(context.data) != context.byte_size
                or extraction.get("source_sha256") != identity or extraction.get("source_size") != len(context.data)
                or params.get("content") != _content(extraction)
                or params.get("extraction_version") != extraction.get("parser_name", "") + ":" + extraction.get("parser_version", "")):
            raise DomainError("failed", "invalid_pdf_asset_context", "Original bytes and extracted snapshot do not agree")
        _blob_lock(c, identity)
        if (self._blocked(c, fingerprint=_fingerprint(identity))
                or self._blocked(c, fingerprint=digest(params["identifier"].strip()))
                or self._blocked(c, fingerprint=digest(params["content"]))):
            raise DomainError("forbidden", "forgotten", "This asset or paper identity is suppressed by a deletion record")
        old_version = c.execute(select(db.versions).where(self._scope(db.versions),
            db.versions.c.identifier == params["identifier"].strip())).mappings().first()
        if old_version:
            source = self._row(c, db.sources, old_version["source_version_id"])
            if source["access_state"] != "accessible" or self._blocked(c, identity=source["id"]):
                raise DomainError("forbidden", "asset_source_unavailable", "Asset source is not available")
            old_asset = c.execute(select(db.source_assets).where(self._scope(db.source_assets),
                db.source_assets.c.source_version_id == source["id"])).mappings().first()
            if not old_asset or old_asset["state"] != "active" or old_asset["sha256"] != identity:
                raise DomainError("conflict", "immutable_pdf_asset", "An explicit paper version cannot change its original PDF bytes")
        for passage in params["passages"]:
            locator = passage.get("locator", {})
            page_index = locator.get("page_index")
            try:
                expected = anchor_passage(extraction, page_index, passage["text"], occurrence=locator.get("occurrence", 0))
            except AssetError:
                raise DomainError("failed", "invalid_pdf_anchor", "Passage does not match the extracted PDF page") from None
            allowed_keys = set(expected["locator"]) | {"occurrence"}
            if set(locator) - allowed_keys or any(locator.get(key) != value for key, value in expected["locator"].items()):
                raise DomainError("failed", "invalid_pdf_anchor", "PDF byte, page and character anchors must match")
        # A purge can have completed after the adapter's first put. Re-publish
        # only after acquiring the same hash lock used by purge/registration.
        try:
            try:
                context.store.read_blob(context.relative_blob_key, identity)
            except AssetError as error:
                if error.code != "blob_not_found":
                    raise
                row = context.store.put_bytes(context.data)
                if row["relative_blob_key"] != context.relative_blob_key:
                    raise AssetError("invalid_blob_key")
        except AssetError as error:
            raise DomainError("failed", error.code, "PDF asset storage validation failed") from None

    def _register_source_asset(self, c, response, context):
        from .service import DomainError
        source_id = response["data"]["source_snapshot_id"]
        source = self._row(c, db.sources, source_id)
        if source["media_type"] != "application/pdf" or source["access_state"] != "accessible" or self._blocked(c, identity=source_id):
            raise DomainError("forbidden", "asset_source_unavailable", "Asset source is not available")
        existing = c.execute(select(db.source_assets).where(self._scope(db.source_assets),
            db.source_assets.c.source_version_id == source_id)).mappings().first()
        if existing:
            if existing["state"] != "active" or existing["sha256"] != context.original_sha256:
                raise DomainError("conflict", "immutable_pdf_asset", "An explicit paper version cannot change its original PDF bytes")
            return
        extracted = context.extraction
        page_metadata = [{"page_index": page["page_index"], "text_sha256": page["text_sha256"],
                          "text_length": len(page["text"]), "warnings": page["warnings"]} for page in extracted["pages"]]
        c.execute(db.source_assets.insert().values(**self._base(source_version_id=source_id,
            sha256=context.original_sha256, byte_size=context.byte_size, blob_key=context.relative_blob_key,
            media_type=context.media_type, parser_name=extracted["parser_name"], parser_version=extracted["parser_version"],
            quality=extracted["quality"], warnings=extracted["warnings"], page_metadata=page_metadata, state="active")))

    def _mark_source_asset_for_purge(self, c, source_id, now):
        for asset in c.execute(select(db.source_assets).where(self._scope(db.source_assets),
                db.source_assets.c.source_version_id == source_id)).mappings():
            self._tombstone(c, "asset:" + asset["id"], _fingerprint(asset["sha256"]), now)
            if asset["state"] != "purged":
                c.execute(update(db.source_assets).where(db.source_assets.c.id == asset["id"]).values(state="purge_pending"))


class PdfIngestor:
    """Local operator adapter; returns canonical receipts without private keys."""
    def __init__(self, service, store: ArtifactStore):
        self.service, self.store = service, store

    def ingest(self, data: bytes, *, identifier: str, title: str, version: str, source_url: str = "",
               passages: list[dict[str, Any]], idempotency_key: str):
        extraction = extract_pdf(data)
        anchored = []
        for passage in passages:
            anchor = anchor_passage(extraction, passage["page_index"], passage["text"], occurrence=passage.get("occurrence", 0))
            if passage.get("occurrence", 0):
                anchor["locator"]["occurrence"] = passage["occurrence"]
            anchored.append(dict(text=anchor["text"], section=passage["section"], locator=anchor["locator"]))
        payload = dict(identifier=identifier, title=title, version=version, source_url=source_url,
            content=_content(extraction), media_type="application/pdf",
            extraction_version=extraction["parser_name"] + ":" + extraction["parser_version"],
            passages=anchored, idempotency_key=idempotency_key)
        blob = self.store.put_bytes(data)
        context = _PdfAssetContext(blob["sha256"], blob["size"], blob["relative_blob_key"], blob["media_type"], extraction, self.store, data)
        try:
            return self.service.execute("papers.import", payload, trusted_capture=True, _asset_context=context)
        except BaseException:
            # Cleanup is best-effort; never hide the primary failure. A pending
            # referenced blob is handled by explicit source-forget purge.
            try:
                _cleanup_unregistered_blob(self.service, self.store, blob)
            except Exception:
                pass
            raise


def _cleanup_unregistered_blob(service, store, blob):
    with service.storage.transaction() as c:
        service._epoch(c)
        _blob_lock(c, blob["sha256"])
        if not c.execute(select(db.source_assets.c.id).where(db.source_assets.c.sha256 == blob["sha256"],
                db.source_assets.c.state == "active")).first():
            store.delete_blob(blob["relative_blob_key"], blob["sha256"])


def read_source_asset(service, store: ArtifactStore, source_id: str) -> bytes:
    """Private operator byte access: scope, source visibility and deletion checked."""
    from .service import DomainError
    with service.storage.transaction() as c:
        service._epoch(c)
        source = service._row(c, db.sources, source_id)
        if (source["access_state"] != "accessible" or service._blocked(c, identity=source_id)
                or not service._linked_asset_available(c, source_id)):
            raise DomainError("forbidden", "source_unavailable", "Source asset is unavailable")
        asset = c.execute(select(db.source_assets).where(service._scope(db.source_assets),
            db.source_assets.c.source_version_id == source_id, db.source_assets.c.state == "active")).mappings().first()
        if not asset:
            raise DomainError("not_found", "asset_not_found", "Source asset is not available")
        _blob_lock(c, asset["sha256"])
        return store.read_blob(asset["blob_key"], asset["sha256"])


def purge_pending_assets(service, store: ArtifactStore) -> dict[str, int]:
    """Retryable local cleanup, retaining bytes still referenced in any scope."""
    purged, retained = 0, 0
    with service.storage.transaction() as c:
        service._epoch(c)
        rows = list(c.execute(select(db.source_assets).where(service._scope(db.source_assets),
            db.source_assets.c.state == "purge_pending").order_by(db.source_assets.c.sha256)).mappings())
        for asset in rows:
            _blob_lock(c, asset["sha256"])
            # Retain even revoked active sources: a legitimate restoration must
            # preserve its bytes. Only explicit forget releases its reference.
            active = c.execute(select(db.source_assets.c.id).where(db.source_assets.c.sha256 == asset["sha256"],
                db.source_assets.c.state == "active")).first()
            if active:
                retained += 1
                continue
            store.delete_blob(asset["blob_key"], asset["sha256"])
            c.execute(update(db.source_assets).where(db.source_assets.c.id == asset["id"]).values(
                state="purged", blob_key="", parser_name="", parser_version="", quality="", warnings=[], page_metadata=[]))
            purged += 1
    return {"purged": purged, "retained_shared": retained}
