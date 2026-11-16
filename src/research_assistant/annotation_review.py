"""Operator-only versioned local reviews, with original v1 reports preserved.

No HTTP operation, source ingestion, evidence creation, provider write, image
output or reading assertion is exposed here. Computation can happen outside a
transaction; recording rechecks the captured inputs and scope epochs.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from . import storage as db
from .asset_service import _blob_lock
from .assets import AssetError
from .service import DomainError, canonical, digest, timestamp
from .zotero_assets import _current_metadata, attachment_binding

_HASH = r'^[a-f0-9]{64}\Z'
_TOKEN = r'^[a-zA-Z0-9][a-zA-Z0-9_.+-]{0,95}\Z'
Engine = Literal['pdfium', 'pypdf', 'mupdf', 'combined']


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, regex_engine='python-re')


class _Parameters(_Strict):
    engine: Engine | None = None
    engine_version: str | None = Field(default=None, pattern=_TOKEN)
    normalization_version: str | None = Field(default=None, pattern=_TOKEN)
    overlap_threshold: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    margin_threshold: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    tolerance: float | None = Field(default=None, ge=0, le=1000, allow_inf_nan=False)
    coordinate_system: Literal['pdf_points_bottom_left', 'pdf_points_top_left', 'zotero_pdf', 'canonical_text'] | None = None


class _Span(_Strict):
    text_start: int = Field(ge=0)
    text_end: int = Field(gt=0)
    literal_sha256: str = Field(pattern=_HASH)


class _Geometry(_Strict):
    candidate_index: int | None = Field(default=None, ge=0, le=1024)
    overlap: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    runner_up_overlap: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    rects: list[list[float]] = Field(default_factory=list, max_length=1000)

    @field_validator('rects')
    @classmethod
    def rectangles(cls, rects):
        if any(len(rect) != 4 or any(not math.isfinite(value) or abs(value) > 1_000_000 for value in rect)
               or not rect[0] < rect[2] or not rect[1] < rect[3] for rect in rects):
            raise ValueError('invalid_geometry')
        return rects


class _EngineObservation(_Strict):
    engine: Engine
    text_sha256: str | None = Field(default=None, pattern=_HASH)
    clip_text_sha256: str | None = Field(default=None, pattern=_HASH)
    page_text_sha256: str | None = Field(default=None, pattern=_HASH)
    literal_sha256: str | None = Field(default=None, pattern=_HASH)
    char_count: int | None = Field(default=None, ge=0, le=5_000_000)
    page_index: int | None = Field(default=None, ge=0, le=1000)


class _Report(_Strict):
    status: Literal['resolved_by_geometry', 'resolved_by_local_text', 'unresolved', 'located_with_parser_difference']
    method: Literal['geometry_overlap', 'local_text_normalization', 'geometry_and_local_text', 'none']
    basis: Literal['geometry_unique', 'canonical_literal', 'geometry_and_text', 'parser_difference', 'no_unique_match']
    parameters: _Parameters = Field(default_factory=_Parameters)
    selected_spans: list[_Span] = Field(default_factory=list, max_length=1)
    geometry: _Geometry | None = None
    engine_observation: _EngineObservation | None = None


def _fail(code='annotation_review_unavailable', status='forbidden'):
    return DomainError(status, code, 'Local annotation review inputs are unavailable or changed')


def _canonical_pages(source, asset):
    """Reconstruct exact parser pages from stored lengths, not split heuristics."""
    metadata = asset['page_metadata']
    if not isinstance(metadata, list) or not 1 <= len(metadata) <= 1000:
        raise _fail('invalid_canonical_pages')
    pages, offset = [], 0
    for index, page in enumerate(metadata):
        if (not isinstance(page, dict) or type(page.get('page_index')) is not int or page['page_index'] != index
                or type(page.get('text_length')) is not int or not 0 <= page['text_length'] <= 5_000_000):
            raise _fail('invalid_canonical_pages')
        end = offset + page['text_length']
        literal = source['content'][offset:end]
        if len(literal) != page['text_length'] or digest(literal) != page.get('text_sha256'):
            raise _fail('invalid_canonical_pages')
        pages.append(dict(page_index=index, text=literal, text_sha256=page['text_sha256']))
        offset = end
        if index + 1 < len(metadata):
            if source['content'][offset:offset + 3] != '\n\f\n':
                raise _fail('invalid_canonical_pages')
            offset += 3
    if offset != len(source['content']):
        raise _fail('invalid_canonical_pages')
    return pages


class AnnotationReviewService:
    """Private Python capability: no report can be submitted through HTTP/MCP."""
    def __init__(self, service, store):
        self.service, self.store = service, store

    def _capture(self, c, alignment_id):
        service = self.service
        epoch = service._epoch(c)
        alignment = service._row(c, db.annotation_alignments, alignment_id)
        if not alignment['report']:
            raise _fail()
        link = service._row(c, db.attachment_asset_links, alignment['attachment_link_id'])
        source = service._row(c, db.sources, alignment['pdf_source_id'])
        if (link['pdf_source_id'] != source['id'] or source['access_state'] != 'accessible'
                or source['kind'] != 'zotero_attachment_pdf' or source['media_type'] != 'application/pdf'
                or service._blocked(c, identity=source['id']) or not service._linked_asset_available(c, source['id'])):
            raise _fail()
        binding = attachment_binding(service, c, link['metadata_snapshot_id'], link['provenance'])
        attachment_source, _, attachment_obj, registration, parent_obj, parent_source = binding
        if (attachment_obj['id'] != link['attachment_object_id']
                or (parent_obj['id'] if parent_obj else None) != link['parent_object_id']
                or (parent_source['id'] if parent_source else None) != link['parent_snapshot_id']):
            raise _fail()
        annotation_source, _, annotation_obj, annotation_registration, _, annotation = _current_metadata(
            service, c, alignment['annotation_snapshot_id'])
        if (annotation_registration['id'] != registration['id'] or annotation.get('itemType') != 'annotation'
                or annotation.get('parentItem') != link['provenance']['attachment_key']):
            raise _fail()
        asset = c.execute(select(db.source_assets).where(service._scope(db.source_assets),
            db.source_assets.c.source_version_id == source['id'], db.source_assets.c.state == 'active')).mappings().first()
        if not asset:
            raise _fail()
        _blob_lock(c, asset['sha256'])
        try:
            raw = self.store.read_blob(asset['blob_key'], asset['sha256'])
        except AssetError:
            raise _fail('annotation_review_asset_unavailable') from None
        if len(raw) != asset['byte_size']:
            raise _fail('annotation_review_integrity_mismatch')
        for snapshot in (source, annotation_source, attachment_source, parent_source):
            if snapshot is not None and digest(snapshot['content']) != snapshot['sha256']:
                raise _fail('annotation_review_integrity_mismatch')
        if (source['extraction_version'] != asset['parser_name'] + ':' + asset['parser_version']
                or alignment['report'].get('source_sha256') != asset['sha256']):
            raise _fail('annotation_review_integrity_mismatch')
        pages = _canonical_pages(source, asset)
        page_index = alignment['report'].get('page_index')
        page = pages[page_index] if type(page_index) is int and 0 <= page_index < len(pages) else None
        if page and alignment['report'].get('page_text_sha256') != page['text_sha256']:
            raise _fail('annotation_review_integrity_mismatch')
        dependencies = dict(scope_key=service.scope_key, alignment_id=alignment_id,
            v1_report_sha256=digest(canonical(alignment['report'])),
            link_id=link['id'], link_provenance_sha256=digest(canonical(link['provenance'])),
            annotation_snapshot_id=annotation_source['id'], annotation_sha256=annotation_source['sha256'],
            annotation_revision=annotation_source['revision'], annotation_object_revision=annotation_obj['revision'],
            attachment_snapshot_id=attachment_source['id'], attachment_sha256=attachment_source['sha256'],
            attachment_revision=attachment_source['revision'], attachment_object_revision=attachment_obj['revision'],
            parent_snapshot_id=parent_source['id'] if parent_source else None,
            parent_sha256=parent_source['sha256'] if parent_source else None,
            parent_revision=parent_source['revision'] if parent_source else None,
            parent_object_revision=parent_obj['revision'] if parent_obj else None,
            pdf_source_id=source['id'], pdf_content_sha256=source['sha256'], pdf_source_revision=source['revision'],
            asset_id=asset['id'], raw_pdf_sha256=asset['sha256'], raw_pdf_size=asset['byte_size'],
            parser_name=asset['parser_name'], parser_version=asset['parser_version'],
            page_metadata_sha256=digest(canonical(asset['page_metadata'])))
        token = digest(canonical(dict(dependencies=dependencies, access_epoch=epoch['access_epoch'],
                                      deletion_epoch=epoch['deletion_epoch'])))
        capture = dict(alignment_id=alignment_id, annotation_snapshot_id=annotation_source['id'],
            pdf_source_id=source['id'], attachment_link_id=link['id'], raw_annotation=deepcopy(annotation),
            v1_report=deepcopy(alignment['report']), canonical_page=deepcopy(page), pdf_sha256=asset['sha256'],
            canonical_parser=dict(name=asset['parser_name'], version=asset['parser_version']), input_token=token)
        return capture, dependencies

    def capture(self, alignment_id):
        with self.service.storage.transaction() as c:
            return self._capture(c, alignment_id)[0]

    @staticmethod
    def _fingerprint(dependencies, algorithm_version, report):
        return digest(canonical(dict(dependencies=dependencies, algorithm_version=algorithm_version,
                                     parameters=report['parameters'])))

    @staticmethod
    def _validate_report(raw, capture):
        if not isinstance(raw, dict):
            raise _fail('invalid_annotation_review', 'failed')
        try:
            model = _Report.model_validate(raw)
        except ValidationError:
            raise _fail('invalid_annotation_review', 'failed') from None
        report = model.model_dump(mode='json', exclude_none=True)
        selected = report['selected_spans']
        resolved = report['status'] in {'resolved_by_geometry', 'resolved_by_local_text'}
        if (resolved and len(selected) != 1) or (not resolved and selected):
            raise _fail('invalid_annotation_review', 'failed')
        page = capture['canonical_page']
        for span in selected:
            if (page is None or not span['text_start'] < span['text_end'] <= len(page['text'])
                    or digest(page['text'][span['text_start']:span['text_end']]) != span['literal_sha256']):
                raise _fail('invalid_annotation_review_span', 'failed')
        geometry = report.get('geometry')
        if report['status'] == 'resolved_by_geometry':
            if (report['method'] not in {'geometry_overlap', 'geometry_and_local_text'}
                    or report['basis'] not in {'geometry_unique', 'geometry_and_text'}
                    or not geometry or 'candidate_index' not in geometry or geometry.get('overlap', 0) <= 0):
                raise _fail('invalid_annotation_review', 'failed')
        elif report['status'] == 'resolved_by_local_text':
            if (report['method'] not in {'local_text_normalization', 'geometry_and_local_text'}
                    or report['basis'] not in {'canonical_literal', 'geometry_and_text'}):
                raise _fail('invalid_annotation_review', 'failed')
        elif report['status'] == 'unresolved' and report['basis'] != 'no_unique_match':
            raise _fail('invalid_annotation_review', 'failed')
        elif report['status'] == 'located_with_parser_difference' and (
                report['basis'] != 'parser_difference' or report['method'] == 'none'):
            raise _fail('invalid_annotation_review', 'failed')
        if geometry and 'candidate_index' in geometry:
            candidates = capture['v1_report'].get('matches', [])
            index = geometry['candidate_index']
            if index >= len(candidates):
                raise _fail('invalid_annotation_review_candidate', 'failed')
            candidate = candidates[index]
            if selected and any(selected[0][field] != candidate[field] for field in ('text_start', 'text_end', 'literal_sha256')):
                raise _fail('invalid_annotation_review_candidate', 'failed')
        observation = report.get('engine_observation')
        if observation and 'page_index' in observation and (page is None or observation['page_index'] != page['page_index']):
            raise _fail('invalid_annotation_review', 'failed')
        if report['status'] == 'located_with_parser_difference' and (not observation or not any(
                key in observation for key in ('text_sha256', 'clip_text_sha256', 'literal_sha256'))):
            raise _fail('invalid_annotation_review', 'failed')
        return report

    def record(self, alignment_id, report, *, algorithm_version, expected_capture, input_fingerprint=None):
        if (not isinstance(algorithm_version, str) or not re.fullmatch(_TOKEN, algorithm_version)
                or not isinstance(expected_capture, dict) or expected_capture.get('alignment_id') != alignment_id
                or not isinstance(expected_capture.get('input_token'), str)):
            raise _fail('invalid_annotation_review', 'failed')
        with self.service.storage.transaction() as c:
            capture, dependencies = self._capture(c, alignment_id)
            if capture['input_token'] != expected_capture['input_token']:
                raise _fail('annotation_review_inputs_changed', 'conflict')
            cleaned = self._validate_report(report, capture)
            fingerprint = self._fingerprint(dependencies, algorithm_version, cleaned)
            if input_fingerprint is not None and input_fingerprint != fingerprint:
                raise _fail('annotation_review_fingerprint_mismatch', 'failed')
            existing = c.execute(select(db.annotation_alignment_resolutions).where(
                self.service._scope(db.annotation_alignment_resolutions),
                db.annotation_alignment_resolutions.c.alignment_id == alignment_id,
                db.annotation_alignment_resolutions.c.algorithm_version == algorithm_version,
                db.annotation_alignment_resolutions.c.input_fingerprint == fingerprint)).mappings().first()
            if existing:
                if canonical(existing['report']) != canonical(cleaned):
                    raise _fail('annotation_review_result_conflict', 'conflict')
                return dict(review_id=existing['id'], deduplicated=True, input_fingerprint=fingerprint)
            row = self.service._base(alignment_id=alignment_id, algorithm_version=algorithm_version,
                input_fingerprint=fingerprint, report=cleaned, recorded_at=timestamp())
            c.execute(db.annotation_alignment_resolutions.insert().values(**row))
            return dict(review_id=row['id'], deduplicated=False, input_fingerprint=fingerprint)

    def read(self, alignment_id):
        with self.service.storage.transaction() as c:
            capture, dependencies = self._capture(c, alignment_id)
            rows = c.execute(select(db.annotation_alignment_resolutions).where(
                self.service._scope(db.annotation_alignment_resolutions),
                db.annotation_alignment_resolutions.c.alignment_id == alignment_id).order_by(
                db.annotation_alignment_resolutions.c.recorded_at, db.annotation_alignment_resolutions.c.id)).mappings()
            result = []
            for row in rows:
                if not row['report']:
                    continue
                cleaned = self._validate_report(row['report'], capture)
                if row['input_fingerprint'] == self._fingerprint(dependencies, row['algorithm_version'], cleaned):
                    result.append(dict(review_id=row['id'], algorithm_version=row['algorithm_version'],
                        input_fingerprint=row['input_fingerprint'], report=cleaned, recorded_at=row['recorded_at']))
            return result
