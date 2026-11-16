"""Cached catalog operator uses only synthetic SQLite metadata and private temp paths."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from research_assistant import storage as db
from research_assistant.assets import AssetError
from research_assistant.operators import configure_knowledge_library as operator
from research_assistant.service import DomainError, ResearchService, canonical

NOTION = dict(provider='notion', source_instance_id='synthetic-catalog',
              namespace='collection://11111111-2222-4333-8444-555555555555')
ZOTERO = dict(provider='zotero', source_instance_id='synthetic-catalog', namespace='users/12345')
ARGS = dict(notion_url='https://www.notion.so/11111111222243338444555555555555',
            zotero_url='https://api.zotero.org/users/12345/items',
            verified_at='2026-10-26T00:00:00Z', notion_namespace=NOTION['namespace'],
            zotero_namespace=ZOTERO['namespace'])


def seed(service, key, *, provider='zotero', kind='preprint', version='123', **fields):
    registration = ZOTERO if provider == 'zotero' else NOTION
    title = 'PRIVATE-SYNTHETIC-TITLE'
    raw = (dict(key=key, version=version,
                data=dict(key=key, version=version, itemType=kind, title=title, **fields))
           if provider == 'zotero' else dict(title=title, **fields))
    path = ['data', 'title'] if provider == 'zotero' else ['title']
    return service.execute('sources.ingest', dict(**registration, external_id=key, external_version=version,
        source_url='https://example.invalid', raw_payload=raw, observed_at='2026-10-26T00:00:00Z',
        occurred_at=None, completeness='partial', missing_parts=['bounded_metadata'], change='upsert',
        passages=[dict(text=title, json_path=path, section='title')], idempotency_key=uuid4().hex),
        trusted_capture=True)['data']['source_snapshot_id']


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    service = ResearchService('sqlite:///' + str(tmp_path/'synthetic.sqlite3'))
    service.initialize()
    for registration in (NOTION, ZOTERO):
        service.execute('sources.register', dict(**registration, idempotency_key=uuid4().hex), trusted_capture=True)
    monkeypatch.setattr('httpx.Client.request', lambda *a, **k: pytest.fail('operator called a remote provider'))
    yield service
    service.storage.engine.dispose()


def test_only_paper_metadata_and_collection_pages_bind_without_body_refresh(catalog):
    a = seed(catalog, 'PAPER001', url='https://arxiv.org/abs/2601.01234')
    seed(catalog, 'PAPER002', kind='journalArticle', DOI='10.1234/synthetic')
    seed(catalog, 'PAPER003', kind='conferencePaper', url='https://arxiv.org/abs/2601.56789v3')
    seed(catalog, 'BOOK0001', kind='book')
    seed(catalog, 'BOOK0002', kind='bookSection')
    seed(catalog, 'DOC00001', kind='document')
    seed(catalog, 'NOTE0001', kind='note')
    seed(catalog, 'ATTACH01', kind='attachment')
    seed(catalog, 'notion-page', provider='notion',
         text='<properties>{"Paper URL":"https://arxiv.org/abs/2601.01234"}</properties><content>PRIVATE-NOTE</content>')
    with catalog.storage.transaction() as c:
        original = [dict(r) for r in c.execute(select(db.sources)).mappings()]
    result = operator.configure_cached_library(catalog, **ARGS)
    assert result['status'] == 'ok' and result['configured']
    assert result['processed_by_provider'] == {'zotero_processed': 3, 'notion_processed': 1}
    assert result['selection']['other_publications_skipped_book'] == 1
    assert result['selection']['other_publications_skipped_bookSection'] == 1
    assert result['selection']['other_publications_skipped_document'] == 1
    assert result['selection']['zotero_nonpapers_skipped_note'] == 1
    assert result['selection']['zotero_nonpapers_skipped_attachment'] == 1
    assert result['scientific_version_states'] == {'unknown': 3, 'explicit': 1}
    assert result['added']['sources'] == result['added']['reading_sessions'] == 0
    assert result['added']['papers'] == result['added']['paper_versions'] == 3
    assert result['added']['catalog_bindings'] == 4
    assert result['remote_calls'] == result['llm_calls'] == result['fulltext_fetches'] == 0
    assert result['remote_write'] is False and result['reading_mutated'] is False
    with catalog.storage.transaction() as c:
        assert [dict(r) for r in c.execute(select(db.sources)).mappings()] == original
        version = c.execute(select(db.versions).where(db.versions.c.source_version_id == a)).mappings().one()
        assert version['version'] == 'unknown'
    assert 'PRIVATE' not in canonical(result) and 'literal_text' not in canonical(result)


def test_repeated_verification_reuses_receipts_and_creates_no_binding_version_or_event(catalog):
    seed(catalog, 'PAPER001', url='https://arxiv.org/abs/2601.01234')
    first = operator.configure_cached_library(catalog, **ARGS)
    with catalog.storage.transaction() as c:
        events = c.scalar(select(func.count()).select_from(db.events))
    second = operator.configure_cached_library(catalog, **ARGS)
    assert first['outcomes'] == {'bound': 1}
    assert second['outcomes'] == {'reused': 1}
    assert all(value == 0 for value in second['added'].values())
    with catalog.storage.transaction() as c:
        assert c.scalar(select(func.count()).select_from(db.events)) == events


def test_only_current_accessible_unforgotten_heads_are_selected(catalog):
    seed(catalog, 'PAPER001', version='1')
    newest = seed(catalog, 'PAPER001', version='2')
    revoked = seed(catalog, 'PAPER002')
    forgotten = seed(catalog, 'PAPER003')
    with catalog.storage.transaction() as c:
        revision = catalog._row(c, db.sources, revoked)['revision']
    catalog.execute('sources.set_access', dict(source_snapshot_id=revoked, state='inaccessible',
        expected_revision=revision, idempotency_key=uuid4().hex))
    catalog.execute('memory.forget', dict(target_ids=[forgotten], scope='memory_and_source_snapshots',
        idempotency_key=uuid4().hex))
    result = operator.configure_cached_library(catalog, **ARGS)
    assert result['outcomes'] == {'bound': 1}
    with catalog.storage.transaction() as c:
        assert c.execute(select(db.versions.c.source_version_id)).scalar_one() == newest


def test_ambiguous_or_missing_registration_fails_before_configuration(catalog):
    duplicate = {**NOTION, 'source_instance_id': 'second-connector'}
    catalog.execute('sources.register', dict(**duplicate, idempotency_key=uuid4().hex), trusted_capture=True)
    with pytest.raises(operator.KnowledgeLibraryError, match='knowledge_library_registration_required'):
        operator.configure_cached_library(catalog, **ARGS)
    with catalog.storage.transaction() as c:
        assert c.scalar(select(func.count()).select_from(db.catalog_defaults)) == 0
        registration = c.execute(select(db.connector_registrations.c.id).where(
            db.connector_registrations.c.source_instance_id == NOTION['source_instance_id'],
            db.connector_registrations.c.provider == 'notion')).scalar_one()
    assert operator.configure_cached_library(catalog, **ARGS, notion_registration_id=registration)['configured']


def test_individual_domain_errors_are_counted_with_fixed_safe_codes(catalog, monkeypatch):
    a = seed(catalog, 'PAPER001')
    b = seed(catalog, 'PAPER002')
    real = catalog.execute
    def fail_some(operation, payload, **kwargs):
        if operation == 'catalog.bind' and payload['source_snapshot_id'] == a:
            raise DomainError('failed', 'PRIVATE-UNSAFE-CODE', 'PRIVATE-SECRET-BODY')
        return real(operation, payload, **kwargs)
    monkeypatch.setattr(catalog, 'execute', fail_some)
    result = operator.configure_cached_library(catalog, **ARGS)
    assert result['status'] == 'partial'
    assert result['errors'] == {'catalog_bind_unavailable': 1} and result['outcomes'] == {'bound': 1}
    assert 'PRIVATE' not in canonical(result)


def test_timezone_required_before_catalog_mutation(catalog):
    with pytest.raises(operator.KnowledgeLibraryError, match='knowledge_library_verification_time_invalid'):
        operator.configure_cached_library(catalog, **{**ARGS, 'verified_at': '2026-10-26T00:00:00'})
    with catalog.storage.transaction() as c:
        assert c.scalar(select(func.count()).select_from(db.catalog_defaults)) == 0


def cli_args(config, report):
    return ['--runtime-config', str(config), '--notion-url', ARGS['notion_url'],
            '--zotero-url', ARGS['zotero_url'], '--verified-at', ARGS['verified_at'],
            '--notion-namespace', ARGS['notion_namespace'], '--zotero-namespace', ARGS['zotero_namespace'],
            '--report', str(report)]


def test_main_writes_only_safe_report_under_backing_artifacts(catalog, tmp_path, monkeypatch, capsys):
    seed(catalog, 'PAPER001')
    artifacts = tmp_path/'synthetic-backing'/'artifacts'
    config = tmp_path/'runtime.json'
    report = artifacts/'safe-report.json'
    runtime = dict(database_url='postgresql+psycopg://synthetic:PRIVATE-PASSWORD@127.0.0.1/synthetic')
    monkeypatch.setattr(operator, '_runtime', lambda _: (runtime, artifacts))
    monkeypatch.setattr(operator, 'ResearchService', lambda *a, **k: catalog)
    assert operator.main(cli_args(config, report)) == 0
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out) == json.loads(report.read_text())
    assert 'PRIVATE' not in output.out and 'PRIVATE' not in report.read_text()


def test_unmounted_storage_fails_before_service_or_report_creation(tmp_path, monkeypatch, capsys):
    config = tmp_path/'runtime.json'
    config.write_text(json.dumps(dict(database_url='postgresql+psycopg://synthetic:PRIVATE@localhost/synthetic')))
    report = tmp_path/'synthetic-backing'/'artifacts'/'no-report.json'
    def unavailable(*a, **k): raise AssetError('external_storage_unavailable')
    monkeypatch.setattr(operator, 'select_asset_root', unavailable)
    monkeypatch.setattr(operator, 'ResearchService', lambda *a, **k: pytest.fail('unguarded DB connection'))
    assert operator.main(cli_args(config, report)) == 1
    assert not report.exists()
    assert json.loads(capsys.readouterr().err)['error_code'] == 'external_storage_unavailable'


@pytest.mark.parametrize('kind', ['outside', 'traversal', 'symlink'])
def test_report_path_cannot_escape_registered_backing_artifacts(tmp_path, kind):
    artifacts = tmp_path/'backing'/'artifacts';artifacts.mkdir(parents=True)
    if kind == 'outside': target = tmp_path/'outside.json'
    elif kind == 'traversal': target = artifacts/'..'/'outside.json'
    else:
        (artifacts/'alias').symlink_to(tmp_path, target_is_directory=True)
        target = artifacts/'alias'/'outside.json'
    with pytest.raises(operator.KnowledgeLibraryError, match='knowledge_library_report_path_invalid'):
        operator._report_path(target, artifacts)
