"""Synthetic identity proofs and catalog permission fences; zero providers."""
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, func, update
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from research_assistant import storage as db
from research_assistant.knowledge_catalog import parse_identity
from research_assistant.service import ResearchService, DomainError
from test_postgres import pg_url

NAMESPACE = 'collection://11111111-2222-4333-8444-555555555555'
ZOTERO = dict(provider='zotero', source_instance_id='synthetic-catalog', namespace='users/12345')
NOTION = dict(provider='notion', source_instance_id='synthetic-catalog', namespace=NAMESPACE)


def call(service, operation, **payload):
    return service.execute(operation, dict(payload, idempotency_key=uuid4().hex), trusted_capture=True)['data']


@pytest.fixture(params=['sqlite', 'postgres'])
def catalog(request, tmp_path):
    url = 'sqlite:///' + str(tmp_path/'catalog.sqlite3') if request.param == 'sqlite' else request.getfixturevalue('pg_url')
    service = ResearchService(url, owner_id='synthetic-catalog', scope_id='private');service.initialize()
    ids = [call(service, 'sources.register', **registration)['registration_id'] for registration in (ZOTERO, NOTION)]
    links = [dict(provider='zotero', namespace=ZOTERO['namespace'], url='https://api.zotero.org/users/12345/items',
        checked_at='2026-10-26T00:00:00Z', valid=True), dict(provider='notion', namespace=NAMESPACE,
        url='https://www.notion.so/11111111222243338444555555555555', checked_at='2026-10-26T00:00:00Z', valid=True)]
    call(service, 'catalog.configure', registration_ids=ids, verified_links=links)
    yield service, ids, links
    service.storage.engine.dispose()


def seed(service, key, *, provider='zotero', title='Synthetic paper', version='987654', **fields):
    registration = ZOTERO if provider == 'zotero' else NOTION
    raw = dict(key=key, version=version, data=dict(key=key, version=version, itemType='journalArticle', title=title) | fields) if provider == 'zotero' else dict(title=title, **fields)
    path = ['data', 'title'] if provider == 'zotero' else ['title']
    return call(service, 'sources.ingest', **registration, external_id=key, external_version=version,
        source_url='https://example.invalid/synthetic', raw_payload=raw, observed_at=datetime.now(timezone.utc).isoformat(),
        occurred_at=None, completeness='complete', missing_parts=[], change='upsert',
        passages=[dict(text=title, json_path=path, section='title')])['source_snapshot_id']


def bind(service, source_id, **extra):
    return call(service, 'catalog.bind', source_snapshot_id=source_id, **extra)


def count(service, table):
    with service.storage.transaction() as connection:
        return connection.scalar(select(func.count()).select_from(table).where(service._scope(table)))


@pytest.mark.parametrize('value,expected', [
    ('https://doi.org/10.12345/ABC', [('doi', '10.12345/abc', None)]),
    ('DOI:10.1234/alpha(beta)', [('doi', '10.1234/alpha(beta)', None)]),
    ('https://arxiv.org/abs/2601.01234v3', [('arxiv', '2601.01234', 'v3')]),
    ('https://arxiv.org/pdf/2601.01234v2.pdf', [('arxiv', '2601.01234', 'v2')]),
    ('arXiv:cs/9901001v2', [('arxiv', 'cs/9901001', 'v2')]),
    ('https://arxiv.org/abs/2601.01234', [('arxiv', '2601.01234', None)]),
    ('https://doi.org/10.48550/arXiv.2601.01234v2', [('doi', '10.48550/arxiv.2601.01234v2', None), ('arxiv', '2601.01234', 'v2')]),
    ('See https://arxiv.org/abs/2601.01234v2 for references', []),
    ('https://arxiv.org.evil.invalid/abs/2601.01234v2', []),
    ('https://arxiv.org/abs/2601.01234v2?token=private', []),
    ('https://user:secret@arxiv.org/abs/2601.01234v2', []),
    ('https://[bad/abs/2601.01234v2', []),
    ('https://arxiv.org:wrong/abs/2601.01234v2', []),
    ('https://arxiv.org:8888/abs/2601.01234v2', []),
    ('2600.01234v1', []), ('2601.01234v0', []), ('987654', []),
])
def test_whole_strong_identifier_parser_never_scans_prose_or_provider_revision(value, expected):
    assert [(item['scheme'], item['value'], item['scientific_version']) for item in parse_identity(value)] == expected


def test_catalog_migration_matches_schema(catalog):
    service, *_ = catalog
    with service.storage.engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={'include_object': lambda obj, name, kind, reflected, other:
            name not in {'evidence_embeddings', 'ix_evidence_spans_fts'}})
        assert compare_metadata(context, db.metadata) == []


def test_doi_is_work_identity_with_unknown_scientific_version_and_unread(catalog):
    service, *_ = catalog
    source_id = seed(service, 'PAPER001', DOI='10.1234/example')
    result = bind(service, source_id)
    assert result['identity_state'] == 'strong' and result['scientific_version_state'] == 'unknown'
    assert result['scientific_version'] is None and result['initial_read_status'] == 'unread'
    assert result['reading_mutated'] is False and count(service, db.reading_sessions) == 0
    with service.storage.transaction() as c:
        version = service._row(c, db.versions, result['paper_version_id'])
        assert version['version'] == 'unknown' and '987654' not in version['identifier']
        assert service._row(c, db.sources, version['source_version_id'])['kind'] == 'zotero'
    assert result['bindings'][0]['role'] == 'metadata'


def test_explicit_arxiv_revision_only_and_title_does_not_merge_unknown_work(catalog):
    service, *_ = catalog
    a = bind(service, seed(service, 'PAPER001', url='https://arxiv.org/abs/2601.01234v2'))
    b = bind(service, seed(service, 'PAPER002'))
    assert a['scientific_version_state'] == 'explicit' and a['scientific_version'] == 'v2'
    assert b['identity_state'] == 'provisional' and b['scientific_version_state'] == 'unknown'
    assert a['paper_id'] != b['paper_id']
    assert count(service, db.reading_sessions) == 0


def test_cross_provider_doi_aliases_share_work_and_unknown_version(catalog):
    service, *_ = catalog
    a = bind(service, seed(service, 'PAPER001', DOI='10.1234/shared'))
    b = bind(service, seed(service, 'notion-page', provider='notion', title='Synthetic note',
        text='<properties>{"Paper URL":"https://doi.org/10.1234/shared"}</properties>\n<content>Existing note.</content>'))
    assert a['paper_id'] == b['paper_id'] and a['paper_version_id'] == b['paper_version_id']
    assert {binding['role'] for binding in b['bindings']} == {'metadata', 'note'}
    assert count(service, db.sources) == 2 and count(service, db.versions) == 1 and count(service, db.reading_sessions) == 0
    with service.storage.transaction() as c:
        aliases = list(c.execute(select(db.catalog_aliases).where(service._scope(db.catalog_aliases))).mappings())
        assert all(service._catalog_alias_live(c, alias) for alias in aliases)
        proof = next(alias['proof'] for alias in aliases if alias['source_snapshot_id'] == b['source_snapshot_id'])
        assert proof['kind'] == 'native_properties_json' and proof['property'] == 'Paper URL'


@pytest.mark.parametrize('text', [
    '<properties>{"Source URL":"https://arxiv.org/abs/2601.01234v2"}</properties><content>References https://arxiv.org/abs/2601.01234v2</content>',
    '<content>References <properties>{"Paper URL":"https://arxiv.org/abs/2601.01234v2"}</properties></content>',
    '<properties>{"Paper URL":"https://arxiv.org/abs/2601.01234v2", "Paper URL":"https://arxiv.org/abs/2601.99999v1"}</properties>',
    '<properties>{"Paper URL":"https://arxiv.org/abs/2601.01234v2"}</properties><properties>{}</properties>',
])
def test_notion_body_source_url_and_ambiguous_properties_do_not_establish_identity(catalog, text):
    service, *_ = catalog
    result = bind(service, seed(service, 'notion-page', provider='notion', text=text))
    assert result['identity_state'] == 'provisional' and result['scientific_version_state'] == 'unknown'


def test_bridge_between_two_existing_works_is_conflict_without_merge(catalog):
    service, *_ = catalog
    a = bind(service, seed(service, 'PAPER001', DOI='10.1234/a'))
    b = bind(service, seed(service, 'PAPER002', url='https://arxiv.org/abs/2601.01234v2'))
    c = bind(service, seed(service, 'PAPER003', DOI='10.1234/a', url='https://arxiv.org/abs/2601.01234v2'))
    assert len({entry['paper_id'] for entry in (a, b, c)}) == 3
    assert c['identity_state'] == 'conflict' and c['scientific_version_state'] == 'unknown'
    assert c['conflicts'][0]['work_ids'] == sorted([a['paper_id'], b['paper_id']])


def test_stale_or_revoked_alias_cannot_join_new_accessible_source(catalog):
    service, *_ = catalog
    source = seed(service, 'PAPER001', DOI='10.1234/private')
    a = bind(service, source)
    with service.storage.transaction() as c:
        revision = service._row(c, db.sources, source)['revision']
    call(service, 'sources.set_access', source_snapshot_id=source, expected_revision=revision, state='inaccessible')
    b = bind(service, seed(service, 'PAPER002', DOI='10.1234/private'))
    assert a['paper_id'] != b['paper_id']
    with service.storage.transaction() as c:
        assert service._catalog_sources(c, a['paper_version_id']) == []


def test_current_head_only_and_repeat_binding_has_no_duplicate_records(catalog):
    service, *_ = catalog
    old = seed(service, 'PAPER001', DOI='10.1234/a', version='10')
    a = bind(service, old)
    new = seed(service, 'PAPER001', DOI='10.1234/a', version='11', title='Synthetic revised title')
    with pytest.raises(DomainError, match='Catalog input'):
        bind(service, old)
    b = bind(service, new)
    before = [count(service, table) for table in (db.works, db.versions, db.catalog_bindings, db.catalog_aliases)]
    repeated = bind(service, new)
    assert b['paper_version_id'] == a['paper_version_id'] and repeated['deduplicated']
    assert before == [count(service, table) for table in (db.works, db.versions, db.catalog_bindings, db.catalog_aliases)]
    assert len(b['bindings']) == 1 and b['bindings'][0]['source_snapshot_id'] == new


def test_cross_scope_and_unconfigured_registration_are_not_catalog_authority(catalog):
    service, *_ = catalog
    other = dict(ZOTERO, namespace='users/23456')
    call(service, 'sources.register', **other)
    sid = call(service, 'sources.ingest', **other, external_id='PAPER001', external_version='1',
        source_url='', raw_payload=dict(key='PAPER001', data=dict(itemType='journalArticle', title='Private other source')),
        observed_at=datetime.now(timezone.utc).isoformat(), completeness='complete', missing_parts=[],
        passages=[dict(text='Private other source', json_path=['data', 'title'], section='title')])['source_snapshot_id']
    with pytest.raises(DomainError): bind(service, sid)
    stranger = ResearchService(service.storage.engine.url.render_as_string(hide_password=False), owner_id='other', scope_id='private')
    stranger.initialize()
    try:
        with pytest.raises(DomainError): bind(stranger, sid)
    finally: stranger.storage.engine.dispose()


def test_forget_version_suppresses_all_alias_sources_and_clears_proofs(catalog):
    service, *_ = catalog
    sid = seed(service, 'PAPER001', DOI='10.1234/shared')
    a = bind(service, sid)
    nid = seed(service, 'notion-page', provider='notion', paper_url='https://doi.org/10.1234/shared')
    bind(service, nid)
    call(service, 'memory.forget', target_ids=[a['paper_version_id']], scope='memory_and_source_snapshots')
    with service.storage.transaction() as c:
        assert service._catalog_sources(c, a['paper_version_id']) == []
        for alias in c.execute(select(db.catalog_aliases).where(service._scope(db.catalog_aliases))).mappings():
            assert alias['value'] == '' and alias['proof'] == {} and alias['state'] == 'forgotten'
        for source_id in (sid, nid):
            assert service._row(c, db.sources, source_id)['content'] == ''
    with pytest.raises(DomainError): bind(service, nid)


def test_configuration_requires_verified_namespace_matching_links(catalog):
    service, ids, links = catalog
    wrong = [dict(links[0], url='https://api.zotero.org/users/23456/items'), links[1]]
    with pytest.raises(DomainError): call(service, 'catalog.configure', registration_ids=ids, verified_links=wrong)
    unverified = [dict(links[0], valid=False), links[1]]
    with pytest.raises(DomainError): call(service, 'catalog.configure', registration_ids=ids, verified_links=unverified)


def test_notion_verified_database_uuid_may_differ_from_registered_data_source_uuid(catalog):
    service, ids, links = catalog
    attested = [links[0], dict(links[1], url='https://app.notion.com/p/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')]
    result = call(service, 'catalog.configure', registration_ids=ids, verified_links=attested)
    assert result['configured'] and result['verified_links'][1]['valid']


def test_provider_revision_and_non_paper_types_cannot_masquerade_as_scientific_version(catalog):
    service, *_ = catalog
    sid = seed(service, 'PAPER001', version='123456789', extra='Revision: 123456789\nReferences: 2601.01234v8')
    result = bind(service, sid)
    assert result['scientific_version_state'] == 'unknown' and result['scientific_version'] is None
    sid = seed(service, 'PAPER002', itemType='book')
    with pytest.raises(DomainError) as error: bind(service, sid)
    assert error.value.code == 'catalog_not_paper_metadata'
