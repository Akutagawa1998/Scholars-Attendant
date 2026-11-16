"""Versioned derived reviews preserve v1 and fence permissions/deletion/races."""
from copy import deepcopy

import pytest
from sqlalchemy import select, update, func

from research_assistant import storage as db
from research_assistant.annotation_review import AnnotationReviewService
from research_assistant.service import DomainError, ResearchService, canonical, digest
from test_zotero_assets import fixture, pg_url, ingest, metadata, access, forget, REG, ATTACHMENT, ANNOTATION


@pytest.fixture
def review(fixture):
    service, store, *_ = fixture
    receipt = ingest(fixture)
    with service.storage.transaction() as c:
        alignment_id = c.execute(select(db.annotation_alignments.c.id).where(
            service._scope(db.annotation_alignments))).scalar_one()
    adapter = AnnotationReviewService(service, store)
    capture = adapter.capture(alignment_id)
    return fixture, receipt, adapter, capture


def report(capture, **overrides):
    original = capture['v1_report']['matches'][0]
    value = dict(status='resolved_by_local_text', method='local_text_normalization', basis='canonical_literal',
        parameters=dict(engine='combined', engine_version='pypdf6.21.1+pdfium1.0',
            normalization_version='local-whitespace-v2', coordinate_system='pdf_points_bottom_left', tolerance=0.1),
        selected_spans=[{key: original[key] for key in ('text_start', 'text_end', 'literal_sha256')}])
    value.update(overrides)
    return value


def record(review, raw=None, **kwargs):
    _, _, adapter, captured = review
    return adapter.record(captured['alignment_id'], raw or report(captured), algorithm_version='local-review-v2',
                          expected_capture=captured, **kwargs)


def counts(service):
    with service.storage.transaction() as c:
        return {table.name: c.scalar(select(func.count()).select_from(table).where(service._scope(table)))
                for table in (db.sources, db.works, db.versions, db.evidence, db.reading_sessions, db.events,
                    db.source_assets, db.annotation_alignments, db.connector_cursors, db.sync_manifests)}


def test_versioned_review_preserves_original_and_creates_no_source_evidence_or_orphans(review, tmp_path):
    fixture, _, adapter, captured = review
    service = fixture[0]
    before = counts(service)
    files_before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*') if path.is_file())
    original = deepcopy(captured['v1_report'])
    first = record(review)
    assert first['deduplicated'] is False
    assert record(review) == dict(first, deduplicated=True)
    rows = adapter.read(captured['alignment_id'])
    assert len(rows) == 1 and rows[0]['review_id'] == first['review_id']
    assert rows[0]['report']['status'] == 'resolved_by_local_text'
    assert counts(service) == before
    with service.storage.transaction() as c:
        assert c.execute(select(db.annotation_alignments.c.report)).scalar_one() == original
    assert files_before == sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*') if path.is_file())
    serialized = canonical(rows)
    assert 'Synthetic selected quote' not in serialized and 'PRIVATE-ANNOTATION-COMMENT' not in serialized
    assert str(tmp_path) not in serialized and 'literal_text' not in serialized
    assert captured['raw_annotation']['annotationComment'] == 'PRIVATE-ANNOTATION-COMMENT'
    assert len(captured['input_token']) == 64


def test_different_parameters_or_algorithm_create_auditable_versions(review):
    _, _, adapter, capture = review
    first = record(review)
    other = report(capture)
    other['parameters']['tolerance'] = 0.2
    second = record(review, other)
    third = adapter.record(capture['alignment_id'], report(capture), algorithm_version='local-review-v3',
                           expected_capture=capture)
    assert len({first['review_id'], second['review_id'], third['review_id']}) == 3
    assert len({row['input_fingerprint'] for row in adapter.read(capture['alignment_id'])}) == 3


def test_same_input_cannot_silently_overwrite_a_different_result(review):
    record(review)
    raw = report(review[3], status='unresolved', method='none', basis='no_unique_match', selected_spans=[])
    with pytest.raises(DomainError) as error:
        record(review, raw)
    assert error.value.code == 'annotation_review_result_conflict'


def test_caller_cannot_choose_input_fingerprint(review):
    with pytest.raises(DomainError) as error:
        record(review, input_fingerprint='f' * 64)
    assert error.value.code == 'annotation_review_fingerprint_mismatch'
    first = record(review)
    assert record(review, input_fingerprint=first['input_fingerprint'])['deduplicated'] is True


@pytest.mark.parametrize('extra', ['quote', 'comment', 'url', 'path', 'raw_annotation_text'])
def test_report_rejects_private_or_unknown_fields(review, extra):
    raw = report(review[3])
    raw[extra] = 'PRIVATE-UNMANAGED-COPY'
    with pytest.raises(DomainError) as error:
        record(review, raw)
    assert error.value.code == 'invalid_annotation_review'


@pytest.mark.parametrize('change', [
    {'status': 'human_confirmed'}, {'method': 'https://example.invalid'}, {'basis': 'scientific_support'},
    {'parameters': {'path': '/PRIVATE/file.pdf'}}, {'parameters': {'engine_version': 'https://PRIVATE'}},
    {'parameters': {'overlap_threshold': float('nan')}}, {'parameters': {'margin_threshold': True}},
    {'geometry': {'rects': [[0, 0, float('inf'), 10]]}}, {'geometry': {'rects': [[10, 10, 0, 0]]}},
    {'geometry': {'candidate_index': True}}, {'engine_observation': {'engine': 'pdfium', 'text_sha256': 'bad'}},
    {'engine_observation': {'engine': 'pdfium', 'page_index': True}},
    {'geometry': {'rects': [[False, 0, 10, 10]]}},
    {'method': 'none'}, {'basis': 'parser_difference'},
    {'status': 'resolved_by_geometry', 'method': 'geometry_overlap', 'basis': 'geometry_unique'},
])
def test_strict_schema_rejects_nonfinite_invalid_types_and_unproven_authority(review, change):
    with pytest.raises(DomainError):
        record(review, report(review[3], **change))


@pytest.mark.parametrize('span', [
    {'text_start': 0, 'text_end': 999999, 'literal_sha256': '0' * 64},
    {'text_start': 2, 'text_end': 1, 'literal_sha256': '0' * 64},
    {'text_start': 0, 'text_end': 5, 'literal_sha256': '0' * 64},
    {'text_start': False, 'text_end': 5, 'literal_sha256': '0' * 64},
])
def test_selected_span_is_recut_and_hashed_against_canonical_page(review, span):
    with pytest.raises(DomainError):
        record(review, report(review[3], selected_spans=[span]))


def test_geometry_candidate_must_match_selected_canonical_reference(review):
    _, _, adapter, capture = review
    raw = report(capture, status='resolved_by_geometry', method='geometry_overlap', basis='geometry_unique',
        geometry=dict(candidate_index=0, overlap=0.9, runner_up_overlap=0.1, rects=[[0, 0, 10, 10]]))
    first = record(review, raw)
    assert adapter.read(capture['alignment_id'])[0]['review_id'] == first['review_id']
    raw['geometry']['candidate_index'] = 1
    with pytest.raises(DomainError):
        record(review, raw)


def test_parser_difference_records_hash_observation_without_fabricated_canonical_span(review):
    capture = review[3]
    raw = report(capture, status='located_with_parser_difference', method='geometry_and_local_text',
        basis='parser_difference', selected_spans=[], engine_observation=dict(engine='pdfium',
        clip_text_sha256=digest('SYNTHETIC alternate engine order'), char_count=31,
        page_index=capture['canonical_page']['page_index']))
    assert record(review, raw)['deduplicated'] is False
    raw['selected_spans'] = report(capture)['selected_spans']
    with pytest.raises(DomainError):
        record(review, raw)


@pytest.mark.parametrize('target', ['annotation', 'attachment', 'parent', 'pdf'])
def test_all_upstream_access_changes_close_reads_and_block_delayed_record(review, target):
    fixture, receipt, adapter, capture = review
    service = fixture[0]
    record(review)
    source_id = dict(annotation=fixture[4], attachment=fixture[3], parent=fixture[2],
                     pdf=receipt['source_snapshot_id'])[target]
    access(service, source_id, 'inaccessible')
    with pytest.raises(DomainError):
        adapter.read(capture['alignment_id'])
    with pytest.raises(DomainError):
        record(review)


@pytest.mark.parametrize('target', ['annotation', 'attachment', 'parent', 'pdf'])
def test_forget_scrubs_all_versioned_reports_and_prevents_delayed_replay(review, target):
    fixture, receipt, adapter, capture = review
    service = fixture[0]
    record(review)
    source_id = dict(annotation=fixture[4], attachment=fixture[3], parent=fixture[2],
                     pdf=receipt['source_snapshot_id'])[target]
    forget(service, source_id)
    with service.storage.transaction() as c:
        assert c.execute(select(db.annotation_alignment_resolutions.c.report)).scalar_one() == {}
    with pytest.raises(DomainError):
        adapter.read(capture['alignment_id'])
    with pytest.raises(DomainError):
        record(review)


def test_unrelated_epoch_change_rejects_inflight_capture_but_preserves_existing_proof(review):
    fixture, _, adapter, capture = review
    service = fixture[0]
    record(review)
    unrelated = metadata(service, 'UNREL001', 'journalArticle', title='Synthetic unrelated source')
    access(service, unrelated, 'inaccessible')
    with pytest.raises(DomainError) as error:
        record(review)
    assert error.value.code == 'annotation_review_inputs_changed'
    assert len(adapter.read(capture['alignment_id'])) == 1
    fresh = adapter.capture(capture['alignment_id'])
    assert fresh['input_token'] != capture['input_token']
    assert adapter.record(capture['alignment_id'], report(fresh), algorithm_version='local-review-v2',
                          expected_capture=fresh)['deduplicated'] is True


def test_new_annotation_head_and_wrong_parent_relationship_block_old_reviews(review):
    fixture, _, adapter, capture = review
    service = fixture[0]
    record(review)
    metadata(service, ANNOTATION, 'annotation', version=23, parentItem='OTHERPDF',
             annotationType='highlight', annotationText='Synthetic changed annotation')
    with pytest.raises(DomainError):
        adapter.capture(capture['alignment_id'])
    with pytest.raises(DomainError):
        adapter.read(capture['alignment_id'])


def test_wrong_registration_is_rejected_even_with_matching_attachment_key(review):
    fixture, _, adapter, capture = review
    service = fixture[0]
    other = metadata(service, 'OTHERAN1', 'annotation', registration=dict(REG, source_instance_id='other-library'),
        parentItem=ATTACHMENT, annotationText='Synthetic selected quote.', annotationType='highlight')
    with service.storage.transaction() as c:
        c.execute(update(db.annotation_alignments).where(db.annotation_alignments.c.id == capture['alignment_id']).values(
            annotation_snapshot_id=other))
    with pytest.raises(DomainError):
        adapter.capture(capture['alignment_id'])


def test_cross_scope_alignment_is_unavailable(review):
    fixture, _, _, capture = review
    service, store, *_ = fixture
    other = ResearchService(service.storage.engine.url.render_as_string(hide_password=False),
                            owner_id=service.owner_id, scope_id='another-private-scope')
    other.initialize()
    try:
        adapter = AnnotationReviewService(other, store)
        with pytest.raises(DomainError):
            adapter.capture(capture['alignment_id'])
        with pytest.raises(DomainError):
            adapter.read(capture['alignment_id'])
        with pytest.raises(DomainError):
            adapter.record(capture['alignment_id'], report(capture), algorithm_version='local-review-v2', expected_capture=capture)
    finally:
        other.storage.engine.dispose()
