"""Trash revisions are durable even though they have no accessible source head."""
import pytest
from sqlalchemy import select

from research_assistant import storage as db
from test_postgres import pg_url
from test_sync_runs import FakeAdapter, SETTINGS, cursor, run, service, snapshot, sources


def access_state(service, external_id):
    with service.storage.transaction() as connection:
        row=connection.execute(select(db.source_objects).where(service._scope(db.source_objects),
            db.source_objects.c.external_id==external_id)).mappings().one()
        epoch=connection.execute(select(db.scopes.c.access_epoch).where(
            db.scopes.c.id==service.scope_key)).scalar_one()
    return row['access_state'],row['revision'],epoch


def trashed_adapter(*,item_version='1'):
    adapter=FakeAdapter(objects={'AAAA0001':(item_version,'Synthetic trash'),
                                'BBBB0002':('1','Synthetic accessible paper')})
    adapter.snapshot_overrides['AAAA0001']={'change':'delete','passages':[]}
    return adapter


def test_unchanged_trash_is_not_refetched_or_rewritten(service):
    adapter=trashed_adapter()
    run(service,adapter)
    assert cursor(service)=='100'
    before=access_state(service,'AAAA0001')
    assert before[0]=='deleted'
    adapter.fetch_calls.clear()
    result=run(service,adapter)
    assert adapter.fetch_calls==[]
    assert access_state(service,'AAAA0001')==before
    assert result['data']['item_counts']=={'unchanged':2}
    assert {item['external_id'] for item in sources(service)}=={'BBBB0002'}


def test_changed_trash_revision_is_fetched_and_can_verify_untrash(service):
    adapter=trashed_adapter()
    run(service,adapter)
    adapter.fetch_calls.clear()
    run(service,adapter)
    assert adapter.fetch_calls==[]
    adapter.version='101'
    adapter.objects['AAAA0001']=('2','Synthetic restored paper')
    adapter.snapshot_overrides.clear()
    run(service,adapter)
    assert adapter.fetch_calls==['AAAA0001']
    assert access_state(service,'AAAA0001')[0]=='accessible'
    assert cursor(service)=='101'


def test_partial_trash_is_not_terminal_coverage_and_is_retried(service):
    adapter=trashed_adapter()
    adapter.snapshot_overrides['AAAA0001'].update(completeness='partial',missing_parts=['body'])
    first=run(service,adapter)
    assert first['data']['item_counts']=={'partial':1,'persisted':1}
    assert cursor(service) is None
    adapter.snapshot_overrides['AAAA0001']={'change':'delete','passages':[]}
    adapter.fetch_calls.clear()
    run(service,adapter)
    assert adapter.fetch_calls==['AAAA0001']
    assert cursor(service)=='100'


def test_deleted_object_without_a_sync_disposition_is_fetched(service):
    direct=snapshot('AAAA0001','1','Synthetic local deletion',change='delete')
    service.execute('sources.ingest',dict(direct,idempotency_key='direct-delete-without-sync'),trusted_capture=True)
    adapter=trashed_adapter()
    run(service,adapter)
    assert set(adapter.fetch_calls)==set(adapter.objects)
    assert access_state(service,'AAAA0001')[0]=='deleted'


def test_access_revoked_object_cannot_reuse_old_trash_disposition(service):
    adapter=trashed_adapter()
    run(service,adapter)
    direct=snapshot('AAAA0001','1','Synthetic access revocation',change='access_revoked')
    service.execute('sources.ingest',dict(direct,idempotency_key='direct-revoke-after-trash'),trusted_capture=True)
    adapter.snapshot_overrides.clear()
    adapter.fetch_calls.clear()
    run(service,adapter)
    assert adapter.fetch_calls==['AAAA0001']
    assert access_state(service,'AAAA0001')[0]=='accessible'


@pytest.mark.parametrize('item_version',[None,''])
def test_unknown_trash_revision_is_always_fetched(service,item_version):
    adapter=trashed_adapter(item_version=item_version)
    run(service,adapter)
    adapter.fetch_calls.clear()
    run(service,adapter)
    assert adapter.fetch_calls==['AAAA0001']
