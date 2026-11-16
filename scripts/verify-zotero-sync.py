#!/usr/bin/env python3
"""Read-only live inventory / durable checkpoint / literal evidence acceptance.

Private configuration stays outside Git. Output contains counters and safe
codes only, never account IDs, object IDs, titles, URLs, keys or source bodies.
"""
import argparse
import json
from pathlib import Path
import sys

from sqlalchemy import select, func
from research_assistant import storage as db
from research_assistant.connectors.common import ConnectorError
from research_assistant.connectors.zotero import ZoteroReadOnlyClient
from research_assistant.service import ResearchService, DomainError, canonical, digest


def require(condition):
    if not condition:
        raise ConnectorError('acceptance_mismatch')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials',type=Path,default=Path.home()/'.scholars-attendant/zotero.json')
    parser.add_argument('--runtime-config',type=Path,default=Path.home()/'.scholars-attendant/runtime/postgres.json')
    args=parser.parse_args(argv)
    try:
        credentials=json.loads(args.credentials.read_text())
        runtime=json.loads(args.runtime_config.read_text())
        service=ResearchService(runtime['database_url'],runtime.get('owner_id','local'),runtime.get('scope_id','private'))
        service.initialize()
        with ZoteroReadOnlyClient(credentials['api_key'],str(credentials.get('library_id') or credentials['user_id']),
                library_type=credentials.get('library_type','user')) as connector:
            inventory=connector.inventory()
            require(inventory['complete'] and inventory['consistent'])
            registration=dict(provider='zotero',source_instance_id=connector.source_instance_id,namespace=connector.namespace)
        counts=dict(inventory_items=len(inventory['object_versions']),accessible_items=0,deleted_items=0,
            suppressed_items=0,source_hashes_verified=0,literal_anchors_verified=0)
        with service.storage.transaction() as c:
            service._epoch(c)
            registered=service._registration(c,registration)
            cursor=c.execute(select(db.connector_cursors).where(
                db.connector_cursors.c.registration_id==registered['id'])).mappings().first()
            require(cursor is not None and cursor['cursor']==inventory['version'])
            manifest=service._row(c,db.sync_manifests,cursor['run_id'])
            require(manifest['registration_id']==registered['id'] and manifest['status']=='complete'
                and manifest['source_version']==inventory['version']
                and manifest['inventory_hash']==digest(canonical(inventory['object_versions']))
                and manifest['inventory_complete'] and manifest['source_consistent'] and manifest['deletions_complete'])
            items={r['identity_hash']:dict(r) for r in c.execute(select(db.sync_items).where(
                db.sync_items.c.manifest_id==manifest['id'])).mappings()}
            objects={r['identity_hash']:dict(r) for r in c.execute(select(db.source_objects).where(
                service._scope(db.source_objects),db.source_objects.c.registration_id==registered['id'])).mappings()}
            for external_id,version in inventory['object_versions'].items():
                key=digest(canonical([registration['provider'],registration['source_instance_id'],registration['namespace'],external_id]))
                item=items.get(key)
                require(item is not None)
                if service._blocked(c,fingerprint=key):
                    require(item['state']=='suppressed')
                    counts['suppressed_items']+=1
                    continue
                require(item['expected_version']==version and item['state'] in {'persisted','unchanged','deleted'})
                obj=objects.get(key)
                require(obj is not None)
                if obj['access_state']=='deleted':
                    require(item['state'] in {'deleted','unchanged'})
                    counts['deleted_items']+=1
                    continue
                require(obj['access_state']=='accessible' and obj['head_source_version_id'] is not None)
                source=service._row(c,db.sources,obj['head_source_version_id'])
                require(source['access_state']=='accessible' and not service._blocked(c,identity=source['id']))
                require(digest(source['content'])==source['sha256'])
                metadata=c.execute(select(db.snapshot_metadata).where(
                    db.snapshot_metadata.c.source_version_id==source['id'])).mappings().one()
                require(metadata['external_version']==version and metadata['completeness']=='complete' and not metadata['missing_parts'])
                raw=json.loads(source['content'])
                for span in c.execute(select(db.evidence).where(service._scope(db.evidence),
                        db.evidence.c.source_version_id==source['id'])).mappings():
                    require(span['locator'].get('kind')=='json')
                    value=raw
                    for part in span['locator']['json_path']:
                        value=value[part]
                    require(isinstance(value,str) and value==span['text'] and digest(value)==span['quote_hash'])
                    counts['literal_anchors_verified']+=1
                counts['source_hashes_verified']+=1
                counts['accessible_items']+=1
            require(c.execute(select(func.count()).select_from(db.sync_failures).where(
                db.sync_failures.c.registration_id==registered['id'])).scalar_one()==0)
            counts['reading_sessions']=c.execute(select(func.count()).select_from(db.reading_sessions).where(
                service._scope(db.reading_sessions))).scalar_one()
        print(json.dumps(dict(status='ok',checkpoint_verified=True,**counts)))
        return 0
    except (ConnectorError,DomainError) as exc:
        # Do not propagate a driver/network exception or its connection string.
        safe={'acceptance_mismatch','insufficient_permissions','identity_mismatch','authentication_failed',
              'access_revoked','rate_limited','source_unavailable','source_not_registered'}
        print(json.dumps(dict(status='failed',error_code=exc.code if exc.code in safe else 'acceptance_unavailable')))
    except Exception:
        print(json.dumps(dict(status='failed',error_code='acceptance_unavailable')))
    return 1


if __name__=='__main__':
    sys.exit(main())
