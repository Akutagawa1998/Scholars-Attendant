"""Acquire a bounded, annotated Zotero PDF sample into the configured private store.

No LLM, summaries, remote writes, or reading-state mutations. Existing current
metadata is required; refresh it with sync-zotero.py --full first. The default
sample is three attachments. Unchanged cached bytes are reused after a fresh
permission/metadata check; provider revisions are not scientific paper versions.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

from sqlalchemy import select

from research_assistant import storage as db
from research_assistant.assets import ArtifactStore, AssetError
from research_assistant.asset_service import read_source_asset
from research_assistant.connectors.common import ConnectorError
from research_assistant.connectors.zotero_files import ZoteroFileClient
from research_assistant.service import ResearchService, DomainError
from research_assistant.storage_paths import select_asset_root, validate_external_storage


def object_file(path):
    try:
        value=json.loads(Path(path).expanduser().read_text())
    except Exception:
        raise ConnectorError('configuration_unavailable') from None
    if not isinstance(value,dict):
        raise ConnectorError('configuration_unavailable')
    return value


def candidates(service, namespace, *, keys=None):
    """Only current registered objects; prefer annotated PDFs with an MD5."""
    attachments=[];annotations=Counter()
    with service.storage.transaction() as c:
        rows=c.execute(select(db.source_objects,db.sources.c.content).join(
            db.connector_registrations,db.source_objects.c.registration_id==db.connector_registrations.c.id).join(
            db.sources,db.sources.c.id==db.source_objects.c.head_source_version_id).where(
                service._scope(db.source_objects),service._scope(db.sources),service._scope(db.connector_registrations),
                db.connector_registrations.c.provider=='zotero',
                db.connector_registrations.c.source_instance_id=='api.zotero.org:v3',
                db.connector_registrations.c.namespace==namespace,
                db.source_objects.c.access_state=='accessible',db.sources.c.access_state=='accessible')).mappings()
        for row in rows:
            if service._blocked(c,identity=row['id']) or service._blocked(c,identity=row['head_source_version_id']):
                continue
            try:data=json.loads(row['content']).get('data',{})
            except (ValueError,AttributeError):continue
            if data.get('deleted'):continue
            key=row['external_id']
            if data.get('itemType')=='annotation':annotations[data.get('parentItem')]+=1
            elif (data.get('itemType')=='attachment' and data.get('contentType')=='application/pdf'
                    and data.get('linkMode') in {'imported_file','imported_url'}
                    and re.fullmatch(r'[a-fA-F0-9]{32}',data.get('md5') or '')):
                attachments.append({'key':key,'snapshot_id':row['head_source_version_id'],
                    'version':str(data.get('version','')),'md5':data['md5'].lower()})
    if keys:
        chosen=set(keys)
        attachments=[row for row in attachments if row['key'] in chosen]
        if {row['key'] for row in attachments}!=chosen:
            raise ConnectorError('attachment_not_available')
    else:
        attachments=[row for row in attachments if annotations[row['key']]>0]
    return sorted(attachments,key=lambda row:(-annotations[row['key']],row['key']))


def cached_bytes(service,store,snapshot_id):
    with service.storage.transaction() as c:
        rows=c.execute(select(db.attachment_asset_links.c.pdf_source_id,db.attachment_asset_links.c.provenance).where(
            service._scope(db.attachment_asset_links),
            db.attachment_asset_links.c.metadata_snapshot_id==snapshot_id)).mappings().all()
    for row in rows:
        try:
            data=read_source_asset(service,store,row['pdf_source_id'])
        except DomainError as error:
            if error.code not in {'source_unavailable','asset_not_found'}:raise
            continue
        return {'data':data,'acquisition_method':row['provenance'].get('acquisition_method','zotero_api_file')}
    return None


def checked_provenance(candidate, provenance):
    if (provenance.get('attachment_key')!=candidate['key']
            or str(provenance.get('attachment_version'))!=candidate['version']
            or provenance.get('metadata_md5')!=candidate['md5']):
        raise ConnectorError('source_changed_refresh_metadata')
    return {'attachment_key':candidate['key'],'attachment_version':candidate['version'],
            'attachment_md5':candidate['md5']}


def acquire(service, store, connector, *, max_files=3, keys=None, local_storage_root=None):
    from research_assistant.zotero_assets import ZoteroPdfIngestor
    selected=candidates(service,connector.namespace,keys=keys)
    if not selected:raise ConnectorError('no_annotated_pdf_candidates')
    counts=Counter();alignments=Counter();errors=Counter();receipts=[]
    # Missing cloud files do not turn into a full-library crawl.
    for candidate in selected[:max_files*2]:
        if counts['acquired']>=max_files:break
        counts['attempted']+=1
        try:
            current=connector.attachment_metadata(candidate['key'])
            provenance=checked_provenance(candidate,current)
            cached=cached_bytes(service,store,candidate['snapshot_id'])
            data=None if cached is None else cached['data']
            method='zotero_api_file' if cached is None else cached['acquisition_method']
            if cached is not None:
                counts['reused_cached']+=1
            elif local_storage_root is not None:
                from research_assistant.connectors.zotero_local import read_local_pdf
                data=read_local_pdf(local_storage_root,candidate['key'],candidate['md5'])
                if data is not None:
                    method='zotero_local_storage'
                    counts['copied_from_local']+=1;counts['copied_bytes']+=len(data)
            if data is None:
                result=connector.fetch_pdf(candidate['key'])
                if result.status!='ok':
                    raise ConnectorError(result.error_code or 'file_unavailable',retry_after=result.retry_after)
                provenance=checked_provenance(candidate,result.provenance)
                data=result.data
                counts['downloaded']+=1;counts['downloaded_bytes']+=len(data)
            if hashlib.md5(data,usedforsecurity=False).hexdigest()!=candidate['md5']:
                raise ConnectorError('file_integrity_mismatch')
            receipt=ZoteroPdfIngestor(service,store).ingest(data,
                attachment_snapshot_id=candidate['snapshot_id'],provenance=provenance,acquisition_method=method)
            if receipt.get('status') not in (None,'ok'):
                raise ConnectorError('pdf_ingestion_failed')
            # The importer returns only canonical IDs and counters.
            receipts.append({key:receipt[key] for key in ('source_snapshot_id','asset_id','page_count','deduplicated') if key in receipt})
            counts['acquired']+=1
            for status,total in receipt.get('alignment_counts',{}).items():
                if status in {'exact','normalized','ambiguous','unmatched','page_only','invalid_position'}:
                    alignments[status]+=total
        except (ConnectorError,AssetError,DomainError) as error:
            # Never echo a provider object, URL or HTTP exception.
            safe={'file_unavailable','not_found_or_inaccessible','source_unavailable','source_backoff',
                  'rate_limited','authentication_failed','access_revoked','file_access_not_granted',
                  'source_changed_refresh_metadata','file_integrity_mismatch','pdf_parse_failed',
                  'pdf_parse_timeout','attachment_changed_during_download','pdf_too_large','forgotten',
                  'file_hash_mismatch','file_length_mismatch',
                  'attachment_source_unavailable','invalid_file_metadata','pdf_ingestion_failed'}
            code=error.code if error.code in safe else 'pdf_acquisition_unavailable'
            errors[code]+=1
            if code in {'authentication_failed','access_revoked','file_access_not_granted','rate_limited','source_backoff'}:break
    requests=getattr(connector,'request_counts',{})
    requests={key:value for key,value in requests.items() if key in {'metadata','file','storage'}
              and type(value) is int and value>=0}
    return {'status':'ok' if counts['acquired']==min(max_files,len(selected)) and not errors else 'partial',
        'provider':'zotero','mode':'bounded_original_pdfs','remote_write':False,'llm_calls':0,
        'requested_files':max_files,'counts':dict(counts),'alignment_counts':dict(alignments),
        'errors':dict(errors),'receipts':receipts,'requests':requests,'complete_library_file_sync':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-config',type=Path,default=Path.home()/'.scholars-attendant/runtime/postgres.json')
    parser.add_argument('--credentials',type=Path,default=Path.home()/'.scholars-attendant/zotero.json')
    parser.add_argument('--max-files',type=int,default=3)
    parser.add_argument('--local-storage',type=Path,default=Path.home()/'Zotero/storage',
        help='Read-only standard Zotero storage root; original files remain untouched')
    parser.add_argument('--attachment-key',action='append',default=[])
    args=parser.parse_args(argv)
    if not 1<=args.max_files<=20:parser.error('--max-files must be 1..20')
    if any(not re.fullmatch(r'[A-Z0-9]{8}',key) for key in args.attachment_key):parser.error('invalid attachment key')
    service=None
    try:
        # This production acquisition command must use the registered external root.
        root=select_asset_root(None,args.runtime_config,environ={})
        runtime=object_file(args.runtime_config)
        if not runtime.get('asset_root') or not isinstance(runtime.get('asset_storage'),dict):
            raise AssetError('external_storage_mismatch')
        validate_external_storage(root,runtime['asset_storage'])
        credentials=object_file(args.credentials)
        if not str(runtime.get('database_url','')).startswith('postgresql+psycopg:'):
            raise ConnectorError('postgresql_configuration_required')
        service=ResearchService(runtime['database_url'],runtime.get('owner_id','local'),runtime.get('scope_id','private'))
        service.initialize()
        with ArtifactStore(root) as store,ZoteroFileClient(credentials['api_key'],
            str(credentials.get('library_id') or credentials['user_id']),library_type=credentials.get('library_type','user')) as connector:
            result=acquire(service,store,connector,max_files=args.max_files,keys=args.attachment_key,
                           local_storage_root=args.local_storage)
        print(json.dumps(result));return 0 if result['status']=='ok' else 1
    except (ConnectorError,AssetError,DomainError):
        print(json.dumps({'status':'failed','error_code':'pdf_acquisition_unavailable'}),file=sys.stderr);return 1
    except Exception:
        print(json.dumps({'status':'failed','error_code':'pdf_acquisition_unavailable'}),file=sys.stderr);return 1
    finally:
        if service is not None:service.storage.engine.dispose()


if __name__=='__main__':
    raise SystemExit(main())
