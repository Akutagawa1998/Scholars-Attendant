"""Cache one already registered paper-collection page from a native fetch.

This command makes zero remote calls and generates no prose. Supply only the
structuredContent object from one explicitly requested native Notion fetch.
Never enumerate/follow child pages, create summaries or write back to Notion.
"""
import argparse
import json
from pathlib import Path
import sys
from uuid import UUID

from sqlalchemy import select
from research_assistant import storage as db
from research_assistant.connectors.notion import normalize_mcp_page
from research_assistant.connectors.common import aware_timestamp
from research_assistant.connectors.notion_native import native_page_kwargs
from research_assistant.service import ResearchService,canonical,digest
from research_assistant.storage_paths import select_asset_root,validate_external_storage

NATIVE_PATHS={key:[key] for key in ('truncated','unknown_block_count','unknown_block_ids','page_last_edited_at')}


def cache_page(service,raw):
    if not isinstance(raw,dict) or raw.get('metadata',{}).get('type')!='page':
        raise ValueError('invalid_native_page')
    identity=str(UUID(raw['id']))
    with service.storage.transaction() as c:
        service._epoch(c)
        rows=list(c.execute(select(db.source_objects,db.connector_registrations.c.source_instance_id,
            db.connector_registrations.c.namespace).join(db.connector_registrations,
            db.source_objects.c.registration_id==db.connector_registrations.c.id).where(
            service._scope(db.source_objects),service._scope(db.connector_registrations),
            db.connector_registrations.c.provider=='notion',db.source_objects.c.external_id==identity)).mappings())
        if len(rows)!=1:raise ValueError('registered_page_required')
        obj=rows[0]
        if not obj['namespace'].startswith('collection://') or obj['access_state']!='accessible':
            raise ValueError('registered_collection_page_required')
        source=service._row(c,db.sources,obj['head_source_version_id'])
        if (source['access_state']!='accessible' or service._blocked(c,identity=obj['id'],fingerprint=obj['identity_hash'])
                or service._blocked(c,identity=source['id'])):
            raise ValueError('source_unavailable')
        meta=c.execute(select(db.snapshot_metadata).where(service._scope(db.snapshot_metadata),
            db.snapshot_metadata.c.source_version_id==source['id'])).mappings().one()
    options=native_page_kwargs(raw,paths=NATIVE_PATHS)
    previous_revision=aware_timestamp(meta['external_version'])
    incoming_revision=aware_timestamp(options['external_version'])
    if previous_revision and incoming_revision and incoming_revision<previous_revision:
        raise ValueError('stale_native_revision')
    snapshot=normalize_mcp_page(**options,source_instance_id=obj['source_instance_id'],namespace=obj['namespace'],
        external_id=identity,markdown_path=['text'],passage_paths=[{'json_path':['title'],'section':'title'}],source_url=raw.get('url'))
    unchanged=(source['sha256']==digest(canonical(snapshot['raw_payload'])) and meta['external_version']==snapshot['external_version'])
    if unchanged:
        result_id=source['id']
    else:
        # The service rechecks scope, object suppression and version fences.
        response=service.execute('sources.ingest',dict(snapshot,idempotency_key='notion-on-demand:'+digest(canonical(snapshot))),trusted_capture=True)
        if response['status'] not in {'ok','partial'}:raise ValueError('notion_cache_failed')
        result_id=response['data']['source_snapshot_id']
    result=service.execute('sources.get_evidence',{'source_snapshot_id':result_id})
    if result['status']!='ok':raise ValueError('notion_cache_unavailable')
    if not result['data']['passages']:raise ValueError('notion_evidence_missing')
    with service.storage.transaction() as c:
        saved=service._row(c,db.sources,result_id)
        if saved['sha256']!=digest(saved['content']):raise ValueError('notion_source_hash_mismatch')
        payload=json.loads(saved['content'])
        for passage in result['data']['passages']:
            value=payload
            for part in passage['locator']['json_path']:value=value[part]
            if passage['text']!=value:raise ValueError('notion_literal_anchor_mismatch')
    return {'status':'ok','cached_pages':1,'remote_calls':0,'remote_write':False,'llm_calls':0,
        'cache_reused':unchanged,'completeness':snapshot['completeness'],'missing_parts':snapshot['missing_parts'],
        'body_characters':len(snapshot['raw_payload']['text']),
        'literal_evidence_anchors_verified':len(result['data']['passages']),
        'source_snapshot_id':result_id,'complete_collection_sync':False,'checkpoint_advanced':False}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native-fetch',required=True,type=Path)
    p.add_argument('--runtime-config',type=Path,default=Path.home()/'.scholars-attendant/runtime/postgres.json')
    args=p.parse_args(argv);service=None
    try:
        root=select_asset_root(None,args.runtime_config,environ={})
        runtime=json.loads(args.runtime_config.read_text());validate_external_storage(root,runtime['asset_storage'])
        raw=json.loads(args.native_fetch.read_text())
        service=ResearchService(runtime['database_url'],runtime.get('owner_id','local'),runtime.get('scope_id','private'))
        result=cache_page(service,raw)
        print(json.dumps(result));return 0
    except Exception:
        print(json.dumps({'status':'failed','error_code':'notion_cache_unavailable'}),file=sys.stderr);return 1
    finally:
        if service is not None:service.storage.engine.dispose()


if __name__=='__main__':
    raise SystemExit(main())
