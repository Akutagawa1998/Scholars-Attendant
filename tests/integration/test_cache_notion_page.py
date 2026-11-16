"""One-page native fetch caching never crawls, generates prose or writes remotely."""
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select,func

from research_assistant import storage as db
from research_assistant.connectors.notion import normalize_mcp_page
from research_assistant.service import ResearchService,DomainError

ID='11111111-2222-4333-8444-555555555555'
REG={'provider':'notion','source_instance_id':'synthetic-native','namespace':'collection://aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'}


@pytest.fixture
def cache(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('synthetic_native_cache',Path(__file__).resolve().parents[2]/'src/research_assistant/operators/cache_notion_page.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    s=ResearchService('sqlite:///'+str(tmp_path/'cache.sqlite3'));s.initialize()
    s.execute('sources.register',dict(REG,idempotency_key='reg'),trusted_capture=True)
    raw={'id':ID,'metadata':{'type':'page'},'title':'Synthetic existing paper','text':'Existing user-authored body.',
         'url':'https://www.notion.so/'+ID,'page_last_edited_at':'2026-10-20T00:00:00Z'}
    def seed(raw):
        snapshot=normalize_mcp_page(raw,source_instance_id=REG['source_instance_id'],namespace=REG['namespace'],
            external_id=ID,markdown_path=['text'],passage_paths=[{'json_path':['title'],'section':'title'}],
            external_version=raw['page_last_edited_at'],source_url=raw['url'])
        return s.execute('sources.ingest',dict(snapshot,idempotency_key=str(uuid4())),trusted_capture=True)['data']['source_snapshot_id']
    def network_forbidden(*args,**kwargs):pytest.fail('cache used remote I/O')
    monkeypatch.setattr('httpx.Client.request',network_forbidden)
    yield m,s,raw,seed
    s.storage.engine.dispose()


def count(s,table):
    with s.storage.transaction() as c:return c.execute(select(func.count()).select_from(table)).scalar_one()


def test_unchanged_native_body_reuses_snapshot_and_events(cache):
    m,s,raw,seed=cache;seed(raw)
    before=(count(s,db.sources),count(s,db.events))
    first=m.cache_page(s,raw);second=m.cache_page(s,raw)
    assert first['cache_reused'] and second['cache_reused']
    assert (count(s,db.sources),count(s,db.events))==before
    assert first['remote_calls']==0 and first['remote_write'] is False and first['llm_calls']==0
    assert first['completeness']=='unknown' and len(first['missing_parts'])==3
    assert first['literal_evidence_anchors_verified']==2


def test_only_changed_existing_body_creates_snapshot(cache):
    m,s,raw,seed=cache;seed(raw);before=count(s,db.sources)
    changed={**raw,'text':'Revised existing source body.','page_last_edited_at':'2026-10-21T00:00:00Z'}
    r=m.cache_page(s,changed)
    assert not r['cache_reused'] and count(s,db.sources)==before+1
    assert m.cache_page(s,changed)['cache_reused']
    assert count(s,db.works)==0 and count(s,db.reading_sessions)==0
    with pytest.raises(ValueError,match='stale_native_revision'):m.cache_page(s,raw)
    assert count(s,db.sources)==before+1


def test_unregistered_page_is_not_automatically_created(cache):
    m,s,raw,_=cache
    with pytest.raises(ValueError,match='registered_page_required'):m.cache_page(s,raw)
    assert count(s,db.sources)==0


def test_revoked_page_does_not_restore_access(cache):
    m,s,raw,seed=cache;sid=seed(raw)
    with s.storage.transaction() as c:source=s._row(c,db.sources,sid)
    s.execute('sources.set_access',{'source_snapshot_id':sid,'expected_revision':source['revision'],'state':'inaccessible','idempotency_key':'revoke'})
    with pytest.raises(ValueError):m.cache_page(s,raw)


def test_forgotten_page_cannot_reappear_from_native_payload(cache):
    m,s,raw,seed=cache;sid=seed(raw)
    s.execute('memory.forget',{'target_ids':[sid],'scope':'memory_and_source_snapshots','idempotency_key':'forget'})
    with pytest.raises((ValueError,DomainError)):m.cache_page(s,raw)


def test_complete_is_only_native_representation_not_collection(cache):
    m,s,raw,seed=cache
    complete={**raw,'truncated':False,'unknown_block_count':0,'unknown_block_ids':[]}
    seed(complete)
    r=m.cache_page(s,complete)
    assert r['completeness']=='complete'
    assert r['complete_collection_sync'] is False and r['checkpoint_advanced'] is False
