"""Attachment bytes remain source-bound and do not become scientific reading."""
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, func, update

from research_assistant import storage as db
from research_assistant.assets import ArtifactStore
from research_assistant.asset_service import read_source_asset, purge_pending_assets
from research_assistant.service import ResearchService, DomainError
from research_assistant.zotero_assets import ZoteroPdfIngestor
import research_assistant.zotero_assets as module
from test_assets import pdf
from test_postgres import pg_url

REG = dict(provider='zotero', source_instance_id='synthetic-library', namespace='user:fixture')
PARENT, ATTACHMENT, ANNOTATION = 'PARENT01', 'ATTACH01', 'ANNOT001'
DATA = pdf('Synthetic selected quote.', 'Second synthetic page.')
MD5 = hashlib.md5(DATA, usedforsecurity=False).hexdigest()
PROVENANCE = dict(attachment_key=ATTACHMENT, attachment_version=22, attachment_md5=MD5)


def metadata(service, key, kind, *, version=22, registration=None, **fields):
    registration = registration or REG
    service.execute('sources.register', dict(registration, idempotency_key=str(uuid4())), trusted_capture=True)
    raw = dict(key=key, version=version, data=dict(key=key, version=version, itemType=kind, **fields))
    result = service.execute('sources.ingest', dict(registration, external_id=key,
        external_version=str(version), source_url='https://example.invalid/synthetic', raw_payload=raw,
        observed_at=datetime.now(timezone.utc).isoformat(), occurred_at=None, completeness='complete',
        missing_parts=[], change='upsert', passages=[dict(text=kind, json_path=['data', 'itemType'], section='metadata')],
        idempotency_key=str(uuid4())), trusted_capture=True)
    return result['data']['source_snapshot_id']


@pytest.fixture(params=['sqlite', 'postgres'])
def fixture(request, tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'zotero-assets.sqlite3') if request.param == 'sqlite' else request.getfixturevalue('pg_url')
    service = ResearchService(url, owner_id='synthetic-zotero-assets', scope_id='private')
    service.initialize()
    parent = metadata(service, PARENT, 'journalArticle', title='Synthetic parent')
    attachment = metadata(service, ATTACHMENT, 'attachment', parentItem=PARENT,
        contentType='application/pdf', linkMode='imported_file', md5=MD5)
    annotation = metadata(service, ANNOTATION, 'annotation', parentItem=ATTACHMENT,
        annotationType='highlight', annotationText='Synthetic selected quote.',
        annotationComment='PRIVATE-ANNOTATION-COMMENT', annotationPageLabel='i',
        annotationPosition=json.dumps(dict(pageIndex=0, rects=[[0, 0, 10, 10]])))
    with ArtifactStore(tmp_path / 'blobs') as store:
        yield service, store, parent, attachment, annotation
    service.storage.engine.dispose()


def ingest(fixture, *, provenance=None, data=DATA, attachment=None, acquisition_method='zotero_api_file'):
    service, store, _, snapshot, _ = fixture
    return ZoteroPdfIngestor(service, store).ingest(data, attachment_snapshot_id=attachment or snapshot,
        provenance=PROVENANCE if provenance is None else provenance, acquisition_method=acquisition_method)


def count(service, table):
    with service.storage.transaction() as c:
        return c.scalar(select(func.count()).select_from(table).where(service._scope(table)))


def access(service, source_id, state):
    with service.storage.transaction() as c:
        revision = service._row(c, db.sources, source_id)['revision']
    return service.execute('sources.set_access', dict(source_snapshot_id=source_id, expected_revision=revision,
        state=state, idempotency_key=str(uuid4())), trusted_capture=True)


def forget(service, source_id):
    return service.execute('memory.forget', dict(target_ids=[source_id], scope='memory_and_source_snapshots',
                                                idempotency_key=str(uuid4())))


def test_canonical_pdf_has_literal_pages_and_stripped_alignment_without_reading(fixture):
    service, store, _, attachment, annotation = fixture
    before = {table.name: count(service, table) for table in (db.works, db.versions, db.reading_sessions,
        db.sync_manifests, db.connector_cursors)}
    result = ingest(fixture)
    assert result['deduplicated'] is False and result['page_count'] == 2
    assert result['alignment_counts'] == {'exact': 1}
    source_id = result['source_snapshot_id']
    assert read_source_asset(service, store, source_id) == DATA
    response = service.execute('sources.get_evidence', dict(source_snapshot_id=source_id))
    assert len(response['data']['passages']) == 2 and len(response['citations']) == 2
    with service.storage.transaction() as c:
        source = service._row(c, db.sources, source_id)
        assert source['kind'] == 'zotero_attachment_pdf' and source['source_url'] == ''
        link = c.execute(select(db.attachment_asset_links)).mappings().one()
        assert link['metadata_snapshot_id'] == attachment
        assert link['provenance'] == dict(PROVENANCE, acquisition_method='zotero_api_file')
        row = c.execute(select(db.annotation_alignments)).mappings().one()
        assert row['annotation_snapshot_id'] == annotation and row['report']['match_status'] == 'exact'
        encoded = json.dumps(row['report'])
        assert 'Synthetic selected quote' not in encoded and 'PRIVATE' not in encoded
        assert 'literal_text' not in encoded and 'annotationComment' not in encoded
        assert row['report']['matches'][0]['text_start'] == 0
        asset = c.execute(select(db.source_assets)).mappings().one()
        assert asset['sha256'] == hashlib.sha256(DATA).hexdigest() and source['sha256'] != asset['sha256']
    assert before == {table.name: count(service, table) for table in (db.works, db.versions, db.reading_sessions,
        db.sync_manifests, db.connector_cursors)}
    original_counts = [count(service, table) for table in (db.sources, db.evidence, db.source_assets,
        db.attachment_asset_links, db.annotation_alignments)]
    repeat = ingest(fixture)
    assert repeat == dict(result, deduplicated=True)
    assert original_counts == [count(service, table) for table in (db.sources, db.evidence, db.source_assets,
        db.attachment_asset_links, db.annotation_alignments)]


@pytest.mark.parametrize('replacement', [
    {'attachment_key': 'OTHER001'}, {'attachment_version': 23}, {'attachment_md5': '0' * 32},
    {'attachment_version': True}, {'url': 'https://example.invalid/?secret=PRIVATE'},
])
def test_wrong_provenance_or_extra_fields_fail_before_blob_publication(fixture, replacement, tmp_path):
    service, store, *_ = fixture
    with pytest.raises(DomainError):
        ingest(fixture, provenance=dict(PROVENANCE, **replacement))
    assert count(service, db.source_assets) == 0
    assert not list((tmp_path / 'blobs').rglob('*.pdf'))


@pytest.mark.parametrize('fields', [
    {'contentType': 'text/plain'}, {'linkMode': 'linked_file'}, {'parentItem': 'MISSING1'},
])
def test_invalid_attachment_contract_is_rejected(fixture, fields):
    service, _, *_ = fixture
    params = dict(parentItem=PARENT, contentType='application/pdf', linkMode='imported_file', md5=MD5)
    params.update(fields)
    snapshot = metadata(service, ATTACHMENT, 'attachment', version=23, **params)
    with pytest.raises(DomainError):
        ingest(fixture, attachment=snapshot, provenance=dict(PROVENANCE, attachment_version=23))


@pytest.mark.parametrize('target_index', [2, 3])
def test_revoked_metadata_or_parent_closes_bytes_citations_and_export(fixture, target_index):
    service, store, *_ = fixture
    result = ingest(fixture)
    source_id = result['source_snapshot_id']
    access(service, fixture[target_index], 'inaccessible')
    with pytest.raises(DomainError):
        read_source_asset(service, store, source_id)
    with pytest.raises(DomainError):
        service.execute('sources.get_evidence', dict(source_snapshot_id=source_id))
    exported = service.execute('export', {})['data']
    assert source_id not in {row['id'] for row in exported['source_versions']}
    assert not any(row['source_version_id'] == source_id for row in exported['evidence_spans'])
    access(service, fixture[target_index], 'accessible')
    assert read_source_asset(service, store, source_id) == DATA


def test_changed_attachment_head_hides_old_pdf_and_requires_new_binding(fixture):
    service, store, *_ = fixture
    original = ingest(fixture)
    latest = metadata(service, ATTACHMENT, 'attachment', version=23, parentItem=PARENT,
        contentType='application/pdf', linkMode='imported_file', md5=MD5)
    with pytest.raises(DomainError):
        ingest(fixture)
    with pytest.raises(DomainError):
        read_source_asset(service, store, original['source_snapshot_id'])
    fresh = ingest(fixture, attachment=latest, provenance=dict(PROVENANCE, attachment_version=23))
    assert fresh['source_snapshot_id'] != original['source_snapshot_id']
    assert read_source_asset(service, store, fresh['source_snapshot_id']) == DATA


def test_changed_parent_head_requires_new_binding_context_without_new_blob(fixture, tmp_path):
    service, store, *_ = fixture
    original = ingest(fixture)
    metadata(service, PARENT, 'journalArticle', version=23, title='Synthetic updated parent')
    with pytest.raises(DomainError):
        read_source_asset(service, store, original['source_snapshot_id'])
    fresh = ingest(fixture)
    assert fresh['deduplicated'] is False and fresh['source_snapshot_id'] != original['source_snapshot_id']
    assert read_source_asset(service, store, fresh['source_snapshot_id']) == DATA
    assert len(list((tmp_path / 'blobs').rglob('*.pdf'))) == 1
    assert ingest(fixture) == dict(fresh, deduplicated=True)


def test_md5_case_is_normalized_without_mutating_operator_provenance(fixture):
    upper = dict(PROVENANCE, attachment_md5=MD5.upper())
    assert ingest(fixture, provenance=upper)['alignment_counts'] == {'exact': 1}
    assert upper['attachment_md5'] == MD5.upper()


def test_local_origin_is_safe_and_existing_link_keeps_first_acquisition_method(fixture):
    service, store, *_ = fixture
    local = dict(PROVENANCE, acquisition_method='zotero_local_storage')
    first = ingest(fixture, provenance=local, acquisition_method='zotero_local_storage')
    assert read_source_asset(service, store, first['source_snapshot_id']) == DATA
    assert ingest(fixture)['deduplicated'] is True
    with service.storage.transaction() as c:
        provenance = c.execute(select(db.attachment_asset_links.c.provenance)).scalar_one()
        assert provenance == local
        assert set(provenance) == set(PROVENANCE) | {'acquisition_method'}


def test_legacy_three_field_link_provenance_remains_readable(fixture):
    service, store, *_ = fixture
    result = ingest(fixture)
    with service.storage.transaction() as c:
        c.execute(update(db.attachment_asset_links).values(provenance=PROVENANCE))
    assert read_source_asset(service, store, result['source_snapshot_id']) == DATA


@pytest.mark.parametrize('method', [None, [], 'unknown', '/PRIVATE/Zotero/storage/file.pdf'])
def test_invalid_acquisition_method_rejected_before_persistence(fixture, method, tmp_path):
    service, *_ = fixture
    with pytest.raises(DomainError):
        ingest(fixture, acquisition_method=method)
    assert count(service, db.source_assets) == 0 and not list((tmp_path / 'blobs').rglob('*.pdf'))


@pytest.mark.parametrize('method', ['unknown', [], None, 'zotero_local_storage'])
def test_invalid_or_conflicting_provenance_origin_is_rejected(fixture, method):
    with pytest.raises(DomainError):
        ingest(fixture, provenance=dict(PROVENANCE, acquisition_method=method))


@pytest.mark.parametrize('target_index', [2, 3])
def test_forget_parent_or_metadata_scrubs_pdf_alignment_and_replay(fixture, target_index):
    service, store, *_ = fixture
    result = ingest(fixture)
    forget(service, fixture[target_index])
    with service.storage.transaction() as c:
        assert service._row(c, db.sources, result['source_snapshot_id'])['content'] == ''
        assert c.execute(select(db.annotation_alignments.c.report)).scalar_one() == {}
        assert c.execute(select(db.attachment_asset_links.c.provenance)).scalar_one() == {}
        assert c.execute(select(db.source_assets.c.state)).scalar_one() == 'purge_pending'
    with pytest.raises(DomainError):
        ingest(fixture)
    assert purge_pending_assets(service, store)['purged'] == 1
    with pytest.raises(DomainError):
        read_source_asset(service, store, result['source_snapshot_id'])


def test_annotation_forget_clears_report_without_deleting_independent_pdf(fixture):
    service, store, _, _, annotation = fixture
    result = ingest(fixture)
    forget(service, annotation)
    with service.storage.transaction() as c:
        assert c.execute(select(db.annotation_alignments.c.report)).scalar_one() == {}
    assert read_source_asset(service, store, result['source_snapshot_id']) == DATA
    assert ingest(fixture)['alignment_counts'] == {}
    assert count(service, db.annotation_alignments) == 1


def test_pdf_forget_blocks_same_byte_replay_before_put(fixture, monkeypatch):
    service, store, *_ = fixture
    result = ingest(fixture)
    forget(service, result['source_snapshot_id'])
    called = []
    monkeypatch.setattr(store, 'put_bytes', lambda data: called.append(True))
    with pytest.raises(DomainError, match='unavailable'):
        ingest(fixture)
    assert called == []
    with service.storage.transaction() as c:
        assert c.execute(select(db.annotation_alignments.c.report)).scalar_one() == {}


def test_parse_time_access_change_is_rechecked_before_put(fixture, monkeypatch):
    service, store, parent, *_ = fixture
    original = module.extract_pdf
    def change_during_parse(data):
        access(service, parent, 'inaccessible')
        return original(data)
    monkeypatch.setattr(module, 'extract_pdf', change_during_parse)
    called = []
    monkeypatch.setattr(store, 'put_bytes', lambda data: called.append(True))
    with pytest.raises(DomainError):
        ingest(fixture)
    assert called == [] and count(service, db.source_assets) == 0


def test_annotation_membership_and_scope_are_not_inferred_from_attachment_key(fixture):
    service, store, *_ = fixture
    other_reg = dict(REG, source_instance_id='other-synthetic-library')
    metadata(service, 'OTHERAN1', 'annotation', registration=other_reg, parentItem=ATTACHMENT,
        annotationText='Synthetic selected quote.', annotationType='highlight', annotationPosition='{"pageIndex":0}')
    metadata(service, 'OTHERAN2', 'annotation', parentItem='OTHERPDF', annotationText='Synthetic selected quote.',
        annotationType='highlight', annotationPosition='{"pageIndex":0}')
    result = ingest(fixture)
    assert result['alignment_counts'] == {'exact': 1}
    other = ResearchService(service.storage.engine.url.render_as_string(hide_password=False),
                            owner_id=service.owner_id, scope_id='another-private-scope')
    other.initialize()
    try:
        with pytest.raises(DomainError):
            read_source_asset(other, store, result['source_snapshot_id'])
        with pytest.raises(DomainError):
            ZoteroPdfIngestor(other, store).ingest(DATA, attachment_snapshot_id=fixture[3], provenance=PROVENANCE)
    finally:
        other.storage.engine.dispose()


def test_transaction_failure_cleans_unregistered_blob(fixture, monkeypatch, tmp_path):
    service, store, *_ = fixture
    def fail(*args):
        raise DomainError('failed', 'synthetic_failure', 'Synthetic failure')
    monkeypatch.setattr(service, '_register_source_asset', fail)
    with pytest.raises(DomainError):
        ingest(fixture)
    assert count(service, db.source_assets) == 0 and count(service, db.attachment_asset_links) == 0
    assert not list((tmp_path / 'blobs').rglob('*.pdf'))
