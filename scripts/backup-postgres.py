#!/usr/bin/env python3
"""External-store logical backup and isolated restore drill; never promote a backup.

Deployment helper, run with the installed core. No provider access, live-data
restore, API startup, schedule, retention deletion or privacy-ledger replay.
Backups contain private source bodies and must stay in the registered APFS
store. A successful drill validates the captured state, not later permissions.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from research_assistant import __version__, storage as db
from research_assistant.annotation_review import AnnotationReviewService
from research_assistant.asset_service import _blob_lock, read_source_asset
from research_assistant.assets import ArtifactStore, AssetError
from research_assistant.service import ResearchService, DomainError
from research_assistant.annotation_review import _canonical_pages
from research_assistant.storage_paths import select_asset_root, validate_external_storage

LABEL = 'org.scholars-attendant.restore-drill'
NAME = 'scholars-attendant-postgres'
MIGRATION_LOCK = 728190471
HASH = re.compile(r'[a-f0-9]{64}\Z')
KEY = re.compile(r'sha256/([a-f0-9]{2})/([a-f0-9]{64})\.pdf\Z')
IDENTIFIER = re.compile(r'[a-z_][a-z0-9_]*\Z')


class BackupError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(value, code='backup_verification_failed'):
    if not value:
        raise BackupError(code)


def hash_file(path):
    require(not path.is_symlink() and path.is_file(), 'backup_file_unavailable')
    h = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def save(path, value):
    require(not path.is_symlink(), 'backup_path_invalid')
    with open(path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        stream.write(json.dumps(value, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def command(args, *, timeout=120, stdin=None, stdout=None):
    try:
        result = subprocess.run(args, stdin=stdin, stdout=stdout or subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False)
        require(result.returncode == 0, 'backup_command_failed')
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        raise BackupError('backup_command_unavailable') from None


def load_context(config_path, *, require_source=True):
    try:
        config = json.loads(Path(config_path).read_text())
        root = select_asset_root(None, config_path, environ={})
        validate_external_storage(root, config['asset_storage'])
        validate_external_storage(Path(config['postgres_storage']['data_dir']), config['postgres_storage'])
        require(config.get('container') == NAME and config.get('postgres_storage', {}).get('kind') == 'bind',
                'backup_configuration_invalid')
        require(config['image_digest'].startswith('pgvector/pgvector@sha256:'), 'backup_configuration_invalid')
        config = dict(config, _asset_root=root, _private_root=root.parent)
        if require_source:source_identity(config)
        return config
    except BackupError:
        raise
    except Exception:
        raise BackupError('backup_configuration_unavailable') from None


def validate_paths(config, parent):
    root = config['_asset_root']
    try:
        validate_external_storage(root, config['asset_storage'])
        validate_external_storage(Path(config['postgres_storage']['data_dir']), config['postgres_storage'])
    except AssetError:
        raise BackupError('backup_storage_unavailable') from None
    parent = Path(parent)
    private = Path(config['_private_root'])
    require(parent.is_absolute() and private.is_absolute() and '..' not in parent.parts
            and private == root.parent and parent.is_relative_to(private)
            and parent != private, 'backup_path_invalid')
    require(not any(path.is_symlink() for path in (parent, *parent.parents)), 'backup_path_invalid')


def source_identity(config):
    raw = command(['docker', 'inspect', '--format',
        '{"id":{{json .Id}},"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}},"image":{{json .Config.Image}},"running":{{json .State.Running}}}', NAME], timeout=10)
    info = json.loads(raw)
    require(info['labels'].get('org.scholars-attendant.component') == 'postgres'
            and info['running'] is True and info['image'] == config['image_digest'], 'backup_source_mismatch')
    mounts = [entry for entry in info['mounts'] if entry['Destination'] == '/var/lib/postgresql/data']
    require(len(mounts) == 1 and mounts[0]['Type'] == 'bind' and mounts[0]['RW'], 'backup_source_mismatch')
    source = mounts[0]['Source']
    if source.startswith('/host_mnt/Volumes/'):
        source = source[len('/host_mnt'):]
    require(Path(source) == Path(config['postgres_storage']['data_dir']), 'backup_source_mismatch')
    return info['id']


def tables(connection):
    names = list(connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")).scalars())
    require(names and all(IDENTIFIER.fullmatch(name) for name in names), 'backup_schema_invalid')
    return names


def inventory(connection):
    result = {}
    for name in tables(connection):
        # PostgreSQL serializes vectors, JSON and generated columns consistently.
        # Sorting complete row strings also covers tables without an id column.
        rows = sorted(connection.execute(text('SELECT to_jsonb(t)::text FROM public."' + name + '" AS t')).scalars())
        h = hashlib.sha256()
        for row in rows:
            encoded = row.encode()
            h.update(len(encoded).to_bytes(8, 'big')); h.update(encoded)
        result[name] = dict(count=len(rows), sha256=h.hexdigest())
    return result


def schema_details(connection):
    return dict(postgresql=connection.scalar(text('SHOW server_version')),
        pgvector=connection.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'")),
        migration=connection.scalar(text('SELECT version_num FROM alembic_version')),
        indexes=[list(row) for row in connection.execute(text(
            "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname"))],
        constraints=[list(row) for row in connection.execute(text(
            "SELECT c.relname,k.conname,pg_get_constraintdef(k.oid) FROM pg_constraint k "
            "JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' ORDER BY c.relname,k.conname"))])


def create_backup(config, backup_parent):
    validate_paths(config, backup_parent)
    original_id = source_identity(config)
    backup_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:12]
    directory = backup_parent / name
    directory.mkdir(mode=0o700)
    (directory / 'INCOMPLETE').touch(mode=0o600)
    engine = create_engine(config['database_url'], pool_pre_ping=True)
    try:
        with ArtifactStore(config['_asset_root']) as source, ArtifactStore(directory/'assets') as target:
            with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c, c.begin():
                c.execute(text("SET LOCAL lock_timeout='15s'"))
                c.execute(text("SET LOCAL statement_timeout='120s'"))
                c.execute(text('SELECT pg_advisory_xact_lock(:key)'), dict(key=MIGRATION_LOCK))
                # Matches scope -> blob lock order used by registration/purge.
                c.execute(text('SELECT id FROM scopes ORDER BY id FOR UPDATE')).all()
                assets = list(c.execute(text("SELECT DISTINCT sha256,byte_size,blob_key FROM source_assets WHERE state='active' ORDER BY sha256")).mappings())
                for asset in assets:
                    _blob_lock(c, asset['sha256'])
                names = tables(c)
                c.execute(text('LOCK TABLE ' + ','.join('public."'+name+'"' for name in names) + ' IN SHARE MODE'))
                before = inventory(c)
                details = schema_details(c)
                snapshot = c.scalar(text('SELECT pg_export_snapshot()'))
                require(isinstance(snapshot, str) and re.fullmatch(r'[A-Fa-f0-9-]+', snapshot), 'backup_snapshot_invalid')
                source_url = make_url(config['database_url'])
                with open(directory/'database.dump', 'xb') as stream:
                    os.chmod(stream.fileno(), 0o600)
                    command(['docker', 'exec', original_id, 'pg_dump', '-U', 'scholars_admin', '-d', source_url.database,
                        '--format=custom', '--no-owner', '--no-privileges', '--snapshot='+snapshot], stdout=stream)
                    stream.flush(); os.fsync(stream.fileno())
                copied = []
                for asset in assets:
                    data = source.read_blob(asset['blob_key'], asset['sha256'])
                    require(len(data) == asset['byte_size'])
                    blob = target.put_bytes(data)
                    require(blob['relative_blob_key'] == asset['blob_key'])
                    copied.append(dict(sha256=asset['sha256'], byte_size=len(data), blob_key=asset['blob_key']))
                require(inventory(c) == before, 'backup_source_changed')
                require(source_identity(config) == original_id, 'backup_source_changed')
                manifest = dict(format='scholars-postgres-backup-v1', complete=True, created_at=datetime.now(timezone.utc).isoformat(),
                    core_version=__version__, image_digest=config['image_digest'], schema=details, tables=before, assets=copied,
                    dump_sha256=hash_file(directory/'database.dump'), dump_bytes=(directory/'database.dump').stat().st_size,
                    protection='private_same_external_apfs', promotion_allowed=False, latest_privacy_ledger_replayed=False)
                save(directory/'manifest.json', manifest)
        (directory/'INCOMPLETE').unlink()
        return directory, manifest
    finally:
        engine.dispose()


def verify_bundle(directory):
    require(directory.is_absolute() and directory.is_dir()
            and not any(path.is_symlink() for path in (directory, *directory.parents))
            and '..' not in directory.parts
            and not (directory/'INCOMPLETE').exists()
            and not (directory/'INCOMPLETE').is_symlink(), 'backup_incomplete')
    require(not (directory/'manifest.json').is_symlink(), 'backup_path_invalid')
    try:
        require((directory/'manifest.json').stat().st_size <= 16*1024*1024, 'backup_manifest_invalid')
        manifest = json.loads((directory/'manifest.json').read_text())
        require(isinstance(manifest, dict), 'backup_manifest_invalid')
        require(manifest['format'] == 'scholars-postgres-backup-v1' and manifest['complete'] is True, 'backup_incomplete')
        require(isinstance(manifest['dump_sha256'], str) and HASH.fullmatch(manifest['dump_sha256'])
                and type(manifest['dump_bytes']) is int and manifest['dump_bytes'] > 0, 'backup_manifest_invalid')
        require(isinstance(manifest['image_digest'], str)
                and re.fullmatch(r'pgvector/pgvector@sha256:[a-f0-9]{64}', manifest['image_digest'])
                and manifest.get('promotion_allowed') is False
                and manifest.get('latest_privacy_ledger_replayed') is False, 'backup_manifest_invalid')
        require(isinstance(manifest['tables'], dict) and 0 < len(manifest['tables']) <= 100000,
                'backup_manifest_invalid')
        for name, details in manifest['tables'].items():
            require(isinstance(name, str) and IDENTIFIER.fullmatch(name) and isinstance(details, dict)
                    and type(details.get('count')) is int and details['count'] >= 0
                    and isinstance(details.get('sha256'), str) and HASH.fullmatch(details['sha256']), 'backup_manifest_invalid')
        schema = manifest['schema']
        require(isinstance(schema, dict) and all(isinstance(schema.get(key), str) and schema[key]
                for key in ('postgresql', 'pgvector', 'migration'))
                and isinstance(schema.get('indexes'), list) and isinstance(schema.get('constraints'), list),
                'backup_manifest_invalid')
        require(hash_file(directory/'database.dump') == manifest['dump_sha256']
                and (directory/'database.dump').stat().st_size == manifest['dump_bytes'], 'backup_dump_integrity_mismatch')
        require(isinstance(manifest['assets'], list) and len(manifest['assets']) <= 100000, 'backup_manifest_invalid')
        require((directory/'assets').is_dir() and not (directory/'assets').is_symlink(), 'backup_file_unavailable')
        seen = set()
        with ArtifactStore(directory/'assets') as store:
            for asset in manifest['assets']:
                require(isinstance(asset, dict) and isinstance(asset.get('blob_key'), str)
                        and isinstance(asset.get('sha256'), str), 'backup_manifest_invalid')
                match = KEY.fullmatch(asset['blob_key'])
                require(match and match[1] == match[2][:2] and match[2] == asset['sha256'] and asset['sha256'] not in seen,
                        'backup_manifest_invalid')
                require(type(asset['byte_size']) is int and 0 < asset['byte_size'] <= 50*1024*1024, 'backup_manifest_invalid')
                require(len(store.read_blob(asset['blob_key'], asset['sha256'])) == asset['byte_size'], 'backup_blob_integrity_mismatch')
                seen.add(asset['sha256'])
        return manifest
    except BackupError:
        raise
    except Exception:
        raise BackupError('backup_bundle_invalid') from None


def verify_domain(url, asset_root, config):
    """Recheck literal data through the installed core without source prose output."""
    s = ResearchService(url, config.get('owner_id','local'), config.get('scope_id','private'))
    counts = dict(source_hashes=0, json_anchors=0, pdf_anchors=0, active_blobs=0,
                  blocked_assets=0, derived_reviews=0, historical_or_scrubbed_reviews=0)
    try:
        with s.storage.transaction() as c:
            sources = {row['id']: dict(row) for row in c.execute(db.sources.select()).mappings()}
            for source in sources.values():
                if source['content']:
                    require(hashlib.sha256(source['content'].encode()).hexdigest() == source['sha256'])
                    counts['source_hashes'] += 1
            payloads = {}
            for evidence in c.execute(db.evidence.select()).mappings():
                if not evidence['text']:
                    continue
                require(hashlib.sha256(evidence['text'].encode()).hexdigest() == evidence['quote_hash'])
                loc = evidence['locator']; source = sources[evidence['source_version_id']]
                if loc['kind'] == 'json':
                    if source['id'] not in payloads:payloads[source['id']] = json.loads(source['content'])
                    value = payloads[source['id']]
                    for part in loc['json_path']:value = value[part]
                    require(value == evidence['text']); counts['json_anchors'] += 1
                elif loc['kind'] == 'pdf':
                    # Full page offsets are reconstructed by the guarded review service below.
                    require(loc['quote_hash'] == evidence['quote_hash'])
                    counts['pdf_anchors'] += 1
            assets = [dict(row) for row in c.execute(db.source_assets.select().where(db.source_assets.c.state=='active')).mappings()]
            page_map = {}
            for asset in assets:
                pages = _canonical_pages(sources[asset['source_version_id']],asset)
                page_map[asset['source_version_id']] = pages
            for evidence in c.execute(db.evidence.select()).mappings():
                if evidence['text'] and evidence['locator'].get('kind')=='pdf':
                    loc=evidence['locator'];page=page_map[evidence['source_version_id']][loc['page_index']]
                    require(page['text'][loc['text_start']:loc['text_end']]==evidence['text'])
                    require(page['text_sha256']==loc['page_text_sha256'])
            reviews = list(c.execute(db.annotation_alignment_resolutions.select()).mappings())
        with ArtifactStore(asset_root) as store:
            for asset in assets:
                scoped = ResearchService(url,asset['owner_id'],asset['scope_id'])
                raw = store.read_blob(asset['blob_key'],asset['sha256'])
                require(len(raw) == asset['byte_size'] and hashlib.sha256(raw).hexdigest() == asset['sha256'])
                try:
                    with scoped.storage.transaction() as c:
                        allowed=(sources[asset['source_version_id']]['access_state']=='accessible'
                            and not scoped._blocked(c,identity=asset['source_version_id'])
                            and scoped._linked_asset_available(c,asset['source_version_id']))
                    if allowed:
                        require(read_source_asset(scoped,store,asset['source_version_id'])==raw)
                        result=scoped.execute('sources.get_evidence',dict(source_snapshot_id=asset['source_version_id']))
                        require(result['status']=='ok' and result['data']['passages'])
                    else:
                        try:read_source_asset(scoped,store,asset['source_version_id'])
                        except DomainError as error:
                            require(error.code=='source_unavailable','restore_access_filter_failed')
                        else:raise BackupError('restore_access_filter_failed')
                        counts['blocked_assets']+=1
                    counts['active_blobs']+=1
                finally:scoped.storage.engine.dispose()
            if assets and not counts['blocked_assets'] and all(sources[a['source_version_id']]['kind']=='zotero_attachment_pdf'
                    and a['owner_id']==s.owner_id and a['scope_id']==s.scope_id for a in assets):
                from research_assistant.operators.verify_zotero_pdfs import verify
                counts['zotero_pdf_revalidation']=verify(s,store,expected_pdfs=len(assets))['counts']
            for row in reviews:
                scoped=ResearchService(url,row['owner_id'],row['scope_id'])
                try:
                    try:saved=AnnotationReviewService(scoped,store).read(row['alignment_id'])
                    except DomainError as error:
                        if error.code not in {'annotation_review_unavailable','attachment_source_unavailable','attachment_parent_unavailable'}:
                            raise
                        saved=[]
                    visible=any(item['review_id']==row['id'] for item in saved)
                    require(not visible or bool(row['report']),'restore_access_filter_failed')
                    counts['derived_reviews' if visible else 'historical_or_scrubbed_reviews']+=1
                finally:scoped.storage.engine.dispose()
        return counts
    finally:
        s.storage.engine.dispose()


def owned_container(name, token):
    try:
        info = json.loads(command(['docker','inspect','--format','{"id":{{json .Id}},"labels":{{json .Config.Labels}}}',name],timeout=10))
        require(info['labels'].get(LABEL) == token, 'restore_container_mismatch')
        return info['id']
    except BackupError:
        raise
    except Exception:
        raise BackupError('restore_container_mismatch') from None


def restore_drill(config, bundle):
    validate_paths(config, bundle)
    manifest = verify_bundle(bundle)
    require(manifest['image_digest'] == config['image_digest'], 'restore_image_mismatch')
    token = uuid4().hex
    area = config['_private_root']/'staging'/('restore-'+token)
    validate_paths(config, area)
    pgdata = area/'pgdata'
    app_password = secrets.token_urlsafe(36)
    secret = area/'admin-password'
    target_assets = area/'assets'
    name = 'scholars-restore-'+token[:12]
    created = False; area_created = False; cleanup_pending = False; engine = None
    result = None; failure = None
    try:
        area.mkdir(parents=True,mode=0o700);area_created=True
        pgdata.mkdir(mode=0o700)
        secret.write_text(secrets.token_urlsafe(36));secret.chmod(0o600)
        with ArtifactStore(bundle/'assets') as source, ArtifactStore(target_assets) as target:
            for asset in manifest['assets']:target.put_bytes(source.read_blob(asset['blob_key'],asset['sha256']))
        created = True  # Even an uncertain docker run must be reconciled by its unique label/name.
        command(['docker','run','-d','--pull','never','--name',name,'--label',LABEL+'='+token,
            '--memory','1g','--cpus','2','--shm-size','256m','--restart','no',
            '-p','127.0.0.1::5432','-e','POSTGRES_USER=restore_admin','-e','POSTGRES_DB=scholars_restore',
            '-e','POSTGRES_PASSWORD_FILE=/run/secrets/admin-password',
            '--mount','type=bind,src='+str(pgdata)+',dst=/var/lib/postgresql/data',
            '--mount','type=bind,src='+str(secret)+',dst=/run/secrets/admin-password,readonly',config['image_digest']])
        identity = owned_container(name,token)
        deadline = time.monotonic()+60
        while time.monotonic()<deadline:
            # Official-image initialization temporarily accepts Unix-socket
            # connections before POSTGRES_DB exists. Require the final TCP
            # server, not that bootstrap server, before role/extension setup.
            ready = subprocess.run(['docker','exec',identity,'pg_isready','-h','127.0.0.1','-U','restore_admin','-d','scholars_restore'],capture_output=True,timeout=5)
            if ready.returncode==0:break
            time.sleep(0.25)
        else:raise BackupError('restore_not_ready')
        # SQL literal stays on private stdin; never in process arguments/logs.
        sql = ("CREATE ROLE scholars_app LOGIN PASSWORD '"+app_password+"'; CREATE EXTENSION vector; "
               "GRANT CONNECT,CREATE ON DATABASE scholars_restore TO scholars_app; "
               "GRANT USAGE,CREATE ON SCHEMA public TO scholars_app;")
        proc = subprocess.run(['docker','exec','-i',identity,'psql','-X','-v','ON_ERROR_STOP=1','-U','restore_admin','-d','scholars_restore'],
            input=sql.encode(),capture_output=True,timeout=30)
        require(proc.returncode==0,'restore_role_setup_failed')
        with (bundle/'database.dump').open('rb') as source:
            command(['docker','exec','-i',identity,'pg_restore','-U','restore_admin','--role=scholars_app',
                '-d','scholars_restore','--no-owner','--no-privileges','--no-comments','--single-transaction','--exit-on-error'],stdin=source)
        ports = json.loads(command(['docker','inspect','--format','{{json .NetworkSettings.Ports}}',identity],timeout=10))
        endpoint = ports['5432/tcp']; require(len(endpoint)==1 and endpoint[0]['HostIp']=='127.0.0.1','restore_port_invalid')
        url = make_url(config['database_url']).set(username='scholars_app', password=app_password,
            host='127.0.0.1',port=int(endpoint[0]['HostPort']),database='scholars_restore')
        engine = create_engine(url,pool_pre_ping=True,connect_args={'connect_timeout':5})
        with engine.begin() as c:
            require(inventory(c)==manifest['tables'],'restore_table_mismatch')
            require(schema_details(c)==manifest['schema'],'restore_schema_mismatch')
            require(c.scalar(text('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')) is False)
            require(c.scalar(text("SELECT '[1,0]'::vector <=> '[0,1]'::vector")) == 1.0)
            require(c.scalar(text("SELECT to_tsvector('simple','backup restore') @@ plainto_tsquery('simple','restore')")) is True)
        counts = verify_domain(url.render_as_string(hide_password=False),target_assets,config)
        engine.dispose(); engine = None
        command(['docker','restart',identity])
        # Docker may allocate a different ephemeral HostPort when restarting.
        ports=json.loads(command(['docker','inspect','--format','{{json .NetworkSettings.Ports}}',identity],timeout=10))
        endpoint=ports['5432/tcp']
        require(len(endpoint)==1 and endpoint[0]['HostIp']=='127.0.0.1','restore_port_invalid')
        url=url.set(port=int(endpoint[0]['HostPort']))
        # A real server restart must still preserve exactly the restored rows.
        deadline=time.monotonic()+60
        engine=create_engine(url,pool_pre_ping=True,connect_args={'connect_timeout':5})
        while time.monotonic()<deadline:
            try:
                with engine.begin() as c:require(inventory(c)==manifest['tables'],'restore_restart_mismatch')
                break
            except BackupError:raise
            except Exception:time.sleep(0.25)
        else:raise BackupError('restore_restart_unavailable')
        result=dict(status='ok',tables_verified=len(manifest['tables']),rows_verified=sum(t['count'] for t in manifest['tables'].values()),
            schema_verified=True,counts=counts,postgresql=manifest['schema']['postgresql'],pgvector=manifest['schema']['pgvector'],
            migration=manifest['schema']['migration'],app_is_superuser=False,restart_verified=True,
            restart_endpoint_reloaded=True,
            production_replaced=False,restored_api_started=False,promotion_allowed=False,
            latest_privacy_ledger_replayed=False,provider_calls=0,llm_calls=0)
    except Exception as error:
        failure = error.code if isinstance(error,BackupError) else 'restore_drill_failed'
    finally:
        if engine is not None:engine.dispose()
        if created:
            try:
                identity=owned_container(name,token)
                command(['docker','rm','-f',identity],timeout=30)
                # Mount only our new disposable PGDATA, never the production path.
                validate_paths(config,pgdata)
                command(['docker','run','--rm','--pull','never','--network','none','--user','root','--read-only',
                    '--mount','type=bind,src='+str(pgdata)+',dst=/data',
                    '--entrypoint','find',config['image_digest'],'/data','-mindepth','1','-delete'],timeout=60)
                pgdata.rmdir()
            except Exception:
                cleanup_pending=True
        if area_created and not cleanup_pending:
            try:shutil.rmtree(area)
            except Exception:cleanup_pending=True
    if failure or cleanup_pending:
        error=BackupError(failure or 'restore_cleanup_pending')
        error.cleanup_pending=cleanup_pending; error.resource_name=name
        raise error
    result['temporary_restore_resources_removed']=True
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['create','verify','restore-drill'])
    parser.add_argument('--runtime-config',type=Path,default=Path.home()/'.scholars-attendant/runtime/postgres.json')
    parser.add_argument('--bundle',type=Path)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args(argv)
    try:
        config=load_context(args.runtime_config,require_source=args.command=='create')
        if args.report:validate_paths(config,args.report)
        if args.command=='create':
            directory,manifest=create_backup(config,config['_private_root']/'backups')
            result=dict(status='ok',backup_directory=str(directory),tables=len(manifest['tables']),
                rows=sum(t['count'] for t in manifest['tables'].values()),blobs=len(manifest['assets']),dump_sha256=manifest['dump_sha256'])
        else:
            require(args.bundle is not None,'backup_directory_required');validate_paths(config,args.bundle)
            if args.command=='verify':
                manifest=verify_bundle(args.bundle)
                result=dict(status='ok',tables=len(manifest['tables']),blobs=len(manifest['assets']))
            else:result=restore_drill(config,args.bundle)
        if args.report:save(args.report,result)
        print(json.dumps(result));return 0
    except Exception as error:
        result=dict(status='failed',error_code=error.code if isinstance(error,BackupError) else 'backup_operation_failed')
        if getattr(error,'cleanup_pending',False):result.update(cleanup_pending=True,resource_name=error.resource_name)
        print(json.dumps(result),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
