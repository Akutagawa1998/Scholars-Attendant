"""Bounded selection, cache reuse and safe stdout; no private accounts/network."""
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

import research_assistant.zotero_assets as assets
from research_assistant.connectors.common import ConnectorError


@pytest.fixture
def runner(monkeypatch):
    path=Path(__file__).resolve().parents[2]/'src/research_assistant/operators/sync_zotero_pdfs.py'
    spec=importlib.util.spec_from_file_location('synthetic_pdf_runner',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    data=b'%PDF-1.7\nSynthetic fixture bytes\n'
    md5=hashlib.md5(data).hexdigest()
    rows=[{'key':f'ITEM{i:04d}','snapshot_id':f'snapshot-{i}','version':'3','md5':md5} for i in range(10)]
    provenance=lambda key:{'attachment_key':key,'attachment_version':'3','metadata_md5':md5}
    calls={'metadata':[],'files':[],'ingest':[]}
    class Adapter:
        namespace='users/1'
        def attachment_metadata(self,key):calls['metadata'].append(key);return provenance(key)
        def fetch_pdf(self,key):
            calls['files'].append(key)
            return SimpleNamespace(status='ok',data=data,provenance=provenance(key))
    class Ingestor:
        def __init__(self,*args):pass
        def ingest(self,data,**kwargs):
            calls['ingest'].append(kwargs)
            return {'source_snapshot_id':'opaque','asset_id':'opaque','deduplicated':False,
                    'page_count':2,'alignment_counts':{'normalized':4}}
    monkeypatch.setattr(module,'candidates',lambda *args,**kwargs:rows)
    monkeypatch.setattr(module,'cached_bytes',lambda *args:None)
    monkeypatch.setattr(assets,'ZoteroPdfIngestor',Ingestor)
    return SimpleNamespace(module=module,adapter=Adapter(),calls=calls,data=data,rows=rows,provenance=provenance)


def test_default_stops_after_three_successful_originals(runner):
    r=runner.module.acquire(None,None,runner.adapter)
    assert r['status']=='ok' and r['counts']['acquired']==3
    assert r['counts']['downloaded']==3 and len(runner.calls['files'])==3
    assert r['alignment_counts']=={'normalized':12}
    assert r['llm_calls']==0 and r['remote_write'] is False
    assert r['complete_library_file_sync'] is False
    assert all(set(c['provenance'])=={'attachment_key','attachment_version','attachment_md5'} for c in runner.calls['ingest'])


def test_repeat_checks_metadata_but_uses_no_file_endpoint(runner,monkeypatch):
    monkeypatch.setattr(runner.module,'cached_bytes',lambda *args:{'data':runner.data,'acquisition_method':'zotero_local_storage'})
    r=runner.module.acquire(None,None,runner.adapter)
    assert r['counts']['reused_cached']==3
    assert r['counts'].get('downloaded',0)==0 and not runner.calls['files']
    assert len(runner.calls['metadata'])==3 and len(runner.calls['ingest'])==3
    assert all(c['acquisition_method']=='zotero_local_storage' for c in runner.calls['ingest'])


def test_local_original_preferred_to_cloud_without_mutating_original(runner,monkeypatch):
    import research_assistant.connectors.zotero_local as local
    reads=[]
    def original(root,key,md5):reads.append((root,key,md5));return runner.data
    monkeypatch.setattr(local,'read_local_pdf',original)
    r=runner.module.acquire(None,None,runner.adapter,local_storage_root=Path('/synthetic-storage'))
    assert r['counts']['copied_from_local']==3 and len(reads)==3
    assert not runner.calls['files']
    assert all(c['acquisition_method']=='zotero_local_storage' for c in runner.calls['ingest'])


def test_missing_cloud_files_do_not_crawl_library(runner):
    def unavailable(key):
        runner.calls['files'].append(key)
        return SimpleNamespace(status='unavailable',error_code='not_found_or_inaccessible',retry_after=None)
    runner.adapter.fetch_pdf=unavailable
    r=runner.module.acquire(None,None,runner.adapter,max_files=2)
    assert r['status']=='partial' and r['counts'].get('acquired',0)==0
    assert r['counts']['attempted']==4 and len(runner.calls['files'])==4
    assert r['errors']=={'not_found_or_inaccessible':4}
    assert not runner.calls['ingest']


def test_changed_metadata_never_downloads_or_ingests(runner):
    runner.adapter.attachment_metadata=lambda key:{**runner.provenance(key),'attachment_version':'4'}
    r=runner.module.acquire(None,None,runner.adapter)
    assert r['status']=='partial' and not runner.calls['files'] and not runner.calls['ingest']
    assert r['errors']=={'source_changed_refresh_metadata':6}


def test_revoked_file_grant_stops_at_first_attempt(runner):
    def revoked(key):raise ConnectorError('file_access_not_granted')
    runner.adapter.attachment_metadata=revoked
    r=runner.module.acquire(None,None,runner.adapter)
    assert r['status']=='partial' and r['counts']['attempted']==1
    assert not runner.calls['files'] and not runner.calls['ingest']


def test_corrupt_cached_bytes_never_ingested_or_replaced(runner,monkeypatch):
    monkeypatch.setattr(runner.module,'cached_bytes',lambda *args:{'data':b'%PDF-WRONG','acquisition_method':'zotero_api_file'})
    r=runner.module.acquire(None,None,runner.adapter,max_files=1)
    assert r['errors']=={'file_integrity_mismatch':2}
    assert not runner.calls['files'] and not runner.calls['ingest']


def test_unexpected_operational_error_is_fixed_code(runner):
    def unavailable(key):raise ConnectorError('PRIVATE-account-url-token')
    runner.adapter.attachment_metadata=unavailable
    r=runner.module.acquire(None,None,runner.adapter,max_files=1)
    assert r['errors']=={'pdf_acquisition_unavailable':2}
    assert 'PRIVATE' not in str(r)


def test_storage_failure_prevents_credentials_and_network(runner,monkeypatch,capsys):
    def missing(*args,**kwargs):raise runner.module.AssetError('external_storage_unavailable')
    monkeypatch.setattr(runner.module,'select_asset_root',missing)
    def forbidden(*args):pytest.fail('read credentials before validating storage')
    monkeypatch.setattr(runner.module,'object_file',forbidden)
    assert runner.module.main([])==1
    assert 'pdf_acquisition_unavailable' in capsys.readouterr().err


def test_production_download_ignores_environment_asset_override(runner,monkeypatch):
    observed={}
    def select(*args,**kwargs):
        observed.update(kwargs)
        raise runner.module.AssetError('external_storage_unavailable')
    monkeypatch.setenv('RA_ASSET_ROOT','/internal/unintended')
    monkeypatch.setattr(runner.module,'select_asset_root',select)
    assert runner.module.main([])==1
    assert observed['environ']=={}


def test_missing_registered_external_storage_prevents_credentials(runner,monkeypatch):
    monkeypatch.setattr(runner.module,'select_asset_root',lambda *args,**kwargs:Path('/synthetic'))
    reads=[]
    def config(path):
        reads.append(path)
        assert len(reads)==1
        return {'database_url':'postgresql+psycopg://fixture'}
    monkeypatch.setattr(runner.module,'object_file',config)
    assert runner.module.main([])==1
    assert len(reads)==1
