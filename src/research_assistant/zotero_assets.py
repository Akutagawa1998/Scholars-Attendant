"""Private operator binding of verified Zotero attachment bytes and annotations.

Attachment revisions are provider metadata, never scientific WorkVersions or
user reading. No remote request or provider write is performed here.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import re

from sqlalchemy import select

from . import storage as db
from .annotation_alignment import align_annotations
from .asset_service import (_PdfAssetContext, _blob_lock, _cleanup_unregistered_blob,
                            _content, _fingerprint)
from .assets import ArtifactStore, extract_pdf, anchor_passage

_KEY = re.compile(r'[A-Z0-9]{8}\Z')
_MD5 = re.compile(r'[a-f0-9]{32}\Z')
_PROVENANCE_KEYS = {'attachment_key', 'attachment_version', 'attachment_md5'}
_ACQUISITION_METHODS = frozenset({'zotero_api_file', 'zotero_local_storage'})


def _unavailable(code='attachment_source_unavailable'):
    from .service import DomainError
    return DomainError('forbidden', code, 'Registered attachment source is unavailable or changed')


def _current_metadata(service, c, snapshot_id):
    """Require scoped accessible heads, including object identity tombstones."""
    source = service._row(c, db.sources, snapshot_id)
    meta = c.execute(select(db.snapshot_metadata).where(service._scope(db.snapshot_metadata),
        db.snapshot_metadata.c.source_version_id == snapshot_id)).mappings().first()
    if not meta:
        raise _unavailable()
    obj = service._row(c, db.source_objects, meta['source_object_id'])
    registration = service._row(c, db.connector_registrations, obj['registration_id'])
    if (registration['provider'] != 'zotero' or source['kind'] != 'zotero'
            or source['access_state'] != 'accessible' or obj['access_state'] != 'accessible'
            or obj['head_source_version_id'] != snapshot_id
            or service._blocked(c, identity=snapshot_id, fingerprint=source['sha256'])
            or service._blocked(c, identity=obj['id'], fingerprint=obj['identity_hash'])):
        raise _unavailable()
    try:
        raw = json.loads(source['content'])
    except (ValueError, TypeError):
        raise _unavailable() from None
    if not isinstance(raw, dict) or not isinstance(raw.get('data'), dict):
        raise _unavailable()
    data = raw['data']
    if (not _KEY.fullmatch(obj['external_id']) or raw.get('key') != obj['external_id']
            or data.get('key', obj['external_id']) != obj['external_id']):
        raise _unavailable()
    return source, dict(meta), obj, registration, raw, data


def attachment_binding(service, c, snapshot_id, provenance):
    """Validate an attachment and optional bibliographic parent at current heads."""
    if isinstance(provenance, dict) and isinstance(provenance.get('attachment_md5'), str):
        provenance = dict(provenance, attachment_md5=provenance['attachment_md5'].lower())
    if (not isinstance(provenance, dict) or not _PROVENANCE_KEYS <= set(provenance)
            or set(provenance) - (_PROVENANCE_KEYS | {'acquisition_method'})
            or ('acquisition_method' in provenance and (not isinstance(provenance['acquisition_method'], str)
                or provenance['acquisition_method'] not in _ACQUISITION_METHODS))
            or not isinstance(provenance.get('attachment_key'), str)
            or not isinstance(provenance.get('attachment_md5'), str)
            or not _MD5.fullmatch(provenance['attachment_md5'])
            or type(provenance.get('attachment_version')) not in (str, int)):
        raise _unavailable('invalid_attachment_provenance')
    source, meta, obj, registration, raw, data = _current_metadata(service, c, snapshot_id)
    version = meta['external_version']
    if (meta['completeness'] != 'complete' or meta['missing_parts']
            or version is None or str(provenance['attachment_version']) != version
            or str(raw.get('version')) != version or str(data.get('version', raw.get('version'))) != version
            or provenance['attachment_key'] != obj['external_id']
            or data.get('itemType') != 'attachment' or data.get('contentType') != 'application/pdf'
            or data.get('linkMode') not in {'imported_file', 'imported_url'}
            or not isinstance(data.get('md5'), str) or data['md5'].lower() != provenance['attachment_md5']):
        raise _unavailable('attachment_metadata_mismatch')
    parent_key, parent_obj, parent_source = data.get('parentItem'), None, None
    if parent_key is not None:
        if not isinstance(parent_key, str) or not _KEY.fullmatch(parent_key):
            raise _unavailable('attachment_parent_unavailable')
        parent_obj = c.execute(select(db.source_objects).where(service._scope(db.source_objects),
            db.source_objects.c.registration_id == registration['id'],
            db.source_objects.c.external_id == parent_key)).mappings().first()
        if not parent_obj or not parent_obj['head_source_version_id']:
            raise _unavailable('attachment_parent_unavailable')
        parent_source, _, parent_obj, _, _, parent_data = _current_metadata(service, c, parent_obj['head_source_version_id'])
        if parent_data.get('itemType') in {'attachment', 'annotation', 'note', None}:
            raise _unavailable('attachment_parent_unavailable')
    return source, meta, obj, registration, parent_obj, parent_source


def _annotations(service, c, registration_id, attachment_key):
    rows = []
    candidates = c.execute(select(db.source_objects.c.head_source_version_id, db.sources.c.content).join(
            db.sources, db.source_objects.c.head_source_version_id == db.sources.c.id).where(
            service._scope(db.source_objects), service._scope(db.sources),
            db.source_objects.c.registration_id == registration_id,
            db.source_objects.c.access_state == 'accessible', db.sources.c.access_state == 'accessible')).mappings()
    for candidate in candidates:
        try:
            raw = json.loads(candidate['content'])
        except (ValueError, TypeError):
            continue
        data = raw.get('data') if isinstance(raw, dict) else None
        if not isinstance(data, dict) or data.get('itemType') != 'annotation' or data.get('parentItem') != attachment_key:
            continue
        try:
            source, _, obj, _, _, data = _current_metadata(service, c, candidate['head_source_version_id'])
        except Exception as error:
            from .service import DomainError
            if isinstance(error, DomainError):
                continue
            raise
        if data.get('itemType') == 'annotation' and data.get('parentItem') == attachment_key:
            rows.append((source['id'], {**data, 'key': obj['external_id']}))
    return rows


def _private_report(report):
    """Whitelist fixed statuses, positions and hashes; never duplicate prose."""
    fields = ('alignment_version', 'kind', 'page_index', 'page_number', 'source_sha256',
        'page_text_sha256', 'match_status', 'candidate_count', 'candidate_count_lower_bound',
        'candidates_truncated', 'warnings', 'position_observation')
    result = {field: report[field] for field in fields if field in report}
    result['matches'] = [{key: match[key] for key in ('text_start', 'text_end', 'literal_sha256',
        'source_sha256', 'page_text_sha256', 'match_method') if key in match} for match in report['matches']]
    return result


class ZoteroPdfIngestor:
    def __init__(self, service, store: ArtifactStore):
        self.service, self.store = service, store

    def ingest(self, data: bytes, *, attachment_snapshot_id: str, provenance: dict,
               acquisition_method: str = 'zotero_api_file') -> dict:
        from .service import DomainError, canonical, digest, timestamp
        if not isinstance(data, bytes):
            raise _unavailable('attachment_bytes_invalid')
        if (not isinstance(acquisition_method, str) or acquisition_method not in _ACQUISITION_METHODS
                or (isinstance(provenance, dict) and 'acquisition_method' in provenance
                    and provenance['acquisition_method'] != acquisition_method)):
            raise _unavailable('invalid_attachment_provenance')
        if isinstance(provenance, dict) and isinstance(provenance.get('attachment_md5'), str):
            provenance = dict(provenance, attachment_md5=provenance['attachment_md5'].lower())
        sha = hashlib.sha256(data).hexdigest()
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        # Do not publish even temporary bytes for suppressed/stale metadata.
        with self.service.storage.transaction() as c:
            self.service._epoch(c)
            _blob_lock(c, sha)
            attachment_binding(self.service, c, attachment_snapshot_id, provenance)
            if md5 != provenance['attachment_md5']:
                raise _unavailable('attachment_md5_mismatch')
            if self.service._blocked(c, fingerprint=_fingerprint(sha)):
                raise _unavailable('forgotten')
        extraction = extract_pdf(data)
        parser = extraction['parser_name'] + ':' + extraction['parser_version']
        blob = None
        try:
            with self.service.storage.transaction() as c:
                self.service._epoch(c)
                _blob_lock(c, sha)
                bound = attachment_binding(self.service, c, attachment_snapshot_id, provenance)
                _, _, obj, registration, parent_obj, parent_source = bound
                identity = 'zotero-attachment-pdf:' + digest(canonical([
                    attachment_snapshot_id, parent_source['id'] if parent_source else None, sha, parser]))
                if (self.service._blocked(c, fingerprint=_fingerprint(sha))
                        or self.service._blocked(c, fingerprint=digest(identity))
                        or self.service._blocked(c, fingerprint=digest(_content(extraction)))):
                    raise _unavailable('forgotten')
                existing = c.execute(select(db.sources).where(self.service._scope(db.sources),
                    db.sources.c.identity_key == identity)).mappings().first()
                if existing and (existing['access_state'] != 'accessible' or self.service._blocked(c, identity=existing['id'])):
                    raise _unavailable('forgotten')
                annotation_rows = _annotations(self.service, c, registration['id'], obj['external_id'])
                reports = align_annotations(extraction, [row[1] for row in annotation_rows], expected_pdf_sha256=sha)
                blob = self.store.put_bytes(data)
                # A store adapter must not permit upstream changes during I/O.
                final = attachment_binding(self.service, c, attachment_snapshot_id, provenance)
                if ((final[4]['id'], final[5]['id']) if final[4] else None) != ((parent_obj['id'], parent_source['id']) if parent_obj else None):
                    raise _unavailable('attachment_parent_unavailable')
                deduplicated = existing is not None
                source = dict(existing) if existing else self.service._new_source(c, key=identity,
                    kind='zotero_attachment_pdf', content=_content(extraction), url='', media='application/pdf',
                    extraction=parser, now=timestamp())
                context = _PdfAssetContext(sha, len(data), blob['relative_blob_key'], 'application/pdf', extraction, self.store, data)
                self.service._register_source_asset(c, {'data': {'source_snapshot_id': source['id']}}, context)
                link = c.execute(select(db.attachment_asset_links).where(self.service._scope(db.attachment_asset_links),
                    db.attachment_asset_links.c.metadata_snapshot_id == attachment_snapshot_id,
                    db.attachment_asset_links.c.pdf_source_id == source['id'])).mappings().first()
                if not link:
                    link = self.service._base(metadata_snapshot_id=attachment_snapshot_id, attachment_object_id=obj['id'],
                        parent_object_id=parent_obj['id'] if parent_obj else None,
                        parent_snapshot_id=parent_source['id'] if parent_source else None,
                        pdf_source_id=source['id'], provenance=dict(
                            {key: provenance[key] for key in sorted(_PROVENANCE_KEYS)}, acquisition_method=acquisition_method))
                    c.execute(db.attachment_asset_links.insert().values(**link))
                if not deduplicated:
                    for page in extraction['pages']:
                        if page['text']:
                            anchor = anchor_passage(extraction, page['page_index'], page['text'])
                            self.service._new_evidence(c, source['id'], anchor['text'], 'page', anchor['locator'])
                for (annotation_id, _), report in zip(annotation_rows, reports, strict=True):
                    _current_metadata(self.service, c, annotation_id)
                    prior = c.execute(select(db.annotation_alignments.c.id).where(self.service._scope(db.annotation_alignments),
                        db.annotation_alignments.c.annotation_snapshot_id == annotation_id,
                        db.annotation_alignments.c.pdf_source_id == source['id'])).first()
                    if not prior:
                        c.execute(db.annotation_alignments.insert().values(**self.service._base(
                            annotation_snapshot_id=annotation_id, pdf_source_id=source['id'], attachment_link_id=link['id'],
                            report=_private_report(report))))
                asset_id = c.execute(select(db.source_assets.c.id).where(self.service._scope(db.source_assets),
                    db.source_assets.c.source_version_id == source['id'])).scalar_one()
                return dict(source_snapshot_id=source['id'], asset_id=asset_id, deduplicated=deduplicated,
                    page_count=extraction['page_count'], alignment_counts=dict(Counter(report['match_status'] for report in reports)))
        except BaseException:
            if blob:
                try:
                    _cleanup_unregistered_blob(self.service, self.store, blob)
                except Exception:
                    pass
            raise
