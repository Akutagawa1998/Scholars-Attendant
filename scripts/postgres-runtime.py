#!/usr/bin/env python3
"""Manage only the dedicated local Scholars-Attendant PostgreSQL container.

Private credentials/config are kept outside the checkout. This script never
prints connection strings or server errors containing bound parameters.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

NAME = 'scholars-attendant-postgres'
VOLUME = 'scholars-attendant-pgdata'
LABEL = 'org.scholars-attendant.component'
DEFAULT_IMAGE = 'pgvector/pgvector:0.8.7-pg17-bookworm'

def docker(*args, check=True):
    result = subprocess.run(['docker', *args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError('Docker operation failed: ' + args[0])
    return result

def save(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)
    path.chmod(0o600)

def postgres_mount(config, *, probe=None, is_mount=None):
    """Validate the configured store before returning a Docker mount argument.

    A configured external store is authoritative. A disconnected disk never
    falls back to the old named volume or creates a fresh internal database.
    """
    storage = config.get('postgres_storage')
    if storage is None:
        return 'type=volume,src='+VOLUME+',dst=/var/lib/postgresql/data'
    if not isinstance(storage, dict) or storage.get('kind') != 'bind':
        raise RuntimeError('Unsupported PostgreSQL storage configuration')
    from research_assistant.assets import AssetError
    from research_assistant.storage_paths import validate_external_storage
    try:
        root = validate_external_storage(Path(storage.get('data_dir', '')), storage,
                                         probe=probe, is_mount=is_mount)
    except (AssetError, TypeError, ValueError):
        raise RuntimeError('External PostgreSQL storage is unavailable or does not match its registered volume') from None
    if any(char in str(root) for char in (',', '\n', '\r', '\0')):
        raise RuntimeError('Unsupported PostgreSQL storage path')
    return 'type=bind,src='+str(root)+',dst=/var/lib/postgresql/data'

def validate_initialized_data(config, mount, *, runner=None):
    """Check PGDATA as its container owner, without exposing private settings.

    Docker may own the bind directory as uid 999 with mode 0700. The host user
    cannot reliably inspect its children, even while PostgreSQL can use them.
    """
    storage = config.get('postgres_storage')
    if not isinstance(storage, dict) or storage.get('initialized') is not True:
        return
    image = config.get('image_digest', '')
    if not isinstance(image, str) or not image.startswith('pgvector/pgvector@sha256:'):
        raise RuntimeError('Initialized PostgreSQL storage requires its pinned image')
    result = (runner or docker)(
        'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
        '--user', 'postgres', '--entrypoint', 'sh', '--mount', mount+',readonly', image,
        '-c', 'test -f /var/lib/postgresql/data/PG_VERSION && '
              'test -s /var/lib/postgresql/data/PG_VERSION && '
              'test ! -L /var/lib/postgresql/data/PG_VERSION', check=False)
    if result.returncode != 0:
        raise RuntimeError('Initialized external PostgreSQL data is missing or unavailable')

def validate_container_mount(config, mounts):
    """Match only the data destination, without inspecting credentials/env."""
    if not isinstance(mounts, list):
        raise RuntimeError('Dedicated container data mount is unavailable')
    data = [mount for mount in mounts if isinstance(mount, dict)
            and mount.get('Destination') == '/var/lib/postgresql/data']
    if len(data) != 1 or data[0].get('RW') is not True:
        raise RuntimeError('Dedicated container data mount does not match private configuration')
    actual = data[0]
    storage = config.get('postgres_storage')
    if storage is None:
        matches = actual.get('Type') == 'volume' and actual.get('Name') == VOLUME
    else:
        source = actual.get('Source')
        # Docker Desktop reports the VM-side spelling for macOS volume binds.
        # Accept only this exact prefix, never an arbitrary matching suffix.
        if isinstance(source, str) and source.startswith('/host_mnt/Volumes/'):
            source = source[len('/host_mnt'):]
        matches = (isinstance(storage, dict) and storage.get('kind') == 'bind'
                   and actual.get('Type') == 'bind' and isinstance(source, str)
                   and Path(source).resolve() == Path(storage.get('data_dir', '')).expanduser().resolve())
    if not matches:
        raise RuntimeError('Dedicated container data mount does not match private configuration')

def container_run_args(config, runtime, mount):
    return ('run', '-d', '--name', NAME, '--label', LABEL+'=postgres', '--restart', 'unless-stopped',
            '--memory', '1g', '--cpus', '2', '--shm-size', '256m',
            '-p', f"127.0.0.1:{config['port']}:5432",
            '-e', 'POSTGRES_USER=scholars_admin', '-e', 'POSTGRES_DB=scholars',
            '-e', 'POSTGRES_PASSWORD_FILE=/run/secrets/admin-password',
            '--mount', mount,
            '--mount', 'type=bind,src='+str(runtime/'admin-password')+',dst=/run/secrets/admin-password,readonly',
            config['image_digest'])

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['up', 'status', 'restart', 'stop'])
    parser.add_argument('--runtime-dir', type=Path, default=Path.home()/'.scholars-attendant/runtime')
    parser.add_argument('--port', type=int, default=55432)
    args = parser.parse_args(argv)
    runtime = args.runtime_dir.expanduser().resolve()
    config_path = runtime/'postgres.json'
    inspected = docker('container', 'inspect', '--format', '{{json .Config.Labels}}', NAME, check=False)
    exists = inspected.returncode == 0
    if exists and json.loads(inspected.stdout or '{}').get(LABEL) != 'postgres':
        raise RuntimeError('Container name is already owned by another deployment')
    if args.command == 'status' and not exists:
        raise RuntimeError('Dedicated container does not exist')
    config = json.loads(config_path.read_text()) if config_path.exists() else None
    mount = None
    if args.command != 'stop':
        if exists and config is None:
            raise RuntimeError('Existing database requires its original private runtime config')
        if config is not None:
            mount = postgres_mount(config)
            if exists:
                actual = docker('container', 'inspect', '--format', '{{json .Mounts}}', NAME)
                validate_container_mount(config, json.loads(actual.stdout))
            validate_initialized_data(config, mount)
    if args.command in ['restart', 'stop']:
        if not exists:
            raise RuntimeError('Dedicated container does not exist')
        docker(args.command, NAME)
        if args.command == 'stop':
            print(json.dumps({'container':NAME, 'status':'stopped'})); return
    if args.command == 'up':
        os.umask(0o077)
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        if exists and not config_path.exists():
            raise RuntimeError('Existing database requires its original private runtime config')
        if not config_path.exists():
            admin_password, app_password = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
            save(runtime/'admin-password', admin_password)
            save(runtime/'app-password', app_password)
            from sqlalchemy.engine import URL
            app_url = URL.create('postgresql+psycopg', username='scholars_app', password=app_password,
                                 host='127.0.0.1', port=args.port, database='scholars').render_as_string(hide_password=False)
            save(config_path, json.dumps({'database_url':app_url, 'owner_id':'local', 'scope_id':'private',
                 'port':args.port, 'container':NAME, 'volume':VOLUME, 'image_tag':DEFAULT_IMAGE}, indent=2))
        config = json.loads(config_path.read_text())
        mount = mount or postgres_mount(config)
        if not exists:
            image = config.get('image_digest')
            if not image:
                image_info = docker('image', 'inspect', '--format', '{{index .RepoDigests 0}}', DEFAULT_IMAGE)
                image = image_info.stdout.strip()
            if not image.startswith('pgvector/pgvector@sha256:'):
                raise RuntimeError('Pull the versioned pgvector image before starting')
            config['image_digest'] = image
            save(config_path, json.dumps(config, indent=2))
            if config.get('postgres_storage') is None:
                docker('volume', 'create', '--label', LABEL+'=postgres', VOLUME)
            else:
                # Only reached after volume identity and filesystem checks.
                Path(config['postgres_storage']['data_dir']).mkdir(parents=True, exist_ok=True, mode=0o700)
            docker(*container_run_args(config, runtime, mount))
        else:
            docker('start', NAME)
    if args.command in ['up', 'restart']:
        deadline = time.monotonic()+50
        while time.monotonic()<deadline:
            ready = docker('exec', NAME, 'pg_isready', '-U', 'scholars_admin', '-d', 'scholars', check=False)
            if ready.returncode == 0:
                break
            time.sleep(0.3)
        else:
            raise RuntimeError('Dedicated PostgreSQL did not become ready')
        if args.command == 'up':
            import psycopg
            from psycopg import sql
            config = json.loads(config_path.read_text())
            with psycopg.connect(host='127.0.0.1', port=config['port'], dbname='scholars', user='scholars_admin',
                                  password=(runtime/'admin-password').read_text(), autocommit=True) as connection:
                if not connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', ['scholars_app']).fetchone():
                    connection.execute(sql.SQL('CREATE ROLE scholars_app LOGIN PASSWORD {}').format(sql.Literal((runtime/'app-password').read_text())))
                connection.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
                connection.execute('GRANT CONNECT, CREATE ON DATABASE scholars TO scholars_app')
                connection.execute('GRANT USAGE, CREATE ON SCHEMA public TO scholars_app')
    if config_path.exists() and args.command != 'stop':
        import psycopg
        config = json.loads(config_path.read_text())
        with psycopg.connect(config['database_url'].replace('postgresql+psycopg://','postgresql://')) as connection:
            server = connection.execute('SHOW server_version').fetchone()[0]
            vector = connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0]
            superuser = connection.execute('SELECT rolsuper FROM pg_roles WHERE rolname=current_user').fetchone()[0]
        if args.command == 'up' and isinstance(config.get('postgres_storage'), dict):
            config['postgres_storage']['initialized'] = True
            save(config_path, json.dumps(config, indent=2))
        print(json.dumps({'container':NAME,'status':'ready','postgresql':server,'pgvector':vector,
            'app_is_superuser':superuser,'host':'127.0.0.1','port':config['port'],
            'image_digest':config.get('image_digest'),'private_config':str(config_path)}))

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status':'failed','error_type':type(error).__name__,
                          'message': str(error) if type(error) is RuntimeError else 'Database/runtime operation failed; credentials redacted'}))
        sys.exit(1)
