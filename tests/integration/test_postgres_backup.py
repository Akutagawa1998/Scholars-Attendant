"""Backup fault checks use temporary files and mocked processes, never a server."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research_assistant.assets import ArtifactStore, AssetError

IMAGE = 'pgvector/pgvector@sha256:' + 'a' * 64
DATA = b'%PDF-SYNTHETIC private backup bytes'
TABLES = {'source_versions': {'count': 3, 'sha256': 'b' * 64}}
SCHEMA = dict(postgresql='17.11', pgvector='0.8.7', migration='0007', indexes=[], constraints=[])


@pytest.fixture
def helper():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'backup-postgres.py'
    spec = importlib.util.spec_from_file_location('synthetic_backup_' + uuid4().hex, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def config(tmp_path, helper, monkeypatch):
    private = tmp_path / 'synthetic-apfs'
    private.mkdir(mode=0o700)
    root = private / 'assets'
    with ArtifactStore(root) as store:
        blob = store.put_bytes(DATA)
    monkeypatch.setattr(helper, 'validate_external_storage', lambda root, storage: root)
    return dict(_asset_root=root, _private_root=private,
        asset_storage={'synthetic': True}, postgres_storage=dict(kind='bind', data_dir=str(private/'postgres')),
        database_url='postgresql+psycopg://synthetic:synthetic@127.0.0.1:55432/scholars',
        image_digest=IMAGE, container=helper.NAME, owner_id='fixture', scope_id='private', _blob=blob)


@pytest.fixture
def bundle(config, helper):
    directory = config['_private_root'] / 'backups' / 'synthetic-complete'
    directory.mkdir(parents=True, mode=0o700)
    dump = directory / 'database.dump'
    dump.write_bytes(b'PGDMP-SYNTHETIC-DUMP')
    dump.chmod(0o600)
    with ArtifactStore(directory/'assets') as store:
        blob = store.put_bytes(DATA)
    manifest = dict(format='scholars-postgres-backup-v1', complete=True,
        created_at='2026-10-21T00:00:00+00:00', core_version='0.4.0', image_digest=IMAGE,
        tables=deepcopy(TABLES), schema=deepcopy(SCHEMA),
        assets=[dict(sha256=blob['sha256'], byte_size=blob['size'], blob_key=blob['relative_blob_key'])],
        dump_sha256=helper.hash_file(dump), dump_bytes=dump.stat().st_size,
        protection='private_same_external_apfs', promotion_allowed=False, latest_privacy_ledger_replayed=False)
    helper.save(directory/'manifest.json', manifest)
    return directory, manifest


def rewrite(helper, bundle, mutate):
    directory, original = bundle
    changed = deepcopy(original)
    mutate(changed)
    helper.save(directory/'manifest.json', changed)
    return directory


def test_complete_bundle_checks_dump_assets_tables_without_processes(helper, bundle, monkeypatch):
    monkeypatch.setattr(helper, 'command', lambda *a, **k: pytest.fail('Verification must not invoke a process'))
    directory, manifest = bundle
    assert helper.verify_bundle(directory) == manifest
    assert stat.S_IMODE((directory/'manifest.json').stat().st_mode) == 0o600
    assert helper.hash_file(directory/'database.dump') == hashlib.sha256(b'PGDMP-SYNTHETIC-DUMP').hexdigest()


@pytest.mark.parametrize('fault', ['missing_dump', 'tampered_dump', 'missing_asset', 'tampered_asset', 'missing_asset_directory'])
def test_missing_or_tampered_private_files_fail_closed(helper, bundle, fault):
    directory, manifest = bundle
    blob = directory/'assets'/manifest['assets'][0]['blob_key']
    if fault == 'missing_dump': (directory/'database.dump').unlink()
    elif fault == 'tampered_dump': (directory/'database.dump').write_bytes(b'PRIVATE-DUMP-TAMPER')
    elif fault == 'missing_asset': blob.unlink()
    elif fault == 'tampered_asset': blob.write_bytes(b'PRIVATE-ASSET-TAMPER')
    else: shutil.rmtree(directory/'assets')
    with pytest.raises(helper.BackupError) as error:
        helper.verify_bundle(directory)
    assert 'PRIVATE' not in str(error.value)
    if fault == 'missing_asset_directory':
        assert not (directory/'assets').exists()


@pytest.mark.parametrize('value', [False, 1, 'true', None])
def test_only_explicit_complete_manifest_is_accepted(helper, bundle, value):
    directory = rewrite(helper, bundle, lambda m: m.update(complete=value))
    with pytest.raises(helper.BackupError):
        helper.verify_bundle(directory)


@pytest.mark.parametrize('marker', ['regular', 'dangling_symlink'])
def test_incomplete_marker_prevents_publication_even_with_valid_manifest(helper, bundle, marker):
    directory, _ = bundle
    if marker == 'regular': (directory/'INCOMPLETE').touch()
    else: (directory/'INCOMPLETE').symlink_to(directory/'absent')
    with pytest.raises(helper.BackupError, match='backup_incomplete'):
        helper.verify_bundle(directory)


@pytest.mark.parametrize('mutate', [
    lambda m: m.update(dump_bytes=True),
    lambda m: m.update(dump_bytes=0),
    lambda m: m.update(dump_sha256='not-a-hash'),
    lambda m: m.update(tables={}),
    lambda m: m['tables']['source_versions'].update(count=True),
    lambda m: m['tables']['source_versions'].update(sha256='bad'),
    lambda m: m.update(schema={}),
    lambda m: m.update(image_digest='unregistered-image'),
    lambda m: m.update(promotion_allowed=True),
    lambda m: m.update(latest_privacy_ledger_replayed=True),
    lambda m: m['assets'][0].update(byte_size=True),
    lambda m: m['assets'].append(deepcopy(m['assets'][0])),
    lambda m: m['assets'][0].update(blob_key='../PRIVATE/outside.pdf'),
    lambda m: m['assets'][0].update(blob_key='/PRIVATE/outside.pdf'),
])
def test_manifest_types_scope_keys_and_safety_flags_are_validated(helper, bundle, mutate):
    directory = rewrite(helper, bundle, mutate)
    with pytest.raises(helper.BackupError) as error:
        helper.verify_bundle(directory)
    assert 'PRIVATE' not in str(error.value)


@pytest.mark.parametrize('target', ['manifest', 'dump', 'asset', 'ancestor'])
def test_symlink_aliases_cannot_escape_bundle_checks(helper, bundle, tmp_path, target):
    directory, manifest = bundle
    if target == 'ancestor':
        alias = tmp_path/'alias'
        alias.symlink_to(directory.parent, target_is_directory=True)
        chosen = alias/directory.name
    else:
        chosen = directory
        path = {'manifest': directory/'manifest.json', 'dump': directory/'database.dump',
                'asset': directory/'assets'/manifest['assets'][0]['blob_key']}[target]
        moved = tmp_path/('original-'+target)
        path.rename(moved)
        path.symlink_to(moved)
    with pytest.raises(helper.BackupError):
        helper.verify_bundle(chosen)


@pytest.mark.parametrize('parent_kind', ['outside', 'dotdot', 'relative', 'root', 'symlink'])
def test_invalid_backup_parent_fails_before_database_or_process(helper, config, tmp_path, monkeypatch, parent_kind):
    private = config['_private_root']
    parents = dict(outside=tmp_path/'outside', dotdot=private/'..'/'escaped', relative=Path('relative'), root=private)
    if parent_kind == 'symlink':
        alias = private/'alias'
        alias.symlink_to(tmp_path, target_is_directory=True)
        parent = alias/'backup'
    else: parent = parents[parent_kind]
    monkeypatch.setattr(helper, 'source_identity', lambda *a: pytest.fail('No source process before path validation'))
    monkeypatch.setattr(helper, 'create_engine', lambda *a, **k: pytest.fail('No database before path validation'))
    with pytest.raises(helper.BackupError, match='backup_path_invalid'):
        helper.create_backup(config, parent)
    assert not (tmp_path/'escaped').exists()


def test_offline_registered_storage_fails_before_creating_backup(helper, config, monkeypatch):
    def offline(*args): raise AssetError('external_storage_unavailable')
    monkeypatch.setattr(helper, 'validate_external_storage', offline)
    monkeypatch.setattr(helper, 'command', lambda *a, **k: pytest.fail('Offline storage must not start processes'))
    parent = config['_private_root']/'backups'
    with pytest.raises(helper.BackupError, match='backup_storage_unavailable'):
        helper.create_backup(config, parent)
    assert not parent.exists()


class FakeRows:
    def __init__(self, values=()): self.values = list(values)
    def mappings(self): return self.values
    def all(self): return self.values
    def scalars(self): return self.values


def test_inventory_is_order_independent_but_detects_changed_rows(helper, monkeypatch):
    monkeypatch.setattr(helper, 'tables', lambda *a: ['source_versions'])
    class Connection:
        rows = ['{"id":"first","content":"SYNTHETIC A"}', '{"id":"second","content":"SYNTHETIC B"}']
        def execute(self, query): return FakeRows(self.rows)
    connection = Connection()
    first = helper.inventory(connection)
    connection.rows = list(reversed(connection.rows))
    assert helper.inventory(connection) == first
    connection.rows[0] = '{"id":"second","content":"SYNTHETIC CHANGED"}'
    changed = helper.inventory(connection)
    assert changed['source_versions']['count'] == first['source_versions']['count'] == 2
    assert changed['source_versions']['sha256'] != first['source_versions']['sha256']


class FakeConnection:
    dialect = SimpleNamespace(name='postgresql')
    def __init__(self, assets=()): self.assets = assets
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execution_options(self, **kwargs): return self
    def begin(self): return nullcontext(self)
    def execute(self, query, *args):
        return FakeRows(self.assets if 'DISTINCT sha256,byte_size,blob_key' in str(query) else [])
    def scalar(self, query):
        if 'pg_export_snapshot' in str(query): return '0001-0002-1'
        return None


class FakeEngine:
    def __init__(self, assets=()): self.connection, self.disposed = FakeConnection(assets), False
    def connect(self): return self.connection
    def begin(self): return nullcontext(self.connection)
    def dispose(self): self.disposed = True


def setup_backup_database(helper, config, monkeypatch):
    blob = config['_blob']
    engine = FakeEngine([dict(sha256=blob['sha256'], byte_size=blob['size'], blob_key=blob['relative_blob_key'])])
    monkeypatch.setattr(helper, 'create_engine', lambda *a, **k: engine)
    monkeypatch.setattr(helper, 'source_identity', lambda *a: 'synthetic-production-id')
    monkeypatch.setattr(helper, 'tables', lambda *a: list(TABLES))
    monkeypatch.setattr(helper, 'inventory', lambda *a: deepcopy(TABLES))
    monkeypatch.setattr(helper, 'schema_details', lambda *a: deepcopy(SCHEMA))
    return engine


def test_dump_failure_keeps_incomplete_and_never_publishes_manifest(helper, config, monkeypatch):
    engine = setup_backup_database(helper, config, monkeypatch)
    def fail(args, **options):
        assert 'pg_dump' in args
        options['stdout'].write(b'PGDMP-PARTIAL-PRIVATE')
        raise helper.BackupError('backup_command_failed')
    monkeypatch.setattr(helper, 'command', fail)
    parent = config['_private_root']/'backups'
    with pytest.raises(helper.BackupError, match='backup_command_failed'):
        helper.create_backup(config, parent)
    areas = list(parent.iterdir())
    assert len(areas) == 1 and (areas[0]/'INCOMPLETE').exists()
    assert not (areas[0]/'manifest.json').exists() and engine.disposed
    with pytest.raises(helper.BackupError, match='backup_incomplete'):
        helper.verify_bundle(areas[0])


def test_complete_backup_publishes_only_after_dump_and_blob_integrity(helper, config, monkeypatch):
    engine = setup_backup_database(helper, config, monkeypatch)
    def dump(args, **options):
        assert 'pg_dump' in args and not any('synthetic:synthetic' in arg for arg in args)
        assert (Path(options['stdout'].name).parent/'INCOMPLETE').exists()
        options['stdout'].write(b'PGDMP-SYNTHETIC-COMPLETE')
    monkeypatch.setattr(helper, 'command', dump)
    directory, manifest = helper.create_backup(config, config['_private_root']/'backups')
    assert engine.disposed and not (directory/'INCOMPLETE').exists()
    assert helper.verify_bundle(directory) == manifest
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory/'database.dump').stat().st_mode) == 0o600


@pytest.mark.parametrize('cleanup_failure', [False, True])
def test_restore_failure_reconciles_only_its_disposable_resources(helper, config, bundle, monkeypatch, cleanup_failure):
    commands = []
    directory, manifest = bundle
    original = {str(path.relative_to(directory)): helper.hash_file(path)
                for path in directory.rglob('*') if path.is_file()}
    monkeypatch.setattr(helper, 'owned_container', lambda name, token: 'synthetic-restore-id' if name.startswith('scholars-restore-') else pytest.fail('Production container targeted'))
    def process(args, **options):
        if 'pg_isready' in args:
            # The official image's temporary Unix server can report ready
            # before initdb has created the target database.
            assert '-h' in args and args[args.index('-h')+1] == '127.0.0.1'
            assert options['timeout'] == 5
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
    monkeypatch.setattr(helper.subprocess, 'run', process)
    def command(args, **kwargs):
        commands.append(args)
        if 'pg_restore' in args: raise helper.BackupError('backup_command_failed')
        if args[:3] == ['docker', 'rm', '-f'] and cleanup_failure:
            raise helper.BackupError('backup_command_unavailable')
        return b'{}'
    monkeypatch.setattr(helper, 'command', command)
    with pytest.raises(helper.BackupError) as error:
        helper.restore_drill(config, directory)
    assert error.value.code == 'backup_command_failed'
    assert error.value.cleanup_pending is cleanup_failure
    assert error.value.resource_name.startswith('scholars-restore-')
    assert ['docker', 'rm', '-f', 'synthetic-restore-id'] in commands
    assert not any(helper.NAME in args for args in commands)
    assert bool(list((config['_private_root']/'staging').iterdir())) is cleanup_failure
    assert original == {str(path.relative_to(directory)): helper.hash_file(path)
                        for path in directory.rglob('*') if path.is_file()}


def test_invalid_bundle_refuses_restore_before_any_container(helper, config, bundle, monkeypatch):
    directory, _ = bundle
    (directory/'INCOMPLETE').touch()
    monkeypatch.setattr(helper, 'command', lambda *a, **k: pytest.fail('No container before bundle verification'))
    with pytest.raises(helper.BackupError): helper.restore_drill(config, directory)
    assert not (config['_private_root']/'staging').exists()


def test_restore_reloads_dynamic_endpoint_after_restart(helper, config, bundle, monkeypatch):
    directory, manifest = bundle
    connections = []
    ports = iter(['6543', '6544'])
    monkeypatch.setattr(helper, 'owned_container', lambda *a: 'synthetic-restore-id')
    monkeypatch.setattr(helper.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    def command(args, **options):
        if '{{json .NetworkSettings.Ports}}' in args:
            return json.dumps({'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':next(ports)}]}).encode()
        return b'{}'
    class Connection:
        def scalar(self, query):
            sql = str(query)
            return False if 'rolsuper' in sql else (1.0 if '<=>' in sql else True)
    class Engine:
        def begin(self):return nullcontext(Connection())
        def dispose(self):pass
    def engine(url, **options):
        connections.append(url.port)
        assert options['connect_args']['connect_timeout'] == 5
        return Engine()
    monkeypatch.setattr(helper, 'command', command)
    monkeypatch.setattr(helper, 'create_engine', engine)
    monkeypatch.setattr(helper, 'inventory', lambda c: manifest['tables'])
    monkeypatch.setattr(helper, 'schema_details', lambda c: manifest['schema'])
    monkeypatch.setattr(helper, 'verify_domain', lambda *a: {'synthetic':True})
    result = helper.restore_drill(config, directory)
    assert connections == [6543, 6544]
    assert result['restart_verified'] and result['restart_endpoint_reloaded']
    assert result['temporary_restore_resources_removed'] and not result['production_replaced']
    assert not list((config['_private_root']/'staging').iterdir())


@pytest.mark.parametrize('failure', ['exit', 'timeout'])
def test_command_errors_never_echo_private_diagnostics(helper, monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == 'timeout': raise subprocess.TimeoutExpired('PRIVATE-COMMAND', 1, stderr=b'PRIVATE-SECRET')
        return SimpleNamespace(returncode=1, stdout=b'PRIVATE-BODY', stderr=b'PRIVATE-PASSWORD')
    monkeypatch.setattr(helper.subprocess, 'run', run)
    with pytest.raises(helper.BackupError) as error: helper.command(['synthetic-command'])
    assert error.value.code in {'backup_command_failed', 'backup_command_unavailable'}
    assert 'PRIVATE' not in str(error.value)
