"""Durable synchronization contracts using synthetic adapters on both stores.

Real accounts are never consulted by these tests. A test PostgreSQL URL, when
provided, is restricted by the shared pg_url fixture to a disposable schema.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select

from research_assistant import storage as db
from research_assistant.connectors.common import ConnectorError
from research_assistant.service import DomainError, ResearchService
from test_postgres import pg_url

SETTINGS = dict(provider='zotero', source_instance_id='fixture-zotero-instance', namespace='users/fixture-library')


def snapshot(key, version, value, *, change='upsert'):
    return dict(SETTINGS, external_id=key, external_version=version,
        source_url='https://example.invalid/fixture/' + key,
        raw_payload={'data':{'key':key,'version':version,'title':value,'itemType':'journalArticle'}},
        observed_at=datetime.now(timezone.utc).isoformat(), occurred_at=None,
        completeness='complete', missing_parts=[], change=change,
        passages=[{'text':value,'json_path':['data','title'],'section':'title'}] if change=='upsert' else [])


class FakeAdapter:
    def __init__(self, objects=None, *, version='100', complete=True, consistent=True):
        self.objects = ({'AAAA0001':('1','Synthetic title A'), 'BBBB0002':('1','Synthetic title B')}
                        if objects is None else objects)
        self.version, self.complete, self.consistent = version, complete, consistent
        self.fetch_calls = []
        self.inventory_calls = 0
        self.deleted_calls = []
        self.fail_keys = set()
        self.deleted_keys = []
        self.version_after_inventory = None
        self.objects_after_inventory = None
        self.omit_keys = set()
        self.snapshot_overrides = {}
        self.extra_snapshots = []
        self.duplicate_keys = set()
        self.delete_error = None

    def inventory(self):
        self.inventory_calls += 1
        version = self.version_after_inventory if self.version_after_inventory and self.inventory_calls > 1 else self.version
        objects=(self.objects_after_inventory if self.objects_after_inventory is not None and self.inventory_calls>1 else self.objects)
        return dict(version=version, object_versions={key:item[0] for key,item in objects.items()},
                    complete=self.complete, consistent=self.consistent, metadata={'fixture_only':True})

    def fetch_objects(self, keys, expected_version):
        self.fetch_calls.extend(keys)
        if any(key in self.fail_keys for key in keys):
            raise ConnectorError('source_unavailable')
        result=[]
        for key in keys:
            if key in self.omit_keys:
                continue
            version,value=self.objects[key]
            item=snapshot(key,version,value)
            item.update(self.snapshot_overrides.get(key,{}))
            result.append(item)
            if key in self.duplicate_keys:
                result.append(dict(item))
        return result + self.extra_snapshots

    def deleted(self, since, expected_version):
        self.deleted_calls.append((since,expected_version))
        if self.delete_error:
            raise ConnectorError(self.delete_error)
        return [snapshot(key,self.version,'Synthetic deletion marker',change='delete') for key in self.deleted_keys]


class FailingPhaseAdapter(FakeAdapter):
    def __init__(self,stage,code):
        super().__init__(version='101')
        self.stage,self.code=stage,code
        self.objects['AAAA0001']=('2','Synthetic updated title A')

    def inventory(self):
        if ((self.stage=='initial_inventory' and self.inventory_calls==0)
                or (self.stage=='final_inventory' and self.inventory_calls==1)):
            raise ConnectorError(self.code)
        return super().inventory()

    def fetch_objects(self,keys,expected_version):
        if self.stage=='fetch':raise ConnectorError(self.code)
        return super().fetch_objects(keys,expected_version)

    def deleted(self,since,expected_version):
        if self.stage=='deleted':raise ConnectorError(self.code)
        return super().deleted(since,expected_version)


@pytest.fixture(params=['sqlite','postgres'])
def service(request,tmp_path):
    url=('sqlite:///'+str(tmp_path/'sync-fixture.sqlite3') if request.param=='sqlite'
         else request.getfixturevalue('pg_url'))
    instance=ResearchService(url,owner_id='sync-fixture-owner',scope_id='private')
    instance.initialize()
    instance.test_database_url=url
    instance.execute('sources.register',dict(SETTINGS,idempotency_key='fixture-registration'),trusted_capture=True)
    yield instance
    instance.storage.engine.dispose()


def run(service,adapter,**options):
    from research_assistant.sync import SyncCoordinator
    return SyncCoordinator(service,**options).run(adapter,**SETTINGS)


def cursor(service):
    with service.storage.transaction() as connection:
        row=connection.execute(select(db.connector_cursors).where(service._scope(db.connector_cursors))).mappings().first()
    return None if row is None else row['cursor']


def sources(service):
    return service.execute('sources.search',{})['data']['sources']


def ledger(service,table):
    with service.storage.transaction() as connection:
        return [dict(row) for row in connection.execute(select(table).where(service._scope(table))).mappings()]


class MutableClock:
    def __init__(self):
        self.value=datetime(2026,1,1,tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self,**duration):
        self.value+=timedelta(**duration)


def test_complete_inventory_commits_once_without_creating_reading(service):
    adapter=FakeAdapter()
    run(service,adapter)
    assert cursor(service)=='100'
    assert {item['external_id'] for item in sources(service)}==set(adapter.objects)
    assert service.execute('papers.search',{'read_filter':'read_only'})['data']['papers']==[]
    assert service.execute('memory.recall',{})['data']['reported_facts']==[]


def test_complete_empty_inventory_can_commit_a_cursor(service):
    adapter=FakeAdapter(objects={})
    run(service,adapter)
    assert cursor(service)=='100'
    assert sources(service)==[]
    assert adapter.fetch_calls==[]


def test_legacy_checkpoint_flags_cannot_claim_unverified_inventory(service):
    item=snapshot('AAAA0001','1','Synthetic manually ingested object')
    item['idempotency_key']='manual-fixture-source'
    imported=service.execute('sources.ingest',item,trusted_capture=True)
    checkpoint=dict(SETTINGS,cursor='999',run_id='invented-complete-run',
        source_snapshot_ids=[imported['data']['source_snapshot_id']],snapshot_complete=True,
        source_consistent=True,idempotency_key='manual-fixture-checkpoint')
    result=service.execute('sources.checkpoint',checkpoint,trusted_capture=True)
    assert cursor(service) is None
    assert result['data']['checkpoint_advanced'] is False


def test_unchanged_inventory_does_not_refetch_or_duplicate_snapshots(service):
    adapter=FakeAdapter()
    run(service,adapter)
    original={item['source_snapshot_id'] for item in sources(service)}
    adapter.fetch_calls.clear()
    run(service,adapter)
    assert adapter.fetch_calls==[]
    assert {item['source_snapshot_id'] for item in sources(service)}==original
    assert cursor(service)=='100'


def test_incremental_change_fetches_only_changed_item(service):
    adapter=FakeAdapter()
    run(service,adapter)
    original={item['external_id']:item['source_snapshot_id'] for item in sources(service)}
    adapter.version='101'
    adapter.objects['AAAA0001']=('2','Synthetic changed title A')
    adapter.fetch_calls.clear()
    run(service,adapter)
    current={item['external_id']:item['source_snapshot_id'] for item in sources(service)}
    assert set(adapter.fetch_calls)=={'AAAA0001'}
    assert current['AAAA0001']!=original['AAAA0001']
    assert current['BBBB0002']==original['BBBB0002']
    assert cursor(service)=='101'


@pytest.mark.parametrize('complete,consistent',[(False,True),(True,False),(False,False)])
def test_incomplete_or_best_effort_inventory_cannot_commit_cursor(service,complete,consistent):
    adapter=FakeAdapter(complete=complete,consistent=consistent)
    run(service,adapter)
    assert cursor(service) is None


def test_source_version_change_before_completion_blocks_checkpoint(service):
    adapter=FakeAdapter()
    adapter.version_after_inventory='101'
    run(service,adapter)
    assert cursor(service) is None
    assert adapter.inventory_calls>=2


def test_object_inventory_change_at_same_source_version_blocks_checkpoint(service):
    adapter=FakeAdapter()
    adapter.objects_after_inventory={'AAAA0001':('2','Synthetic changed object'), 'BBBB0002':('1','Synthetic title B')}
    run(service,adapter)
    assert cursor(service) is None


def test_stale_numeric_zotero_library_version_cannot_rollback_cursor(service):
    adapter=FakeAdapter()
    run(service,adapter)
    adapter.version='99'
    run(service,adapter)
    assert cursor(service)=='100'


def test_missing_requested_object_cannot_be_silently_completed(service):
    adapter=FakeAdapter()
    adapter.omit_keys.add('AAAA0001')
    run(service,adapter)
    assert cursor(service) is None


def test_duplicate_requested_object_cannot_be_silently_completed(service):
    adapter=FakeAdapter()
    adapter.duplicate_keys.add('AAAA0001')
    run(service,adapter)
    assert cursor(service) is None


def test_unrequested_object_response_cannot_be_ingested(service):
    adapter=FakeAdapter()
    adapter.extra_snapshots=[snapshot('ZZZZ0009','1','Synthetic object outside inventory')]
    run(service,adapter)
    assert cursor(service) is None
    assert all(item['external_id']!='ZZZZ0009' for item in sources(service))


def test_failed_object_is_retried_at_same_library_version_after_restart(service):
    adapter=FakeAdapter()
    clock=MutableClock()
    adapter.fail_keys.add('AAAA0001')
    run(service,adapter,clock=clock)
    assert cursor(service) is None
    failures=ledger(service,db.sync_failures)
    assert failures
    assert any(row['error_code']=='source_unavailable' for row in failures)
    assert all(row['attempts']>=1 for row in failures)
    assert all('external_id' not in row and len(row['identity_hash'])==64 for row in failures)
    # Retry policy remains durable across process recreation, including its due time.
    clock.advance(days=1)
    recreated=ResearchService(service.test_database_url,owner_id=service.owner_id,scope_id=service.scope_id)
    recreated.initialize()
    adapter.fail_keys.clear()
    adapter.fetch_calls.clear()
    try:
        run(recreated,adapter,clock=clock)
        assert 'AAAA0001' in adapter.fetch_calls
        assert cursor(recreated)=='100'
        assert {item['external_id'] for item in sources(recreated)}==set(adapter.objects)
        assert ledger(recreated,db.sync_failures)==[]
    finally:
        recreated.storage.engine.dispose()


def test_retry_backoff_blocks_early_attempt_and_cursor(service):
    clock=MutableClock()
    adapter=FakeAdapter()
    adapter.fail_keys.add('AAAA0001')
    run(service,adapter,clock=clock)
    failures=ledger(service,db.sync_failures)
    assert failures and all(row['next_retry_at'] for row in failures)
    adapter.fail_keys.clear()
    adapter.fetch_calls.clear()
    run(service,adapter,clock=clock)
    assert 'AAAA0001' not in adapter.fetch_calls
    assert cursor(service) is None
    clock.advance(days=1)
    run(service,adapter,clock=clock)
    assert 'AAAA0001' in adapter.fetch_calls
    assert cursor(service)=='100'


@pytest.mark.parametrize('failure_type',[RuntimeError,ConnectorError])
def test_unknown_adapter_error_is_sanitized_in_durable_ledger(service,failure_type):
    secret='synthetic-private-token-that-must-never-be-persisted'
    class FailingAdapter(FakeAdapter):
        def fetch_objects(self,keys,expected_version):
            raise failure_type(secret)
    result=run(service,FailingAdapter())
    assert cursor(service) is None
    failures=ledger(service,db.sync_failures)
    assert failures
    rows=failures+ledger(service,db.sync_items)+ledger(service,db.sync_manifests)
    serialized=json.dumps(rows,default=str)+json.dumps(result,default=str)
    assert secret not in serialized
    assert 'AAAA0001' not in json.dumps(failures,default=str)
    assert 'Synthetic title A' not in serialized


def test_active_lease_prevents_a_second_worker_from_fetching(service):
    clock=MutableClock()
    competing=FakeAdapter()
    other=ResearchService(service.test_database_url,owner_id=service.owner_id,scope_id=service.scope_id)
    other.initialize()
    class OverlappingAdapter(FakeAdapter):
        def inventory(self):
            if self.inventory_calls==0:
                with pytest.raises(DomainError) as caught:
                    run(other,competing,clock=clock)
                assert caught.value.code=='sync_lease_busy'
            return super().inventory()
    try:
        run(service,OverlappingAdapter(),clock=clock)
        assert competing.inventory_calls==0
        assert competing.fetch_calls==[]
        assert cursor(service)=='100'
        leases=ledger(service,db.sync_leases)
        assert len(leases)==1 and leases[0]['token_hash']==''
    finally:
        other.storage.engine.dispose()


def test_expired_worker_cannot_ingest_after_a_new_worker_acquires_the_lease(service):
    clock=MutableClock()
    replacement=FakeAdapter(objects={'AAAA0001':('1','Synthetic replacement worker title')})
    other=ResearchService(service.test_database_url,owner_id=service.owner_id,scope_id=service.scope_id)
    other.initialize()
    class ExpiringAdapter(FakeAdapter):
        def fetch_objects(self,keys,expected_version):
            clock.advance(seconds=3)
            run(other,replacement,clock=clock,lease_seconds=2)
            return super().fetch_objects(keys,expected_version)
    stale=ExpiringAdapter(objects={'AAAA0001':('1','Synthetic stale worker title')})
    try:
        try:
            run(service,stale,clock=clock,lease_seconds=2)
        except DomainError as exc:
            assert exc.code=='sync_lease_lost'
        assert cursor(service)=='100'
        assert len(sources(service))==1
        assert service.execute('sources.search',{'query':'Synthetic stale worker title'})['data']['sources']==[]
        assert service.execute('sources.search',{'query':'Synthetic replacement worker title'})['data']['total']==1
        with service.storage.transaction() as connection:
            saved=list(connection.execute(select(db.sources.c.content).where(service._scope(db.sources))).scalars())
        assert all('Synthetic stale worker title' not in content for content in saved)
    finally:
        other.storage.engine.dispose()


@pytest.mark.parametrize('override',[
    {'namespace':'another-fixture-library'},
    {'source_instance_id':'another-fixture-instance'},
    {'external_id':'ZZZZ0009'},
    {'external_version':'not-the-inventory-version'},
])
def test_snapshot_outside_manifest_cannot_be_committed(service,override):
    adapter=FakeAdapter()
    adapter.snapshot_overrides['AAAA0001']=override
    run(service,adapter)
    assert cursor(service) is None
    assert all(item['external_id']!='ZZZZ0009' for item in sources(service))


@pytest.mark.parametrize('completeness,missing_parts',[
    ('partial',['body']),('unknown',[]),('complete',['unsupported_block']),
])
def test_incomplete_object_content_blocks_checkpoint(service,completeness,missing_parts):
    adapter=FakeAdapter()
    adapter.snapshot_overrides['AAAA0001']={'completeness':completeness,'missing_parts':missing_parts}
    run(service,adapter)
    assert cursor(service) is None


def test_explicit_deletion_is_persisted_before_incremental_cursor(service):
    adapter=FakeAdapter()
    run(service,adapter)
    original={item['external_id']:item['source_snapshot_id'] for item in sources(service)}
    adapter.version='101'
    del adapter.objects['AAAA0001']
    adapter.deleted_keys=['AAAA0001']
    run(service,adapter)
    assert cursor(service)=='101'
    assert {item['external_id'] for item in sources(service)}=={'BBBB0002'}
    with pytest.raises(DomainError):
        service.execute('sources.get_evidence',{'source_snapshot_id':original['AAAA0001']})


def test_complete_current_snapshot_verifies_remote_untrash_restore(service):
    adapter=FakeAdapter()
    run(service,adapter)
    adapter.version='101'
    adapter.objects['AAAA0001']=('2','Synthetic trashed item')
    adapter.snapshot_overrides['AAAA0001']={'change':'delete','passages':[]}
    run(service,adapter)
    assert {item['external_id'] for item in sources(service)}=={'BBBB0002'}
    assert cursor(service)=='101'
    adapter.version='102'
    adapter.objects['AAAA0001']=('3','Synthetic restored item')
    adapter.snapshot_overrides.clear()
    run(service,adapter)
    assert {item['external_id'] for item in sources(service)}=={'AAAA0001','BBBB0002'}
    assert cursor(service)=='102'
    assert service.execute('sources.search',{'query':'Synthetic restored item'})['data']['total']==1


def test_fresh_same_version_fetch_can_verify_restored_access(service):
    adapter=FakeAdapter()
    run(service,adapter)
    first=next(item for item in sources(service) if item['external_id']=='AAAA0001')
    service.execute('sources.set_access', dict(
        source_snapshot_id=first['source_snapshot_id'], expected_revision=1,
        state='inaccessible',
        idempotency_key='revoke',
    ))
    adapter.fetch_calls.clear()
    run(service,adapter)
    assert 'AAAA0001' in adapter.fetch_calls
    assert {item['external_id'] for item in sources(service)}=={'AAAA0001','BBBB0002'}
    assert cursor(service)=='100'


@pytest.mark.parametrize('incomplete_inventory',[True,False])
def test_incomplete_observation_cannot_restore_deleted_content(service,incomplete_inventory):
    adapter=FakeAdapter()
    run(service,adapter)
    adapter.version='101'
    adapter.objects['AAAA0001']=('2','Synthetic trashed item')
    adapter.snapshot_overrides['AAAA0001']={'change':'delete','passages':[]}
    run(service,adapter)
    adapter.version='102'
    adapter.objects['AAAA0001']=('3','Synthetic unverified restored item')
    if incomplete_inventory:
        adapter.complete=False
        adapter.snapshot_overrides.clear()
    else:
        adapter.snapshot_overrides['AAAA0001']={'change':'restore','completeness':'partial','missing_parts':['body']}
    run(service,adapter)
    assert {item['external_id'] for item in sources(service)}=={'BBBB0002'}
    assert cursor(service)=='101'


@pytest.mark.parametrize('change',['access_revoked','delete'])
@pytest.mark.parametrize('stage',['fetch','final_inventory'])
def test_revocation_during_sync_cannot_be_reopened_or_checkpointed(service,change,stage):
    initial=FakeAdapter()
    run(service,initial)
    def revoke():
        revoked=snapshot('AAAA0001','2','Synthetic source revoked while sync was in flight',change=change)
        revoked['observed_at']=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
        revoked['idempotency_key']='fixture-revocation-during-sync'
        service.execute('sources.ingest',revoked,trusted_capture=True)
    class RevokedDuringFetch(FakeAdapter):
        def fetch_objects(self,keys,expected_version):
            values=super().fetch_objects(keys,expected_version)
            if stage=='fetch':revoke()
            return values
        def inventory(self):
            if stage=='final_inventory' and self.inventory_calls==1:revoke()
            return super().inventory()
    adapter=RevokedDuringFetch(version='101')
    adapter.objects['AAAA0001']=('2','Synthetic inflight body must remain inaccessible')
    try:
        run(service,adapter)
    except DomainError as exc:
        assert exc.code=='sync_lease_lost'
    assert {item['external_id'] for item in sources(service)}=={'BBBB0002'}
    assert cursor(service)=='100'


def test_forgetting_after_ingest_before_ledger_commit_keeps_ledger_suppressed(service,monkeypatch):
    execute=service.execute
    forgotten=False
    def forget_before_receipt(operation,payload,**options):
        nonlocal forgotten
        result=execute(operation,payload,**options)
        if operation=='sources.ingest' and payload['external_id']=='AAAA0001' and not forgotten:
            forgotten=True
            execute('memory.forget',dict(target_ids=[result['data']['source_snapshot_id']],
                scope='memory_and_source_snapshots',idempotency_key='fixture-forget-before-ledger'))
        return result
    monkeypatch.setattr(service,'execute',forget_before_receipt)
    try:
        run(service,FakeAdapter())
    except DomainError as exc:
        assert exc.code=='sync_lease_lost'
    assert all(item['external_id']!='AAAA0001' for item in sources(service))
    with service.storage.transaction() as connection:
        forgotten_key=connection.execute(select(db.source_objects.c.identity_hash).where(
            service._scope(db.source_objects),db.source_objects.c.external_id=='')).scalar_one()
    items=[row for row in ledger(service,db.sync_items) if row['identity_hash']==forgotten_key]
    assert items and all(row['state']=='suppressed' and row['expected_version'] is None and row['snapshot_id'] is None for row in items)
    assert all(row['identity_hash']!=forgotten_key for row in ledger(service,db.sync_failures))


def test_missing_inventory_entry_without_delete_never_claims_deleted(service):
    adapter=FakeAdapter()
    run(service,adapter)
    first=next(item for item in sources(service) if item['external_id']=='AAAA0001')
    adapter.version='101'
    del adapter.objects['AAAA0001']
    run(service,adapter)
    with service.storage.transaction() as connection:
        row=service._row(connection,db.sources,first['source_snapshot_id'])
    assert row['access_state']!='deleted'
    assert cursor(service)=='100'


def test_failed_deletion_feed_cannot_advance_incremental_cursor(service):
    adapter=FakeAdapter()
    run(service,adapter)
    adapter.version='101'
    del adapter.objects['AAAA0001']
    adapter.delete_error='source_unavailable'
    run(service,adapter)
    assert cursor(service)=='100'
    assert {item['external_id'] for item in sources(service)}=={'AAAA0001','BBBB0002'}


def test_deletion_feed_contradicting_inventory_cannot_delete_current_item(service):
    adapter=FakeAdapter()
    run(service,adapter)
    adapter.version='101'
    adapter.deleted_keys=['AAAA0001']
    run(service,adapter)
    assert cursor(service)=='100'
    assert {item['external_id'] for item in sources(service)}=={'AAAA0001','BBBB0002'}


def test_deletion_feed_outside_expected_library_version_cannot_commit(service):
    initial=FakeAdapter()
    run(service,initial)
    class WrongVersionDeleted(FakeAdapter):
        def deleted(self,since,expected_version):
            return [snapshot('AAAA0001','999','Synthetic wrong-version deletion',change='delete')]
    adapter=WrongVersionDeleted(objects={'BBBB0002':('1','Synthetic title B')},version='101')
    run(service,adapter)
    assert cursor(service)=='100'
    assert {item['external_id'] for item in sources(service)}=={'AAAA0001','BBBB0002'}


def test_forgotten_item_is_suppressed_before_fetch_under_a_new_revision(service):
    adapter=FakeAdapter()
    run(service,adapter)
    first=next(item for item in sources(service) if item['external_id']=='AAAA0001')
    service.execute('memory.forget',dict(target_ids=[first['source_snapshot_id']],scope='memory_and_source_snapshots',
        idempotency_key='fixture-forget-source'))
    adapter.version='101'
    adapter.objects['AAAA0001']=('2','Synthetic erased content must not be fetched again')
    adapter.fetch_calls.clear()
    run(service,adapter)
    assert 'AAAA0001' not in adapter.fetch_calls
    assert {item['external_id'] for item in sources(service)}=={'BBBB0002'}


@pytest.mark.parametrize('stage,code',[
    ('initial_inventory','access_revoked'),
    ('initial_inventory','authentication_failed'),
    ('initial_inventory','unauthorized'),
    ('initial_inventory','forbidden'),
    ('initial_inventory','insufficient_permissions'),
    ('initial_inventory','identity_mismatch'),
    ('final_inventory','access_revoked'),
    ('fetch','authentication_failed'),
    ('deleted','forbidden'),
])
def test_explicit_connector_permission_loss_closes_copies_and_can_recover(service,stage,code):
    clock=MutableClock()
    run(service,FakeAdapter(),clock=clock)
    originals=[item['source_snapshot_id'] for item in sources(service)]
    denied=FailingPhaseAdapter(stage,code)
    run(service,denied,clock=clock)
    assert sources(service)==[]
    assert cursor(service)=='100'
    events=[row for row in ledger(service,db.events) if row['operation']=='sources.permission_revoked']
    assert len(events)==1 and events[0]['receipt']['data']['revoked_count']==2
    assert 'AAAA0001' not in json.dumps(events)
    assert 'Synthetic title' not in json.dumps(events)
    for sid in originals:
        with pytest.raises(DomainError):
            service.execute('sources.get_evidence',{'source_snapshot_id':sid})
    clock.advance(days=1)
    restored=FakeAdapter(objects=denied.objects,version='101')
    run(service,restored,clock=clock)
    assert set(restored.fetch_calls)==set(denied.objects)
    assert {item['external_id'] for item in sources(service)}==set(denied.objects)
    assert cursor(service)=='101'


def test_permission_loss_is_not_discarded_when_final_inventory_also_changes(service):
    run(service,FakeAdapter())
    denied=FailingPhaseAdapter('deleted','access_revoked')
    denied.version_after_inventory='102'
    run(service,denied)
    assert sources(service)==[]
    assert cursor(service)=='100'


def test_registration_permission_loss_closes_all_copies_atomically(service,monkeypatch):
    run(service,FakeAdapter())
    execute=service.execute
    leaked=[]
    checked=False
    def observe_revoke_commits(operation,payload,**options):
        nonlocal checked
        result=execute(operation,payload,**options)
        if operation=='sources.ingest' and payload.get('change')=='access_revoked' and not checked:
            checked=True
            visible=execute('sources.search',{})['data']['sources']
            leaked.extend(item['external_id'] for item in visible)
        return result
    monkeypatch.setattr(service,'execute',observe_revoke_commits)
    run(service,FailingPhaseAdapter('initial_inventory','access_revoked'))
    assert leaked==[]
    assert sources(service)==[]


@pytest.mark.parametrize('stage',['fetch','deleted'])
def test_permission_loss_closes_copies_before_any_further_remote_work(service,stage):
    run(service,FakeAdapter())
    leaked=[]
    class ObserveAfterDenial(FailingPhaseAdapter):
        def deleted(self,since,expected_version):
            if self.stage=='fetch':leaked.extend(item['external_id'] for item in sources(service))
            return super().deleted(since,expected_version)
        def inventory(self):
            if self.stage=='deleted' and self.inventory_calls==1:
                leaked.extend(item['external_id'] for item in sources(service))
            return super().inventory()
    run(service,ObserveAfterDenial(stage,'access_revoked'))
    assert leaked==[]
    assert sources(service)==[]


def test_permission_loss_does_not_revoke_another_registered_namespace(service):
    run(service,FakeAdapter())
    foreign=dict(SETTINGS,namespace='users/other-fixture-library')
    service.execute('sources.register',dict(foreign,idempotency_key='fixture-other-registration'),trusted_capture=True)
    item=snapshot('ZZZZ0009','1','Synthetic separate library content')
    item.update(foreign,idempotency_key='fixture-other-library-source')
    service.execute('sources.ingest',item,trusted_capture=True)
    run(service,FailingPhaseAdapter('initial_inventory','access_revoked'))
    remaining=sources(service)
    assert len(remaining)==1 and remaining[0]['namespace']==foreign['namespace']
    assert remaining[0]['external_id']=='ZZZZ0009'


@pytest.mark.parametrize('stage',['initial_inventory','fetch','deleted','final_inventory'])
def test_ordinary_network_error_preserves_offline_copies(service,stage):
    run(service,FakeAdapter())
    originals=[item['source_snapshot_id'] for item in sources(service)]
    run(service,FailingPhaseAdapter(stage,'source_unavailable'))
    assert {item['external_id'] for item in sources(service)}=={'AAAA0001','BBBB0002'}
    assert cursor(service)=='100'
    for sid in originals:
        assert service.execute('sources.get_evidence',{'source_snapshot_id':sid})['data']['passages']
